import os
import sys
import json
import time
import yaml
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QStackedWidget, QLineEdit, QFormLayout, QScrollArea,
    QMessageBox, QTextEdit, QButtonGroup, QSystemTrayIcon, QMenu, QAction,
    QComboBox, QGridLayout
)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QIcon, QColor
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
import atexit

from xray_manager import XrayManager, RULES_JSON_PATH
from system_proxy import set_system_proxy, get_system_proxy_status
from ui_components import SpeedWaveWidget, NodeCard, format_speed

class PingWorker(QThread):
    finished = pyqtSignal(str, int)

    def __init__(self, node_id: str, server: str, port: int, is_active: bool = False, inbound_port: int = 10808):
        super().__init__()
        self.node_id = node_id
        self.server = server
        self.port = port
        self.is_active = is_active
        self.inbound_port = inbound_port

    def run(self):
        if self.node_id == "DIRECT":
            ms = XrayManager.test_node_ping("223.5.5.5", 80)
            self.finished.emit(self.node_id, max(1, ms))
            return

        if self.is_active:
            # Active node: Test REAL HTTP 204 through proxy tunnel!
            ms = XrayManager.test_real_http_ping(self.inbound_port)
            if ms >= 0:
                self.finished.emit(self.node_id, ms)
                return
            self.finished.emit(self.node_id, -1)
        else:
            ms = XrayManager.test_node_ping(self.server, self.port)
            self.finished.emit(self.node_id, ms)

class StatsWorker(QThread):
    stats_updated = pyqtSignal(float, float)

    def __init__(self, mgr: XrayManager):
        super().__init__()
        self.mgr = mgr
        self.running = True

    def run(self):
        while self.running:
            try:
                if self.mgr.is_running:
                    up, down = self.mgr.update_traffic_stats()
                    self.stats_updated.emit(up, down)
                else:
                    self.stats_updated.emit(0.0, 0.0)
            except Exception:
                pass
            time.sleep(1.0)

    def stop(self):
        self.running = False
        self.quit()
        self.wait(500)

if getattr(sys, 'frozen', False):
    CURRENT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(CURRENT_DIR, "icon.png")

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Xray Verge")
        self.resize(960, 640)
        self.setMinimumSize(850, 560)
        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))

        self.mgr = XrayManager()
        self.data = self.mgr.load_nodes_data()
        self.active_node_id = self.data.get("active_node_id", "")
        self.editing_node_id = self.active_node_id
        self.routing_mode = self.data.get("routing_mode", "rule")
        self.inbound_port = int(self.data.get("inbound_port", 10808))
        self.allow_lan = bool(self.data.get("allow_lan", False))
        self.system_proxy_enabled = False
        self.tun_mode_enabled = False

        self.node_cards = {}
        self.ping_workers = []

        self.init_ui()
        self.setup_timer()
        self.setup_tray()

        # Initial core start
        self.apply_and_start_core()

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QHBoxLayout(main_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 1. Left Sidebar
        sidebar = QFrame()
        sidebar.setFixedWidth(220)
        sidebar.setStyleSheet("""
            QFrame {
                background-color: #ffffff;
                border-right: 1px solid #e2e8f0;
            }
        """)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 16, 12, 12)
        sidebar_layout.setSpacing(8)

        # Logo
        logo_layout = QHBoxLayout()
        logo_icon = QLabel("🐱")
        logo_icon.setFont(QFont("Segoe UI Emoji", 20))
        logo_text = QLabel("Xray Verge")
        logo_text.setFont(QFont("Segoe UI", 16, QFont.Bold))
        logo_text.setStyleSheet("color: #0f172a; border: none;")
        logo_layout.addWidget(logo_icon)
        logo_layout.addWidget(logo_text)
        logo_layout.addStretch()
        sidebar_layout.addLayout(logo_layout)
        sidebar_layout.addSpacing(16)

        # Nav Buttons
        self.nav_btn_group = QButtonGroup(self)
        self.nav_btn_group.setExclusive(True)

        self.btn_nav_proxies = self.create_nav_button("⚡  代理节点", 0)
        self.btn_nav_config = self.create_nav_button("⚙  节点配置", 1)
        self.btn_nav_logs = self.create_nav_button("📜  运行日志", 2)

        sidebar_layout.addWidget(self.btn_nav_proxies)
        sidebar_layout.addWidget(self.btn_nav_config)
        sidebar_layout.addWidget(self.btn_nav_logs)

        sidebar_layout.addStretch()

        # Bottom Traffic Wave Chart
        self.wave_widget = SpeedWaveWidget()
        sidebar_layout.addWidget(self.wave_widget)

        main_layout.addWidget(sidebar)

        # 2. Right Content Area
        content_container = QWidget()
        content_container.setStyleSheet("background-color: #f8fafc;")
        content_layout = QVBoxLayout(content_container)
        content_layout.setContentsMargins(24, 16, 24, 20)
        content_layout.setSpacing(16)

        # Top Bar
        top_bar = self.create_top_bar()
        content_layout.addLayout(top_bar)

        # Stacked Pages
        self.stack = QStackedWidget()
        self.page_proxies = self.create_proxies_page()
        self.page_config = self.create_config_page()
        self.page_logs = self.create_logs_page()

        self.stack.addWidget(self.page_proxies)
        self.stack.addWidget(self.page_config)
        self.stack.addWidget(self.page_logs)

        content_layout.addWidget(self.stack)

        main_layout.addWidget(content_container)

        self.btn_nav_proxies.setChecked(True)

    def create_nav_button(self, text: str, page_index: int) -> QPushButton:
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setFont(QFont("Segoe UI", 11, QFont.Medium))
        btn.setFixedHeight(42)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet("""
            QPushButton {
                text-align: left;
                padding-left: 14px;
                border-radius: 8px;
                border: none;
                color: #475569;
                background-color: transparent;
            }
            QPushButton:hover {
                background-color: #f1f5f9;
                color: #0f172a;
            }
            QPushButton:checked {
                background-color: #e0f2fe;
                color: #0284c7;
                font-weight: bold;
            }
        """)
        btn.clicked.connect(lambda: self.stack.setCurrentIndex(page_index))
        self.nav_btn_group.addButton(btn, page_index)
        return btn

    def create_top_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(12)

        self.page_title_label = QLabel("代理组 (PROXY)")
        self.page_title_label.setFont(QFont("Segoe UI", 16, QFont.Bold))
        self.page_title_label.setStyleSheet("color: #0f172a;")
        bar.addWidget(self.page_title_label)

        bar.addStretch()

        # Mode Buttons (Rule / Global / Direct)
        mode_frame = QFrame()
        mode_frame.setStyleSheet("""
            QFrame {
                background-color: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
            }
        """)
        mode_layout = QHBoxLayout(mode_frame)
        mode_layout.setContentsMargins(2, 2, 2, 2)
        mode_layout.setSpacing(2)

        self.mode_btn_group = QButtonGroup(self)
        self.btn_mode_rule = self.create_mode_button("规则", "rule")
        self.btn_mode_global = self.create_mode_button("全局", "global")
        self.btn_mode_direct = self.create_mode_button("直连", "direct")

        mode_layout.addWidget(self.btn_mode_rule)
        mode_layout.addWidget(self.btn_mode_global)
        mode_layout.addWidget(self.btn_mode_direct)

        if self.routing_mode == "global":
            self.btn_mode_global.setChecked(True)
        elif self.routing_mode == "direct":
            self.btn_mode_direct.setChecked(True)
        else:
            self.btn_mode_rule.setChecked(True)

        bar.addWidget(mode_frame)

        # Big System Proxy Toggle Switch
        self.btn_sys_proxy = QPushButton("⚪  系统代理：已关闭")
        self.btn_sys_proxy.setFixedHeight(36)
        self.btn_sys_proxy.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.btn_sys_proxy.setCursor(Qt.PointingHandCursor)
        self.btn_sys_proxy.clicked.connect(self.toggle_system_proxy)
        bar.addWidget(self.btn_sys_proxy)
        self.update_sys_proxy_button_style()

        # Big TUN Mode Toggle Switch
        self.btn_tun = QPushButton("⚪  TUN模式：已关闭")
        self.btn_tun.setFixedHeight(36)
        self.btn_tun.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.btn_tun.setCursor(Qt.PointingHandCursor)
        self.btn_tun.clicked.connect(self.toggle_tun_mode)
        bar.addWidget(self.btn_tun)
        self.update_tun_button_style()

        # Big Allow LAN Toggle Switch
        self.btn_allow_lan = QPushButton("⚪  局域网连接：已关闭")
        self.btn_allow_lan.setFixedHeight(36)
        self.btn_allow_lan.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.btn_allow_lan.setCursor(Qt.PointingHandCursor)
        self.btn_allow_lan.clicked.connect(self.toggle_allow_lan)
        bar.addWidget(self.btn_allow_lan)
        self.update_allow_lan_button_style()

        return bar

    def create_mode_button(self, text: str, mode_val: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setFixedSize(56, 30)
        btn.setFont(QFont("Segoe UI", 10))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet("""
            QPushButton {
                border: none;
                border-radius: 6px;
                color: #64748b;
                background-color: transparent;
            }
            QPushButton:hover {
                background-color: #f1f5f9;
            }
            QPushButton:checked {
                background-color: #0284c7;
                color: #ffffff;
                font-weight: bold;
            }
        """)
        btn.clicked.connect(lambda: self.set_routing_mode(mode_val))
        self.mode_btn_group.addButton(btn)
        return btn

    def set_routing_mode(self, mode: str):
        if self.routing_mode == mode:
            return
        self.routing_mode = mode
        self.data["routing_mode"] = mode
        self.mgr.save_nodes_data(self.data)
        self.log(f"切换分流模式为: {mode}")
        self.apply_and_start_core()

    def update_sys_proxy_button_style(self):
        if self.system_proxy_enabled:
            self.btn_sys_proxy.setText("🟢  系统代理：已开启")
            self.btn_sys_proxy.setStyleSheet("""
                QPushButton {
                    background-color: #10b981;
                    color: white;
                    border: none;
                    border-radius: 8px;
                    padding: 0 16px;
                }
                QPushButton:hover {
                    background-color: #059669;
                }
            """)
        else:
            self.btn_sys_proxy.setText("⚪  系统代理：已关闭")
            self.btn_sys_proxy.setStyleSheet("""
                QPushButton {
                    background-color: #ffffff;
                    color: #475569;
                    border: 1px solid #cbd5e1;
                    border-radius: 8px;
                    padding: 0 16px;
                }
                QPushButton:hover {
                    background-color: #f8fafc;
                    border-color: #94a3b8;
                }
            """)

    def update_allow_lan_button_style(self):
        if self.allow_lan:
            self.btn_allow_lan.setText("🟢  局域网连接：已开启")
            self.btn_allow_lan.setStyleSheet("""
                QPushButton {
                    background-color: #10b981;
                    color: white;
                    border: none;
                    border-radius: 8px;
                    padding: 0 16px;
                }
                QPushButton:hover {
                    background-color: #059669;
                }
            """)
        else:
            self.btn_allow_lan.setText("⚪  局域网连接：已关闭")
            self.btn_allow_lan.setStyleSheet("""
                QPushButton {
                    background-color: #ffffff;
                    color: #475569;
                    border: 1px solid #cbd5e1;
                    border-radius: 8px;
                    padding: 0 16px;
                }
                QPushButton:hover {
                    background-color: #f8fafc;
                    border-color: #94a3b8;
                }
            """)

    def toggle_allow_lan(self):
        self.allow_lan = not self.allow_lan
        self.data["allow_lan"] = self.allow_lan
        self.mgr.save_nodes_data(self.data)
        self.update_allow_lan_button_style()
        status_str = "已开启 (0.0.0.0:" + str(self.inbound_port) + ")" if self.allow_lan else "已关闭"
        self.log(f"局域网连接 {status_str}")
        # 监听地址变更需要重启核心生效
        self.apply_and_start_core()

    def toggle_system_proxy(self):
        new_state = not self.system_proxy_enabled
        if new_state and not XrayManager.is_local_proxy_alive(self.inbound_port):
            QMessageBox.warning(self, "无法开启", f"本地代理端口 127.0.0.1:{self.inbound_port} 没有服务在监听。\n请先选好节点让 Xray 核心运行起来，再开系统代理，否则会全系统断网！")
            return
        ok = set_system_proxy(new_state, "127.0.0.1", self.inbound_port)
        if ok:
            self.system_proxy_enabled = new_state
            self.data["system_proxy"] = new_state
            self.mgr.save_nodes_data(self.data)
            self.update_sys_proxy_button_style()
            status_str = "已开启 (127.0.0.1:" + str(self.inbound_port) + ")" if new_state else "已关闭"
            self.log(f"系统代理 {status_str}")
        else:
            QMessageBox.warning(self, "错误", "修改系统代理设置失败")

    def update_tun_button_style(self):
        if self.tun_mode_enabled:
            self.btn_tun.setText("🟢  TUN模式：已开启")
            self.btn_tun.setStyleSheet("""
                QPushButton {
                    background-color: #0284c7;
                    color: white;
                    border: none;
                    border-radius: 8px;
                    padding: 0 16px;
                }
                QPushButton:hover {
                    background-color: #0369a1;
                }
            """)
        else:
            self.btn_tun.setText("⚪  TUN模式：已关闭")
            self.btn_tun.setStyleSheet("""
                QPushButton {
                    background-color: #ffffff;
                    color: #475569;
                    border: 1px solid #cbd5e1;
                    border-radius: 8px;
                    padding: 0 16px;
                }
                QPushButton:hover {
                    background-color: #f8fafc;
                    border-color: #94a3b8;
                }
            """)

    def toggle_tun_mode(self):
        new_state = not self.tun_mode_enabled
        if new_state:
            if not XrayManager.is_local_proxy_alive(self.inbound_port):
                QMessageBox.warning(self, "无法开启 TUN", f"本地代理端口 127.0.0.1:{self.inbound_port} 没有服务在监听。\nTUN 会接管全局流量却无出口，导致全机断网！请先让 Xray 核心运行起来。")
                return
            active_node = self.get_active_node()
            server_ip = active_node.get("server", "").strip() if active_node else ""
            ok, msg = self.mgr.start_tun(self.inbound_port, server_ip)
            self.log(msg)
            if ok:
                self.tun_mode_enabled = True
                self.update_tun_button_style()
                QMessageBox.information(self, "TUN 模式已启用", "TUN 虚拟网卡已成功启动！\n系统全局流量（包含不支持代理设置的软件、游戏、命令行）已全部接管。")
            else:
                QMessageBox.warning(self, "TUN 启动失败", msg)
        else:
            self.mgr.stop_tun()
            self.tun_mode_enabled = False
            self.update_tun_button_style()
            self.log("TUN 虚拟网卡已关闭")

    # Page 1: Proxies Page
    def create_proxies_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # Warning banner if IP missing
        self.ip_warn_banner = QFrame()
        self.ip_warn_banner.setStyleSheet("""
            QFrame {
                background-color: #fef3c7;
                border: 1px solid #fde68a;
                border-radius: 8px;
                padding: 10px;
            }
        """)
        warn_layout = QHBoxLayout(self.ip_warn_banner)
        warn_layout.setContentsMargins(8, 4, 8, 4)
        warn_lbl = QLabel("⚠️ 提示：当前节点尚未填写真实服务器IP，请点击左侧【节点配置】填入您的正确IP并保存！")
        warn_lbl.setStyleSheet("color: #b45309; font-weight: bold; font-size: 12px; border: none; background: transparent;")
        btn_go_config = QPushButton("去配置")
        btn_go_config.setFixedSize(70, 26)
        btn_go_config.setStyleSheet("background-color: #f59e0b; color: white; border: none; border-radius: 4px; font-weight: bold;")
        btn_go_config.clicked.connect(lambda: self.btn_nav_config.setChecked(True))
        warn_layout.addWidget(warn_lbl)
        warn_layout.addStretch()
        warn_layout.addWidget(btn_go_config)
        layout.addWidget(self.ip_warn_banner)

        # Group Toolbar
        group_header = QHBoxLayout()
        group_header.setSpacing(8)

        lbl_group = QLabel("节点列表")
        lbl_group.setFont(QFont("Segoe UI", 12, QFont.Bold))
        lbl_group.setStyleSheet("color: #334155;")
        group_header.addWidget(lbl_group)

        lbl_badge = QLabel("Selector")
        lbl_badge.setStyleSheet("background-color: #e2e8f0; color: #475569; border-radius: 4px; padding: 2px 6px; font-size: 11px;")
        group_header.addWidget(lbl_badge)

        group_header.addStretch()

        btn_test_all = QPushButton("⚡  一键测速")
        btn_test_all.setFixedHeight(30)
        btn_test_all.setCursor(Qt.PointingHandCursor)
        btn_test_all.setStyleSheet("""
            QPushButton {
                background-color: #ffffff;
                color: #0284c7;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 0 12px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #f0f9ff;
                border-color: #0284c7;
            }
        """)
        btn_test_all.clicked.connect(self.ping_all_nodes)
        group_header.addWidget(btn_test_all)

        btn_restart = QPushButton("🔄  重启核心")
        btn_restart.setFixedHeight(30)
        btn_restart.setCursor(Qt.PointingHandCursor)
        btn_restart.setStyleSheet("""
            QPushButton {
                background-color: #ffffff;
                color: #475569;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 0 12px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #f1f5f9;
            }
        """)
        btn_restart.clicked.connect(self.apply_and_start_core)
        group_header.addWidget(btn_restart)

        layout.addLayout(group_header)

        # Node cards scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self.cards_container = QWidget()
        self.cards_layout = QGridLayout(self.cards_container)
        self.cards_layout.setContentsMargins(4, 4, 4, 16)
        self.cards_layout.setSpacing(12)
        self.cards_layout.setAlignment(Qt.AlignTop)

        self.rebuild_node_cards()

        scroll.setWidget(self.cards_container)
        layout.addWidget(scroll)

        return page

    def rebuild_node_cards(self):
        # Clear existing
        for i in reversed(range(self.cards_layout.count())):
            item = self.cards_layout.itemAt(i)
            if item and item.widget():
                item.widget().deleteLater()
        self.node_cards.clear()

        nodes = self.data.get("nodes", [])
        has_server_ip = False

        idx = 0
        for node in nodes:
            nid = node.get("id", "")
            if node.get("server", "").strip():
                has_server_ip = True
            is_active = (nid == self.active_node_id)
            card = NodeCard(node, is_selected=is_active)
            card.clicked.connect(self.on_node_selected)
            card.ping_requested.connect(self.on_ping_node)
            card.delete_requested.connect(self.on_delete_node)
            card.edit_requested.connect(self.on_edit_node)
            row = idx // 2
            col = idx % 2
            self.cards_layout.addWidget(card, row, col)
            self.node_cards[nid] = card
            idx += 1

        # Direct card
        direct_node = {"id": "DIRECT", "name": "DIRECT 直连", "protocol": "direct", "network": "", "security": ""}
        direct_card = NodeCard(direct_node, is_selected=(self.active_node_id == "DIRECT"))
        direct_card.clicked.connect(self.on_node_selected)
        direct_card.ping_requested.connect(self.on_ping_node)
        row = idx // 2
        col = idx % 2
        self.cards_layout.addWidget(direct_card, row, col)
        self.node_cards["DIRECT"] = direct_card

        # Update warning banner
        self.ip_warn_banner.setVisible(not has_server_ip)

    def on_delete_node(self, node_id: str):
        if node_id == "DIRECT":
            return
        node = self.get_node_by_id(node_id)
        name = node.get("name", node_id) if node else node_id
        ret = QMessageBox.question(
            self,
            "确认删除",
            f"确定要删除节点 [{name}] 吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if ret != QMessageBox.Yes:
            return

        nodes = self.data.get("nodes", [])
        self.data["nodes"] = [n for n in nodes if n.get("id") != node_id]

        if self.active_node_id == node_id:
            if self.data["nodes"]:
                self.active_node_id = self.data["nodes"][0]["id"]
            else:
                self.active_node_id = "DIRECT"
            self.data["active_node_id"] = self.active_node_id

        if getattr(self, "editing_node_id", None) == node_id:
            self.editing_node_id = self.active_node_id

        self.mgr.save_nodes_data(self.data)
        self.log(f"已删除节点: {name}")
        self.rebuild_node_cards()
        self.update_config_node_combo()
        self.apply_and_start_core()

    def on_edit_node(self, node_id: str):
        if node_id == "DIRECT":
            return
        self.btn_nav_config.setChecked(True)
        self.stack.setCurrentIndex(1)
        self.editing_node_id = node_id
        if hasattr(self, "combo_nodes"):
            idx = self.combo_nodes.findData(node_id)
            if idx >= 0:
                self.combo_nodes.setCurrentIndex(idx)
                return
        node = self.get_node_by_id(node_id)
        self.load_node_to_form(node)

    def on_node_selected(self, node_id: str):
        if self.active_node_id == node_id:
            return
        self.active_node_id = node_id
        self.data["active_node_id"] = node_id
        self.mgr.save_nodes_data(self.data)

        for nid, card in self.node_cards.items():
            card.set_selected(nid == node_id)

        self.log(f"已选中节点: {node_id}")
        self.apply_and_start_core()

    def on_ping_node(self, node_id: str):
        if node_id == "DIRECT":
            if node_id in self.node_cards:
                self.node_cards[node_id].set_ping(1)
            return

        node = self.get_node_by_id(node_id)
        if not node:
            return
        server = node.get("server", "").strip()
        port = int(node.get("port", 443))
        if not server:
            if node_id in self.node_cards:
                self.node_cards[node_id].set_ping(-1)
            self.log(f"测速失败: 节点 {node_id} 尚未填写服务器IP")
            return

        if node_id in self.node_cards:
            self.node_cards[node_id].set_ping(-3)

        self.ping_workers = [w for w in self.ping_workers if w.isRunning()]
        is_active = (node_id == self.active_node_id and self.mgr.is_running)
        worker = PingWorker(node_id, server, port, is_active=is_active, inbound_port=self.inbound_port)
        worker.finished.connect(self.on_ping_finished)
        self.ping_workers.append(worker)
        worker.start()

    def on_ping_finished(self, node_id: str, ms: int):
        if node_id in self.node_cards:
            self.node_cards[node_id].set_ping(ms)
        node = self.get_node_by_id(node_id)
        name = node.get("name", node_id) if node else node_id
        if ms >= 0:
            p_type = "真实 HTTP 204" if (node_id == self.active_node_id and self.mgr.is_running) else "TCP 握手"
            self.log(f"节点 [{name}] {p_type}延迟: {ms} ms")
        else:
            self.log(f"节点 [{name}] 测速超时")

    def ping_all_nodes(self):
        for card in self.node_cards.values():
            card.set_ping(-3)
        for nid in self.node_cards.keys():
            self.on_ping_node(nid)

    # Page 2: Config Page
    def create_config_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        # Node selector bar
        selector_frame = QFrame()
        selector_frame.setStyleSheet("""
            QFrame {
                background-color: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                padding: 6px 12px;
            }
        """)
        selector_layout = QHBoxLayout(selector_frame)
        selector_layout.setContentsMargins(4, 4, 4, 4)
        selector_layout.setSpacing(10)

        lbl_sel = QLabel("选择编辑节点:")
        lbl_sel.setFont(QFont("Segoe UI", 10, QFont.Bold))
        lbl_sel.setStyleSheet("color: #334155; border: none; background: transparent;")
        selector_layout.addWidget(lbl_sel)

        self.combo_nodes = QComboBox()
        self.combo_nodes.setFixedHeight(32)
        self.combo_nodes.setFont(QFont("Segoe UI", 10))
        self.combo_nodes.setMinimumWidth(260)
        self.combo_nodes.setStyleSheet("""
            QComboBox {
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 4px 10px;
                background-color: #f8fafc;
                color: #0f172a;
            }
            QComboBox:hover {
                border-color: #0284c7;
            }
            QComboBox::drop-down {
                border: none;
                width: 24px;
            }
        """)
        self.combo_nodes.currentIndexChanged.connect(self.on_config_combo_changed)
        selector_layout.addWidget(self.combo_nodes, stretch=1)

        btn_new_node = QPushButton("➕ 新建节点")
        btn_new_node.setFixedHeight(32)
        btn_new_node.setCursor(Qt.PointingHandCursor)
        btn_new_node.setStyleSheet("""
            QPushButton {
                background-color: #f1f5f9;
                color: #0284c7;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 0 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #e0f2fe;
            }
        """)
        btn_new_node.clicked.connect(self.on_new_node_clicked)
        selector_layout.addWidget(btn_new_node)

        layout.addWidget(selector_frame)

        # Form card
        form_card = QFrame()
        form_card.setStyleSheet("""
            QFrame {
                background-color: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 12px;
                padding: 16px;
            }
        """)
        form_layout = QFormLayout(form_card)
        form_layout.setSpacing(12)
        form_layout.setLabelAlignment(Qt.AlignRight)

        # Fields
        self.inp_name = QLineEdit()
        self.inp_server = QLineEdit()
        self.inp_server.setPlaceholderText("请输入你的真实服务器 IP 地址 (例如 203.0.113.10)")
        self.inp_server.setStyleSheet("font-weight: bold; color: #0284c7;")

        self.inp_port = QLineEdit("443")
        self.inp_uuid = QLineEdit()
        self.inp_sni = QLineEdit()
        self.inp_pbk = QLineEdit()
        self.inp_sid = QLineEdit()
        self.inp_spx = QLineEdit()
        self.inp_fp = QLineEdit("chrome")
        self.inp_inbound_port = QLineEdit(str(self.inbound_port))

        for inp in [self.inp_name, self.inp_server, self.inp_port, self.inp_uuid, self.inp_sni, self.inp_pbk, self.inp_sid, self.inp_spx, self.inp_fp, self.inp_inbound_port]:
            inp.setFixedHeight(34)
            inp.setFont(QFont("Segoe UI", 10))

        form_layout.addRow("节点名称:", self.inp_name)
        form_layout.addRow("⭐ 服务器 IP:", self.inp_server)
        form_layout.addRow("端口 (Port):", self.inp_port)
        form_layout.addRow("用户 UUID:", self.inp_uuid)
        form_layout.addRow("伪装域名 (SNI):", self.inp_sni)
        form_layout.addRow("Reality 公钥 (pbk):", self.inp_pbk)
        form_layout.addRow("Short ID (sid):", self.inp_sid)
        form_layout.addRow("SpiderX 路径:", self.inp_spx)
        form_layout.addRow("TLS 指纹 (fp):", self.inp_fp)
        form_layout.addRow("本地监听端口:", self.inp_inbound_port)

        layout.addWidget(form_card)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)

        btn_import_clip = QPushButton("📋  从剪贴板导入")
        btn_import_clip.setFixedHeight(38)
        btn_import_clip.setCursor(Qt.PointingHandCursor)
        btn_import_clip.setStyleSheet("""
            QPushButton {
                background-color: #f1f5f9;
                color: #334155;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                font-weight: bold;
                padding: 0 14px;
            }
            QPushButton:hover {
                background-color: #e2e8f0;
            }
        """)
        btn_import_clip.clicked.connect(self.import_from_clipboard)
        btn_layout.addWidget(btn_import_clip)

        btn_import_clash = QPushButton("📦  从 Clash Verge 导入")
        btn_import_clash.setFixedHeight(38)
        btn_import_clash.setCursor(Qt.PointingHandCursor)
        btn_import_clash.setStyleSheet("""
            QPushButton {
                background-color: #f1f5f9;
                color: #334155;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                font-weight: bold;
                padding: 0 14px;
            }
            QPushButton:hover {
                background-color: #e2e8f0;
            }
        """)
        btn_import_clash.clicked.connect(self.import_from_clash_verge)
        btn_layout.addWidget(btn_import_clash)

        btn_edit_rules = QPushButton("📜  编辑分流规则")
        btn_edit_rules.setFixedHeight(38)
        btn_edit_rules.setCursor(Qt.PointingHandCursor)
        btn_edit_rules.setStyleSheet("""
            QPushButton {
                background-color: #f1f5f9;
                color: #334155;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                font-weight: bold;
                padding: 0 14px;
            }
            QPushButton:hover {
                background-color: #e2e8f0;
            }
        """)
        btn_edit_rules.clicked.connect(self.edit_routing_rules)
        btn_layout.addWidget(btn_edit_rules)

        self.btn_delete_curr = QPushButton("🗑️  删除此节点")
        self.btn_delete_curr.setFixedHeight(38)
        self.btn_delete_curr.setCursor(Qt.PointingHandCursor)
        self.btn_delete_curr.setStyleSheet("""
            QPushButton {
                background-color: #fef2f2;
                color: #dc2626;
                border: 1px solid #fecaca;
                border-radius: 8px;
                font-weight: bold;
                padding: 0 14px;
            }
            QPushButton:hover {
                background-color: #fee2e2;
                border-color: #f87171;
            }
        """)
        self.btn_delete_curr.clicked.connect(self.on_delete_current_config_node)
        btn_layout.addWidget(self.btn_delete_curr)

        btn_layout.addStretch()

        btn_save = QPushButton("💾  保存并立即生效")
        btn_save.setFixedHeight(38)
        btn_save.setCursor(Qt.PointingHandCursor)
        btn_save.setStyleSheet("""
            QPushButton {
                background-color: #0284c7;
                color: white;
                border: none;
                border-radius: 8px;
                font-weight: bold;
                padding: 0 24px;
            }
            QPushButton:hover {
                background-color: #0369a1;
            }
        """)
        btn_save.clicked.connect(self.save_form_node)
        btn_layout.addWidget(btn_save)

        layout.addLayout(btn_layout)
        layout.addStretch()

        self.update_config_node_combo()

        return page

    def update_config_node_combo(self):
        if not hasattr(self, "combo_nodes"):
            return
        self.combo_nodes.blockSignals(True)
        self.combo_nodes.clear()
        nodes = self.data.get("nodes", [])
        for n in nodes:
            nid = n.get("id", "")
            name = n.get("name", nid)
            server = n.get("server", "")
            display = f"{name} ({server})" if server else f"{name} (未填IP)"
            self.combo_nodes.addItem(display, nid)

        target_id = getattr(self, "editing_node_id", None) or self.active_node_id
        idx = self.combo_nodes.findData(target_id)
        if idx >= 0:
            self.combo_nodes.setCurrentIndex(idx)
        elif self.combo_nodes.count() > 0:
            self.combo_nodes.setCurrentIndex(0)
        self.combo_nodes.blockSignals(False)
        self.on_config_combo_changed()

    def on_config_combo_changed(self):
        curr_id = self.combo_nodes.currentData()
        if not curr_id:
            self.load_node_to_form(None)
            self.btn_delete_curr.setEnabled(False)
            return
        self.editing_node_id = curr_id
        node = self.get_node_by_id(curr_id)
        self.load_node_to_form(node)
        self.btn_delete_curr.setEnabled(True)

    def on_new_node_clicked(self):
        self.editing_node_id = None
        self.combo_nodes.blockSignals(True)
        self.combo_nodes.setCurrentIndex(-1)
        self.combo_nodes.blockSignals(False)
        self.load_node_to_form(None)
        count = len(self.data.get("nodes", [])) + 1
        self.inp_name.setText(f"节点_{count}")
        self.btn_delete_curr.setEnabled(False)
        self.inp_server.setFocus()

    def on_delete_current_config_node(self):
        curr_id = self.combo_nodes.currentData()
        if not curr_id:
            return
        self.on_delete_node(curr_id)

    def load_node_to_form(self, node: dict):
        if not node:
            self.inp_name.clear()
            self.inp_server.clear()
            self.inp_port.setText("443")
            self.inp_uuid.clear()
            self.inp_sni.clear()
            self.inp_pbk.clear()
            self.inp_sid.clear()
            self.inp_spx.clear()
            self.inp_fp.setText("chrome")
            self.inp_inbound_port.setText(str(self.inbound_port))
            return

        self.editing_node_id = node.get("id", "")
        self.inp_name.setText(node.get("name", ""))
        self.inp_server.setText(node.get("server", ""))
        self.inp_port.setText(str(node.get("port", 443)))
        self.inp_uuid.setText(node.get("uuid", ""))
        self.inp_sni.setText(node.get("sni", ""))
        self.inp_pbk.setText(node.get("public_key", ""))
        self.inp_sid.setText(node.get("short_id", ""))
        self.inp_spx.setText(node.get("spider_x", ""))
        self.inp_fp.setText(node.get("fingerprint", "chrome"))
        self.inp_inbound_port.setText(str(self.inbound_port))

    def save_form_node(self):
        nodes = self.data.setdefault("nodes", [])
        node = None
        if getattr(self, "editing_node_id", None):
            node = self.get_node_by_id(self.editing_node_id)

        if not node:
            import uuid
            new_id = f"node_{uuid.uuid4().hex[:8]}"
            node = {"id": new_id, "protocol": "vless", "network": "tcp", "security": "reality"}
            nodes.append(node)
            self.editing_node_id = new_id
            if not self.active_node_id or self.active_node_id == "DIRECT":
                self.active_node_id = new_id
                self.data["active_node_id"] = new_id

        node["name"] = self.inp_name.text().strip() or "VLESS_Node"
        server_raw = self.inp_server.text().strip()
        if server_raw.startswith("http://"): server_raw = server_raw[7:]
        if server_raw.startswith("https://"): server_raw = server_raw[8:]
        if "/" in server_raw: server_raw = server_raw.split("/")[0]
        if ":" in server_raw and not server_raw.startswith("["):
            parts = server_raw.split(":")
            server_raw = parts[0]
            try:
                self.inp_port.setText(parts[1])
            except Exception:
                pass
        self.inp_server.setText(server_raw)
        node["server"] = server_raw

        try:
            node["port"] = int(self.inp_port.text().strip())
        except ValueError:
            node["port"] = 443
        node["uuid"] = self.inp_uuid.text().strip()
        node["sni"] = self.inp_sni.text().strip()
        node["public_key"] = self.inp_pbk.text().strip()
        node["short_id"] = self.inp_sid.text().strip()
        node["spider_x"] = self.inp_spx.text().strip()
        node["fingerprint"] = self.inp_fp.text().strip() or "chrome"

        try:
            self.inbound_port = int(self.inp_inbound_port.text().strip())
            self.data["inbound_port"] = self.inbound_port
        except ValueError:
            pass

        self.mgr.save_nodes_data(self.data)
        self.update_config_node_combo()
        self.rebuild_node_cards()
        self.apply_and_start_core()
        QMessageBox.information(self, "成功", f"节点 [{node['name']}] 配置已保存并已重新启动 Xray 核心！")

    def import_from_clipboard(self):
        clip = QApplication.clipboard().text().strip()
        if not clip.startswith("vless://"):
            QMessageBox.warning(self, "错误", "剪贴板内容不是有效的 vless:// 链接！")
            return
        try:
            node = self.mgr.parse_vless_url(clip)
            self.editing_node_id = None
            if hasattr(self, "combo_nodes"):
                self.combo_nodes.blockSignals(True)
                self.combo_nodes.setCurrentIndex(-1)
                self.combo_nodes.blockSignals(False)
            self.load_node_to_form(node)
            self.btn_delete_curr.setEnabled(False)
            QMessageBox.information(self, "成功", "已从剪贴板成功解析节点信息，请检查无误后点击【保存并立即生效】！")
        except Exception as e:
            QMessageBox.warning(self, "解析失败", f"无法解析链接: {e}")

    def edit_routing_rules(self):
        """用系统默认编辑器打开分流规则文件（首次自动生成默认规则）"""
        rules = self.mgr.load_routing_rules()
        self.mgr.save_routing_rules(rules)
        os.startfile(os.path.abspath(RULES_JSON_PATH))
        self.log("已打开分流规则文件，修改保存后点击【🔄 重启核心】生效")

    def import_from_clash_verge(self):
        from PyQt5.QtWidgets import QFileDialog
        clash_profile_path, _ = QFileDialog.getOpenFileName(
            self, "选择 Clash 配置文件 (*.yaml / *.yml)", "", "YAML 文件 (*.yaml *.yml);;所有文件 (*.*)")
        if not clash_profile_path:
            return
        try:
            with open(clash_profile_path, "r", encoding="utf-8") as f:
                clash_data = yaml.safe_load(f)
            count = 0
            existing_ids = {n["id"] for n in self.data.get("nodes", [])}
            for p in clash_data.get("proxies", []):
                nid = p.get("name")
                if nid and nid not in existing_ids:
                    ropts = p.get("reality-opts", {}) or {}
                    node = {
                        "id": nid,
                        "name": nid,
                        "protocol": p.get("type", "vless"),
                        "server": p.get("server", ""),
                        "port": int(p.get("port", 443)),
                        "uuid": p.get("uuid", ""),
                        "network": p.get("network", "tcp"),
                        "security": "reality" if p.get("reality-opts") else ("tls" if p.get("tls") else "none"),
                        "flow": p.get("flow", ""),
                        "sni": p.get("servername", ""),
                        "fingerprint": p.get("client-fingerprint", "chrome"),
                        "public_key": ropts.get("public-key", ""),
                        "short_id": ropts.get("short-id", ""),
                        "spider_x": ropts.get("spider-x", "")
                    }
                    self.data.setdefault("nodes", []).append(node)
                    existing_ids.add(nid)
                    count += 1
            self.mgr.save_nodes_data(self.data)
            self.update_config_node_combo()
            self.rebuild_node_cards()
            self.log(f"已成功从 Clash Verge 导入 {count} 个节点")
            QMessageBox.information(self, "导入成功", f"已成功从 Clash Verge 导入 {count} 个新节点！")
        except Exception as e:
            QMessageBox.warning(self, "导入失败", f"导入异常: {e}")

    # Page 3: Logs Page
    def create_logs_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        header = QHBoxLayout()
        lbl = QLabel("核心与系统日志")
        lbl.setFont(QFont("Segoe UI", 12, QFont.Bold))
        header.addWidget(lbl)
        header.addStretch()

        btn_clear = QPushButton("清空日志")
        btn_clear.setFixedHeight(28)
        btn_clear.clicked.connect(lambda: self.log_text.clear())
        header.addWidget(btn_clear)
        layout.addLayout(header)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 10))
        self.log_text.setStyleSheet("""
            QTextEdit {
                background-color: #0f172a;
                color: #f8fafc;
                border-radius: 8px;
                padding: 10px;
            }
        """)
        layout.addWidget(self.log_text)

        return page

    def log(self, text: str):
        now_str = time.strftime("%H:%M:%S")
        self.log_text.append(f"[{now_str}] {text}")

    # Core control
    def get_node_by_id(self, node_id: str):
        for n in self.data.get("nodes", []):
            if n.get("id") == node_id:
                return n
        return None

    def get_active_node(self):
        if not self.active_node_id:
            nodes = self.data.get("nodes", [])
            if nodes:
                self.active_node_id = nodes[0]["id"]
                return nodes[0]
            return None
        return self.get_node_by_id(self.active_node_id)

    def apply_and_start_core(self):
        if self.active_node_id == "DIRECT":
            direct_node = {"id": "DIRECT", "name": "DIRECT 直连", "protocol": "direct"}
            ok, msg = self.mgr.start_core(direct_node, "direct", self.inbound_port, allow_lan=self.allow_lan)
            self.log("已切换到 DIRECT 直连模式（不走代理，直接联网）")
            self.wave_widget.status_label.setText("⚙  当前为直连模式")
            return

        node = self.get_active_node()
        if not node:
            self.log("未配置任何代理节点")
            return

        server_ip = node.get("server", "").strip()
        if not server_ip:
            self.mgr.stop_core()
            self.log("⚠️ 节点尚未填写服务器IP，Xray 等待配置中")
            self.wave_widget.status_label.setText("⚠️ 等待填写服务器 IP")
            return

        ok, msg = self.mgr.start_core(node, self.routing_mode, self.inbound_port, allow_lan=self.allow_lan)
        self.log(msg)
        if ok:
            self.wave_widget.status_label.setText(f"⚙  Xray-core 运行中 ({self.routing_mode})")
        else:
            self.wave_widget.status_label.setText("❌ Xray 启动异常")

    def setup_timer(self):
        # Background worker for traffic stats query (zero UI freeze)
        self.stats_worker = StatsWorker(self.mgr)
        self.stats_worker.stats_updated.connect(self.on_stats_updated)
        self.stats_worker.start()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.on_timer_tick)
        self.timer.start(1000)

    def on_stats_updated(self, up: float, down: float):
        status_text = ""
        if self.mgr.is_running:
            status_text = f"⚙  Xray-core 运行中 ({self.routing_mode})"
        elif not self.get_active_node() or not self.get_active_node().get("server", "").strip():
            status_text = "⚠️ 等待填写服务器 IP"
        else:
            status_text = "⚪  Xray 未运行"

        self.wave_widget.add_data(up, down, status_text)

    def on_timer_tick(self):
        self.watch_xray_process()
        # Sync system proxy from OS (lightweight registry query)
        os_sys_proxy, _ = get_system_proxy_status()
        if os_sys_proxy != self.system_proxy_enabled:
            self.system_proxy_enabled = os_sys_proxy
            self.update_sys_proxy_button_style()
        self.watch_tun_process()

    def watch_xray_process(self):
        """xray 核心意外退出时修正状态；系统代理指向死端口会全系统断网，必须立刻自愈"""
        if self.mgr.is_running and self.mgr.process and self.mgr.process.poll() is not None:
            self.mgr.is_running = False
            self.log("⚠ Xray 核心意外退出：" + self.mgr.get_xray_log_tail())
            if self.tun_mode_enabled:
                self.log("⚠ TUN 仍接管全局流量但代理出口已死，全机网络中断！请重启核心或关闭 TUN")
        # 自愈：注册表残留系统代理（上次崩溃/强杀）且本地端口已无服务 → 自动关闭
        if self.system_proxy_enabled and not XrayManager.is_local_proxy_alive(self.inbound_port):
            if set_system_proxy(False):
                self.system_proxy_enabled = False
                self.update_sys_proxy_button_style()
                self.data["system_proxy"] = False
                self.log("检测到系统代理指向已失效的端口，已自动关闭系统代理（防断网自愈）")

    def watch_tun_process(self):
        """sing-box 中途退出时立刻恢复按钮状态，避免“绿灯但已断网”的假象"""
        if not self.tun_mode_enabled:
            return
        proc = self.mgr.tun_process
        if proc is not None and proc.poll() is not None:
            self.tun_mode_enabled = False
            self.mgr.stop_tun()
            self.update_tun_button_style()
            self.log("⚠ TUN 进程意外退出：" + self.mgr.get_tun_log_tail())

    def setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        if os.path.exists(ICON_PATH):
            self.tray.setIcon(QIcon(ICON_PATH))
        else:
            self.tray.setIcon(self.windowIcon())
        tray_menu = QMenu()

        act_show = QAction("显示主窗口", self)
        act_show.triggered.connect(self.show_and_activate)
        tray_menu.addAction(act_show)

        act_proxy = QAction("切换系统代理", self)
        act_proxy.triggered.connect(self.toggle_system_proxy)
        tray_menu.addAction(act_proxy)

        act_tun = QAction("切换 TUN 模式", self)
        act_tun.triggered.connect(self.toggle_tun_mode)
        tray_menu.addAction(act_tun)

        tray_menu.addSeparator()

        act_exit = QAction("退出 Xray Verge", self)
        act_exit.triggered.connect(self.clean_exit)
        tray_menu.addAction(act_exit)

        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

    def show_and_activate(self):
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
        self.activateWindow()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick or reason == QSystemTrayIcon.Trigger:
            self.show_and_activate()

    def closeEvent(self, event):
        self.hide()
        self.tray.showMessage(
            "Xray Verge",
            "已最小化到系统托盘，代理在后台保持连接。双击托盘图标即可恢复窗口。",
            QSystemTrayIcon.Information,
            2500
        )
        event.ignore()

    def clean_exit(self):
        # Clean shutdown
        if hasattr(self, 'stats_worker'):
            self.stats_worker.stop()
        self.mgr.stop_tun()
        set_system_proxy(False)
        self.mgr.stop_core()
        QApplication.quit()

if __name__ == "__main__":
    # Ensure system proxy is ALWAYS disabled if python terminates
    atexit.register(lambda: set_system_proxy(False))

    # Enable High DPI Crisp Rendering on 4K/scaling screens
    if hasattr(Qt, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Single-instance protection (brings existing instance to front)
    MUTEX_SERVER_NAME = "XrayVerge_SingleInstance_Mutex_Server"
    sock = QLocalSocket()
    sock.connectToServer(MUTEX_SERVER_NAME)
    if sock.waitForConnected(350):
        sock.write(b"WAKEUP")
        sock.waitForBytesWritten(350)
        sock.close()
        sys.exit(0)

    win = MainWindow()

    # Local server to wake up when user double-clicks shortcut again
    inst_server = QLocalServer()
    inst_server.removeServer(MUTEX_SERVER_NAME)
    if inst_server.listen(MUTEX_SERVER_NAME):
        def on_new_instance():
            client = inst_server.nextPendingConnection()
            if client:
                client.waitForReadyRead(400)
                msg = client.readAll().data()
                if b"WAKEUP" in msg:
                    win.show_and_activate()
                client.close()
        inst_server.newConnection.connect(on_new_instance)
        win._inst_server = inst_server

    app.aboutToQuit.connect(win.clean_exit)
    win.show()
    sys.exit(app.exec_())
