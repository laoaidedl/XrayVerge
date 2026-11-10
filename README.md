# 🐱 XrayVerge

一款**图形化的 Xray 客户端**，专为**用惯了 Clash 系界面**、却用不惯其他 Xray 客户端的用户打造——界面布局与 Clash Verge 一脉相承，上手零成本：左侧导航、节点卡片、一键测速、实时流量波形图，熟悉的味道。

底层是**真 Xray 内核**（VLESS + REALITY），TLS 指纹标准，不会因客户端指纹不标准连累服务端 IP 被针对。纯 PyQt5 实现，界面全中文，功能只留最常用的那几个按钮。

## ✨ 特性

- **VLESS + REALITY**：原生 Xray 内核，TLS 指纹标准，不会因客户端指纹不标准连累服务端 IP
- **vless:// 一键导入**：复制节点链接 → 从剪贴板导入 → 保存生效，三步完事
- **系统代理 / TUN 全局接管**：TUN 模式可接管所有软件流量（含不支持代理设置的程序、命令行、游戏），需以管理员身份运行
- **可视化管理**：节点卡片、真实 HTTP 204 测速、实时流量波形图、运行日志
- **可自定义分流规则**：`config/routing_rules.json` 一个文件管分流（中文键名），改完点「重启核心」即生效
- **三种模式**：规则分流 / 全局代理 / 直连，一键切换
- **崩溃自愈**：系统代理指向失效端口时自动关闭，防止全系统断网；节点数据原子写入，断电不丢

## 📦 目录结构

```
XrayVerge/
├── main_window.py      # 主窗口与交互逻辑
├── xray_manager.py     # 内核进程管理 / 配置生成 / 规则加载
├── system_proxy.py     # Windows 系统代理开关
├── ui_components.py    # 流量波形图、节点卡片等自定义控件
├── core/               # 内核与数据文件（需自行放入，见下）
│   ├── xray.exe        # Xray-core
│   ├── sing-box.exe    # TUN 模式用
│   ├── wintun.dll      # TUN 虚拟网卡驱动
│   ├── geoip.dat / geosite.dat
│   └── ...
└── config/             # 运行时自动生成（节点、分流规则）
```

> ⚠️ 出于体积与许可考虑，本仓库**不含** `core/` 内的二进制。请自行下载 [Xray-core](https://github.com/XTLS/Xray-core) 与 [sing-box](https://github.com/SagerNet/sing-box)，连同 `wintun.dll`、`geoip.dat`、`geosite.dat` 放入 `core/` 目录。

## 🚀 从源码运行

```bash
# 依赖：Python 3.10+，PyQt5，requests，pyyaml
pip install PyQt5 requests pyyaml
python main_window.py
```

## 🛠️ 打包

```bash
pip install pyinstaller
pyinstaller XrayVerge.spec --noconfirm
```

打包产物在 `dist/XrayVerge/`。TUN 模式需要管理员权限，spec 中已配置 `uac_admin=True`。

## 📜 自定义分流规则

首次以「规则」模式启动后会生成 `config/routing_rules.json`：

```json
{
  "屏蔽域名": ["geosite:category-ads-all"],
  "直连域名": ["geosite:private", "geosite:cn", "geosite:apple"],
  "代理域名": ["geosite:geolocation-!cn", "geosite:google", "geosite:gfw",
              "geosite:telegram", "geosite:twitter", "geosite:youtube", "geosite:github"],
  "直连IP": ["geoip:private", "geoip:cn"],
  "代理IP": ["geoip:telegram"],
  "屏蔽QUIC": true
}
```

- 支持 `geosite:xxx` / `geoip:xxx` / `domain:example.com` / 纯 IP 段
- 修改保存后，点击界面上的「🔄 重启核心」即生效
- 界面【节点配置】页有「📜 编辑分流规则」按钮可直接打开该文件

## ⚠️ 免责声明

本项目仅供学习与研究网络技术使用，请遵守所在地区的法律法规，勿用于任何违法用途。
