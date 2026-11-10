import os
import sys
import json
import copy
import time
import subprocess
import urllib.parse
import socket
import requests

if getattr(sys, 'frozen', False):
    CURRENT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.join(CURRENT_DIR, "core")
CONFIG_DIR = os.path.join(CURRENT_DIR, "config")
XRAY_EXE = os.path.join(CORE_DIR, "xray.exe")
SINGBOX_EXE = os.path.join(CORE_DIR, "sing-box.exe")
ACTIVE_CONFIG_PATH = os.path.join(CORE_DIR, "run_config.json")
TUN_CONFIG_PATH = os.path.join(CORE_DIR, "tun_config.json")
TUN_LOG_PATH = os.path.join(CORE_DIR, "tun.log")
XRAY_LOG_PATH = os.path.join(CORE_DIR, "xray.log")
NODES_JSON_PATH = os.path.join(CONFIG_DIR, "nodes.json")
NODES_BAK_PATH = os.path.join(CONFIG_DIR, "nodes.json.bak")
RULES_JSON_PATH = os.path.join(CONFIG_DIR, "routing_rules.json")

# 「规则」模式的默认分流规则（对齐 Loyalsoldier/clash-rules 那套逻辑）：
# 广告拦截 → 私有/国内/苹果直连 → 被墙走代理 → 国内 IP 直连 → Telegram IP 段代理 → 兜底代理
DEFAULT_ROUTING_RULES = {
    "屏蔽域名": ["geosite:category-ads-all"],
    "直连域名": ["geosite:private", "geosite:cn", "geosite:apple"],
    "代理域名": ["geosite:geolocation-!cn", "geosite:google", "geosite:gfw",
                "geosite:telegram", "geosite:twitter", "geosite:youtube", "geosite:github"],
    "直连IP": ["geoip:private", "geoip:cn"],
    "代理IP": ["geoip:telegram"],
    "屏蔽QUIC": True
}

class XrayManager:
    def __init__(self):
        self.process = None
        self.tun_process = None
        self._tun_log_f = None
        self._xray_log_f = None
        # True = 上次 stop_tun 已清掉幽灵网卡，本次 start 可跳过 PowerShell 清理（省 2~4 秒）
        self._tun_adapter_clean = False
        self.is_tun_running = False
        self.last_uplink = 0
        self.last_downlink = 0
        self.last_stats_time = time.time()
        self.uplink_speed = 0.0  # B/s
        self.downlink_speed = 0.0  # B/s
        self.is_running = False

    def load_nodes_data(self):
        default = {"active_node_id": "", "routing_mode": "rule", "system_proxy": False,
                   "allow_lan": False, "inbound_port": 10808, "nodes": []}
        for path in (NODES_JSON_PATH, NODES_BAK_PATH):
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 兼容旧配置文件：补齐 allow_lan 字段
                if "allow_lan" not in data:
                    data["allow_lan"] = False
                return data
            except Exception:
                continue  # 主文件损坏则尝试备份
        return default

    def save_nodes_data(self, data):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        # 原子写：先写临时文件再替换，写入中途断电/崩溃不会损坏节点数据
        tmp_path = NODES_JSON_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        if os.path.exists(NODES_JSON_PATH):
            try:
                import shutil
                shutil.copy2(NODES_JSON_PATH, NODES_BAK_PATH)
            except Exception:
                pass
        os.replace(tmp_path, NODES_JSON_PATH)

    def load_routing_rules(self) -> dict:
        """读取用户分流规则；不存在则写入默认规则，损坏则重建（坏文件留 .bad 备查）"""
        if not os.path.exists(RULES_JSON_PATH):
            self.save_routing_rules(DEFAULT_ROUTING_RULES)
            return copy.deepcopy(DEFAULT_ROUTING_RULES)
        try:
            with open(RULES_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 逐字段校验并补默认值
            merged = {}
            for key, default in DEFAULT_ROUTING_RULES.items():
                val = data.get(key, default)
                if isinstance(default, list):
                    merged[key] = [str(x) for x in val] if isinstance(val, list) else default
                else:
                    merged[key] = val if isinstance(val, bool) else default
            return merged
        except Exception:
            try:
                os.replace(RULES_JSON_PATH, RULES_JSON_PATH + ".bad")
            except Exception:
                pass
            self.save_routing_rules(DEFAULT_ROUTING_RULES)
            return copy.deepcopy(DEFAULT_ROUTING_RULES)

    def save_routing_rules(self, rules: dict):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(RULES_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(rules, f, indent=2, ensure_ascii=False)

    def parse_vless_url(self, url: str) -> dict:
        url = url.strip()
        if not url.startswith("vless://"):
            raise ValueError("URL must start with vless://")
        
        parsed = urllib.parse.urlparse(url)
        uuid = parsed.username
        server = parsed.hostname
        if not server:
            raise ValueError("链接缺少服务器地址")
        if not uuid:
            raise ValueError("链接缺少 UUID")
        port = parsed.port or 443
        remarks = urllib.parse.unquote(parsed.fragment) if parsed.fragment else "VLESS_Node"

        query = urllib.parse.parse_qs(parsed.query)
        def get_q(k, default=""):
            return query.get(k, [default])[0]

        return {
            "id": remarks or f"vless_{server}_{port}",
            "name": remarks or f"{server}:{port}",
            "protocol": "vless",
            "server": server,
            "port": int(port),
            "uuid": uuid,
            "network": get_q("type", "tcp"),
            "security": get_q("security", "reality"),
            "flow": get_q("flow", ""),
            "sni": get_q("sni", ""),
            "fingerprint": get_q("fp", "chrome"),
            "public_key": get_q("pbk", ""),
            "short_id": get_q("sid", ""),
            "spider_x": urllib.parse.unquote(get_q("spx", ""))
        }

    def generate_xray_config(self, node: dict, routing_mode: str = "rule", inbound_port: int = 10808, allow_lan: bool = False) -> dict:
        # Build outbound for the node
        server_ip = node.get("server", "").strip() or "127.0.0.1"
        if node.get("protocol") == "direct" or node.get("id") == "DIRECT":
            outbound_proxy = {
                "tag": "proxy",
                "protocol": "freedom"
            }
        else:
            outbound_proxy = {
                "tag": "proxy",
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": server_ip,
                            "port": int(node.get("port", 443)),
                            "users": [
                                {
                                    "id": node.get("uuid", ""),
                                    "encryption": "none",
                                    "flow": node.get("flow", "") or ("xtls-rprx-vision" if node.get("security") == "reality" else "")
                                }
                            ]
                        }
                    ]
                },
                "streamSettings": {
                    "network": "raw" if node.get("network", "tcp") == "tcp" else node.get("network", "tcp"),
                    "security": node.get("security", "reality"),
                    "realitySettings": {
                        "serverName": node.get("sni", ""),
                        "fingerprint": node.get("fingerprint", "chrome"),
                        "show": False,
                        "publicKey": node.get("public_key", ""),
                        "shortId": node.get("short_id", ""),
                        "spiderX": node.get("spider_x", "")
                    }
                },
                "mux": {
                    "enabled": False,
                    "concurrency": -1
                }
            }

        # Inbounds
        inbounds = [
            {
                "tag": "api",
                "port": 10085,
                "listen": "127.0.0.1",
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"}
            },
            {
                "tag": "proxy-in",
                "port": inbound_port,
                "listen": "0.0.0.0" if allow_lan else "127.0.0.1",
                "protocol": "mixed",
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls"],
                    "routeOnly": False
                },
                "settings": {
                    "auth": "noauth",
                    "udp": True
                }
            }
        ]

        outbounds = [
            outbound_proxy,
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
            {"tag": "api", "protocol": "freedom"}
        ]

        # Routing rules
        rules = [
            {
                "type": "field",
                "inboundTag": ["api"],
                "outboundTag": "api"
            }
        ]

        if routing_mode == "rule":
            # 规则分流：读用户规则文件 config/routing_rules.json，域名优先匹配，兜底走代理
            user_rules = self.load_routing_rules()
            if user_rules.get("屏蔽QUIC", True):
                rules.append({
                    "type": "field",
                    "port": "443",
                    "network": "udp",
                    "outboundTag": "block"
                })
            if user_rules.get("屏蔽域名"):
                rules.append({
                    "type": "field",
                    "outboundTag": "block",
                    "domain": user_rules["屏蔽域名"]
                })
            if user_rules.get("直连域名"):
                rules.append({
                    "type": "field",
                    "outboundTag": "direct",
                    "domain": user_rules["直连域名"]
                })
            if user_rules.get("代理域名"):
                rules.append({
                    "type": "field",
                    "outboundTag": "proxy",
                    "domain": user_rules["代理域名"]
                })
            if user_rules.get("直连IP"):
                rules.append({
                    "type": "field",
                    "outboundTag": "direct",
                    "ip": user_rules["直连IP"]
                })
            if user_rules.get("代理IP"):
                rules.append({
                    "type": "field",
                    "outboundTag": "proxy",
                    "ip": user_rules["代理IP"]
                })
            rules.append({
                "type": "field",
                "port": "0-65535",
                "outboundTag": "proxy"
            })
        elif routing_mode == "global":
            # 全局代理
            rules.extend([
                {
                    "type": "field",
                    "port": "443",
                    "network": "udp",
                    "outboundTag": "block"
                },
                {
                    "type": "field",
                    "outboundTag": "direct",
                    "ip": ["geoip:private"]
                },
                {
                    "type": "field",
                    "port": "0-65535",
                    "outboundTag": "proxy"
                }
            ])
        else: # direct
            rules.extend([
                {
                    "type": "field",
                    "port": "0-65535",
                    "outboundTag": "direct"
                }
            ])

        return {
            "log": {"loglevel": "warning"},
            "stats": {},
            "api": {
                "tag": "api",
                "services": ["StatsService"]
            },
            "policy": {
                "system": {
                    "statsInboundUplink": True,
                    "statsInboundDownlink": True,
                    "statsOutboundUplink": True,
                    "statsOutboundDownlink": True
                }
            },
            "dns": {
                "servers": [
                    "223.5.5.5",
                    "119.29.29.29",
                    "1.1.1.1",
                    "localhost"
                ]
            },
            "inbounds": inbounds,
            "outbounds": outbounds,
            "routing": {
                "domainStrategy": "AsIs",
                "rules": rules
            }
        }

    @staticmethod
    def _get_silent_flags():
        startupinfo = None
        creationflags = 0
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE
            creationflags = subprocess.CREATE_NO_WINDOW
        return startupinfo, creationflags

    @staticmethod
    def cleanup_zombie_cores(inbound_port: int = 10808):
        """Ensure no lingering xray or sing-box processes from crashed runs block ports"""
        if sys.platform != "win32":
            return
        try:
            for port in [inbound_port, 10085]:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    if s.connect_ex(('127.0.0.1', port)) == 0:
                        si, cf = XrayManager._get_silent_flags()
                        out = subprocess.check_output(f'netstat -ano | findstr :{port}', shell=True, text=True, startupinfo=si, creationflags=cf)
                        for line in out.strip().split('\n'):
                            parts = line.split()
                            if len(parts) >= 5 and "LISTENING" in parts:
                                pid = parts[-1]
                                subprocess.run(f'taskkill /F /PID {pid}', shell=True, capture_output=True, startupinfo=si, creationflags=cf)
        except Exception:
            pass

    def start_core(self, node: dict, routing_mode: str = "rule", inbound_port: int = 10808, allow_lan: bool = False) -> tuple[bool, str]:
        self.stop_core()
        self.cleanup_zombie_cores(inbound_port)

        cfg = self.generate_xray_config(node, routing_mode, inbound_port, allow_lan)
        with open(ACTIVE_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)

        si, cf = self._get_silent_flags()

        # Validate with xray -test
        env = os.environ.copy()
        env["xray.location.asset"] = CORE_DIR
        test_res = subprocess.run(
            [XRAY_EXE, "-test", "-c", ACTIVE_CONFIG_PATH],
            env=env,
            cwd=CORE_DIR,
            capture_output=True,
            text=True,
            startupinfo=si,
            creationflags=cf
        )
        if test_res.returncode != 0:
            return False, f"配置错误: {test_res.stderr or test_res.stdout}"

        self._xray_log_f = open(XRAY_LOG_PATH, "w", encoding="utf-8", errors="replace")
        self.process = subprocess.Popen(
            [XRAY_EXE, "run", "-c", ACTIVE_CONFIG_PATH],
            cwd=CORE_DIR,
            env=env,
            stdout=self._xray_log_f,
            stderr=subprocess.STDOUT,
            startupinfo=si,
            creationflags=cf
        )
        time.sleep(0.5)
        if self.process.poll() is not None:
            self._close_xray_log()
            return False, "Xray启动失败，日志：" + self.get_xray_log_tail()

        self.is_running = True
        self.last_uplink = 0
        self.last_downlink = 0
        self.last_stats_time = time.time()
        return True, "Xray启动成功"

    def stop_core(self):
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=1.5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
        self._close_xray_log()
        self.is_running = False
        self.uplink_speed = 0.0
        self.downlink_speed = 0.0

    def get_xray_log_tail(self, limit: int = 300) -> str:
        try:
            with open(XRAY_LOG_PATH, "rb") as f:
                return f.read()[-limit:].decode("utf-8", errors="replace").strip()
        except Exception:
            return "(无日志)"

    def _close_xray_log(self):
        f = getattr(self, "_xray_log_f", None)
        if f:
            try:
                f.close()
            except Exception:
                pass
        self._xray_log_f = None

    def update_traffic_stats(self) -> tuple[float, float]:
        """Returns (uplink_speed_Bps, downlink_speed_Bps)"""
        if not self.is_running or not self.process:
            self.uplink_speed = 0.0
            self.downlink_speed = 0.0
            return 0.0, 0.0

        try:
            si, cf = self._get_silent_flags()
            res = subprocess.run(
                [XRAY_EXE, "api", "statsquery", "--server=127.0.0.1:10085", "-pattern=inbound>>>proxy-in"],
                cwd=CORE_DIR,
                capture_output=True,
                text=True,
                timeout=1.0,
                startupinfo=si,
                creationflags=cf
            )
            if res.returncode == 0 and res.stdout:
                data = json.loads(res.stdout)
                now_up = 0
                now_down = 0
                for item in data.get("stat", []):
                    name = item.get("name", "")
                    val = int(item.get("value", 0))
                    if "traffic>>>uplink" in name:
                        now_up = val
                    elif "traffic>>>downlink" in name:
                        now_down = val

                now_time = time.time()
                dt = max(0.2, now_time - self.last_stats_time)
                
                if self.last_uplink > 0 or self.last_downlink > 0:
                    self.uplink_speed = max(0.0, (now_up - self.last_uplink) / dt)
                    self.downlink_speed = max(0.0, (now_down - self.last_downlink) / dt)
                else:
                    self.uplink_speed = 0.0
                    self.downlink_speed = 0.0

                self.last_uplink = now_up
                self.last_downlink = now_down
                self.last_stats_time = now_time
        except Exception:
            pass

        return self.uplink_speed, self.downlink_speed

    @staticmethod
    def is_local_proxy_alive(port: int = 10808) -> bool:
        """本地代理端口是否有人监听（xray 是否活着）"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                return s.connect_ex(('127.0.0.1', port)) == 0
        except Exception:
            return False

    @staticmethod
    def test_node_ping(server: str, port: int = 443, timeout: float = 2.0) -> int:
        """Test TCP ping to server in ms. Returns -1 if failed."""
        if not server:
            return -1
        try:
            t0 = time.time()
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((server, port))
            s.close()
            return int((time.time() - t0) * 1000)
        except Exception:
            return -1

    @staticmethod
    def test_real_http_ping(proxy_port: int = 10808, timeout: float = 3.5) -> int:
        """Test real HTTP 204 connectivity through local proxy port in ms. Returns -1 if failed."""
        try:
            t0 = time.time()
            proxies = {
                "http": f"http://127.0.0.1:{proxy_port}",
                "https": f"http://127.0.0.1:{proxy_port}"
            }
            r = requests.get("http://www.gstatic.com/generate_204", proxies=proxies, timeout=timeout)
            if r.status_code in [200, 204]:
                return int((time.time() - t0) * 1000)
            return -1
        except Exception:
            return -1

    def start_tun(self, inbound_port: int = 10808, server_ip: str = "") -> tuple[bool, str]:
        self.stop_tun()
        if not os.path.exists(SINGBOX_EXE):
            return False, "未找到 sing-box.exe"

        rules = [
            {
                "process_name": ["xray.exe", "sing-box.exe"],
                "outbound": "direct"
            }
        ]
        if server_ip and server_ip != "127.0.0.1":
            clean_ip = server_ip.split(":")[0].strip()
            if clean_ip.replace('.', '').isdigit():
                rules.insert(0, {
                    "ip_cidr": [f"{clean_ip}/32"],
                    "outbound": "direct"
                })

        tun_cfg = {
            "log": {"level": "warn"},
            "inbounds": [
                {
                    "type": "tun",
                    "tag": "tun-in",
                    "interface_name": "XrayVergeTun",
                    "address": ["172.19.0.1/30"],
                    "mtu": 1500,
                    "auto_route": True,
                    # strict_route 关闭：严格路由会拦截 WSL2/虚拟网卡的 NAT 转发流量
                    "strict_route": False,
                    "stack": "mixed"
                }
            ],
            "outbounds": [
                {
                    "type": "socks",
                    "tag": "proxy",
                    "server": "127.0.0.1",
                    "server_port": inbound_port
                },
                {
                    "type": "direct",
                    "tag": "direct"
                }
            ],
            "route": {
                "auto_detect_interface": True,
                "rules": rules
            }
        }
        with open(TUN_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(tun_cfg, f, indent=2)

        si, cf = self._get_silent_flags()
        env = os.environ.copy()
        env["PATH"] = CORE_DIR + ";" + env.get("PATH", "")
        last_err = ""
        for attempt in (1, 2):
            self.stop_tun()
            if not self._tun_adapter_clean:
                # 只在状态未知（程序刚启动/上次崩溃）时才动 PowerShell 清残骸，
                # 正常关闭过的循环重开跳过，省下 2~4 秒
                self._cleanup_phantom_tun(si, cf)
                self._tun_adapter_clean = True
            self._close_tun_log()
            self._tun_log_f = open(TUN_LOG_PATH, "w", encoding="utf-8", errors="replace")
            try:
                self.tun_process = subprocess.Popen(
                    [SINGBOX_EXE, "run", "-c", TUN_CONFIG_PATH],
                    cwd=CORE_DIR,
                    env=env,
                    stdout=self._tun_log_f,
                    stderr=subprocess.STDOUT,
                    startupinfo=si,
                    creationflags=cf
                )
                # 无管理员权限时 sing-box 会在 1~2 秒内退出并留下日志；
                # wintun 建网卡慢时可能要 5~15 秒，开 20 秒观察窗轮询
                tun_up = False
                for i in range(100):
                    time.sleep(0.2)
                    if self.tun_process.poll() is not None:
                        break
                    if i % 2 == 1:  # 每 0.4 秒查一次网卡是否出现
                        ipconfig_out = subprocess.check_output(
                            ["ipconfig"], text=True, errors="replace",
                            startupinfo=si, creationflags=cf
                        )
                        if "XrayVergeTun" in ipconfig_out:
                            tun_up = True
                            break
                if tun_up:
                    self.is_tun_running = True
                    return True, "TUN 虚拟网卡已启动 (全局生效)"
                exited = self.tun_process.poll() is not None
                self.stop_tun()
                last_err = ("sing-box 已退出，日志：" if exited else "TUN 网卡 20 秒内未创建，日志：") + self.get_tun_log_tail()
                time.sleep(0.3)
            except Exception as e:
                self.stop_tun()
                last_err = f"TUN 启动异常: {e}"
        return False, last_err

    def _cleanup_phantom_tun(self, si, cf):
        """强杀 sing-box 后 Wintun 网卡会滞留在 Disconnected 状态，
        残骸会拖死下一次 open interface（卡 10 秒超时），启动前先拔掉设备实例。
        只动非 Up 状态的，不碰正在使用的网卡。"""
        try:
            ps = ("Get-NetAdapter -IncludeHidden | Where-Object {$_.InterfaceDescription -like '*Wintun*' "
                  "-and $_.Status -ne 'Up'} | ForEach-Object { pnputil /remove-device $_.InstanceID | Out-Null }")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=15,
                           capture_output=True, startupinfo=si, creationflags=cf)
        except Exception:
            pass

    def stop_tun(self):
        if self.tun_process:
            try:
                self.tun_process.terminate()
                self.tun_process.wait(timeout=1.0)
            except Exception:
                try:
                    self.tun_process.kill()
                except Exception:
                    pass
            self.tun_process = None
        self._close_tun_log()
        self.is_tun_running = False
        # 强杀 sing-box 会留下幽灵网卡拖死下次开启，趁关闭时清掉（把耗时从"开"挪到"关"）
        si, cf = self._get_silent_flags()
        self._cleanup_phantom_tun(si, cf)
        self._tun_adapter_clean = True

    def get_tun_log_tail(self, limit: int = 300) -> str:
        try:
            with open(TUN_LOG_PATH, "rb") as f:
                return f.read()[-limit:].decode("utf-8", errors="replace").strip()
        except Exception:
            return "(无日志)"

    def _close_tun_log(self):
        f = getattr(self, "_tun_log_f", None)
        if f:
            try:
                f.close()
            except Exception:
                pass
        self._tun_log_f = None
