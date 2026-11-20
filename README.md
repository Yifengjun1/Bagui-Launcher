# 八桂启动器（Bagui Launcher）

一个面向中文玩家的 Minecraft 桌面启动器，基于 **PySide6** 构建，图标与背景全部由代码绘制，不依赖 HTML。支持微软正版登录、下载游戏与模组、整合包导入安装、独立启动等。

> **关于本开源版本**
> 本仓库是**开源副本**：仅保留 **Modrinth** 作为模组/整合包来源，**不含 CurseForge 相关代码与 API 密钥**，也不含任何第三方 API 凭据。所有需要密钥的能力均已剔除或改为空实现。

## 主要功能

- **游戏管理**：安装 / 删除游戏版本与整合包（删除时可选「移入回收站」或「彻底删除」）
- **微软正版登录**：设备码登录、令牌自动续期、正版皮肤显示
- **模组与整合包**：从 Modrinth 搜索 / 下载模组、光影、资源包、整合包；本地整合包（`.mrpack` 等）拖放安装
- **独立启动**：可脱离官方启动器直接启动游戏，支持 Java / 内存 / 版本隔离等设置
- **界面与体验**：简繁中文切换（跟随系统语言）、萌化悬浮球 / 托盘、磨砂卡片界面、壁纸与背景调色
- **启动器自更新**：「关于」页检查更新（夸克网盘 / GitHub 两种下载方式）；自动更新默认开启，后台静默下载新版本，关闭程序后安装、下次打开即最新版（更新包经 SHA256 校验）
- **使用指南**：安装包内含简 / 繁两种语言的 Word / PDF 指南

## 环境要求

- Python 3.12+（开发与打包使用 3.13）
- Windows 10/11（部分功能依赖 Windows API：注册表代理探测、回收站、窗口管理等）

依赖：

```bash
pip install PySide6 requests opencc-python-reimplemented PyInstaller
```

## 运行

```bash
python run.py
```

- `run.py` — 主程序
- `run_setup.py` — 安装向导打包入口
- `run_uninstall.py` — 卸载器打包入口

## 打包

```bash
python tools/build.py        # 完整打包（默认 --clean）
python tools/build.py --fast # 保留构建缓存的快速打包
python tools/check_payload.py # 校验安装向导内嵌载荷的版本 / 大小 / SHA256
```

打包会生成三个 exe：主程序、安装向导、卸载器。安装向导自带主程序与卸载器，并通过 SHA256 清单校验完整性。

## 目录结构

```
app/
  core/     # 核心逻辑：配置、登录、下载、启动、整合包、皮肤等
  ui/       # 界面：页面、弹窗、自绘控件、主题
  assets/   # 背景图、Logo、欢迎素材
  main.py   # 主程序入口
tools/      # 构建与校验脚本
guide/      # 使用指南（简 / 繁，docx / pdf）
```

## 数据存储

- 配置与账号：`%APPDATA%\bgl_datat_config\`（独立于游戏目录，重装不丢）
- 游戏与整合包：默认安装目录下的 `Osmanthus flower\`

## 许可

MIT License，详见 [LICENSE](LICENSE)。

## 说明

- 本项目只从 **Modrinth** 获取模组与整合包数据；任何需要 CurseForge API 密钥的功能均不在本开源版本内。
- 微软 / Mojang 账号体系归微软所有；本项目仅在本机保存登录凭据用于启动游戏。
- `ROADMAP.md` 记录尚未实现的功能计划，不代表已支持。
