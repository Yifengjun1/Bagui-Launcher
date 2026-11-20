# -*- coding: utf-8 -*-
"""把整合包拖进窗口就能装（第六十二轮）。

为什么不能只看扩展名：.mrpack 是整合包，但**绝大多数整合包也是 .zip**
（别人打包好的整个游戏目录）。反过来，模组 jar 改名成 zip、
材质包 zip、光影 zip 也都是 .zip。所以真正要认的是**里面有什么** ——
判断逻辑在 `installer.pack_kind()`，这里只负责问一句「装吗」然后交给下载页
那条现成的安装流程（进度窗 / 内存分配 / 结果弹窗全都复用）。

对外：
    handle_drop(window, mime) -> bool      # 处理一次拖放，返回「接住了没有」
    accepted(paths) -> str                 # 拖进来的东西里第一个能处理的文件
    ask(parent, info) -> bool              # 确认窗
"""
import os

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QLabel

from ..core import installer
from . import theme
from .dialogs import _DialogBase
from .widgets import ElideLabel, PillButton, Toast

FORMAT_CN = {"mrpack": "Modrinth 整合包（.mrpack）",
             "instance": "其它启动器导出的整合包实例",
             "raw": "打包好的游戏目录（mods + 配置）"}
KIND_CN = {"mod": "模组", "resourcepack": "材质包", "shader": "光影",
           "datapack": "数据包"}


def _first_file(mime):
    """拖放数据里第一个本地文件（没有就空串）"""
    try:
        for u in (mime.urls() or []):
            if u.isLocalFile():
                p = u.toLocalFile()
                if p and os.path.isfile(p):
                    return p
    except Exception:                                  # noqa: BLE001
        pass
    return ""


def accepted(path: str) -> bool:
    """这个文件值不值得接住（先粗筛扩展名，别为了一张图片去解压）"""
    low = str(path or "").lower()
    return low.endswith((".zip", ".mrpack", ".jar"))


def ask(parent, info: dict) -> bool:
    """「要装这个整合包吗？」—— 名称 + 识别结果 + 版本 + 路径"""
    d = _DialogBase(parent, width=520, title="拖进来一个整合包")
    name = str(info.get("name") or "未命名整合包")
    lb = QLabel(name)
    f = QFont(theme.FONT)
    f.setPixelSize(15)
    f.setBold(True)
    lb.setFont(f)
    lb.setWordWrap(True)
    lb.setStyleSheet("color:#1f2733;background:transparent;")
    d.body.addWidget(lb)

    fmt = FORMAT_CN.get(str(info.get("format") or ""), "整合包")
    bits = [fmt]
    mc = str(info.get("mc") or "")
    loader = str(info.get("loader") or "")
    lver = str(info.get("loader_ver") or "")
    if mc:
        bits.append("MC " + mc)
    if loader:
        cn = {"forge": "Forge", "neoforge": "NeoForge", "fabric": "Fabric",
              "quilt": "Quilt"}.get(loader, loader)
        bits.append(cn + (" " + lver if lver else ""))
    who = QLabel("识别为：" + " · ".join(bits))
    who.setWordWrap(True)
    who.setStyleSheet("color:#2f7cf6;font-size:12px;background:transparent;")
    d.body.addWidget(who)

    if not mc:
        warn = QLabel("没读到游戏版本 —— 装的时候会按 mods 里的文件名猜，"
                      "猜不出来才需要你手动备份。")
        warn.setWordWrap(True)
        warn.setStyleSheet("color:#8a6d3b;font-size:11px;background:transparent;")
        d.body.addWidget(warn)

    path = str(info.get("path") or "")
    row = QHBoxLayout()
    row.setSpacing(6)
    lb_p = ElideLabel(path)
    lb_p.setToolTip(path)
    lb_p.setStyleSheet("color:#7b8794;font-size:11px;background:transparent;")
    row.addWidget(lb_p, 1)
    d.body.addLayout(row)

    tip = QLabel("会新建一个独立的游戏实例（在「游戏管理」里），"
                 "原来的游戏、存档、模组都不动。")
    tip.setWordWrap(True)
    tip.setStyleSheet("color:#4c5866;font-size:11.5px;background:transparent;")
    d.body.addWidget(tip)

    box = {"ok": False}
    row2 = QHBoxLayout()
    row2.addStretch(1)
    row2.setSpacing(10)
    b_no = PillButton("取消", "ghost")
    b_no.setFixedHeight(36)
    b_no.setMinimumWidth(92)
    b_no.clicked.connect(d.reject)
    row2.addWidget(b_no)
    b_ok = PillButton("安装", "accent")
    b_ok.setFixedHeight(36)
    b_ok.setMinimumWidth(92)

    def go():
        box["ok"] = True
        d.accept()

    b_ok.clicked.connect(go)
    row2.addWidget(b_ok)
    d.body.addLayout(row2)
    d.exec()
    return box["ok"]


def handle_drop(window, mime) -> bool:
    """窗口收到拖放 → 认一认 → 问一句 → 交给下载页安装。

    返回 True 表示「这个拖放我们处理了」（调用方据此 accept 事件）。
    """
    path = _first_file(mime)
    if not path or not accepted(path):
        return False
    try:
        info = installer.pack_kind(path)
    except Exception as e:                             # noqa: BLE001
        Toast.show_toast(window, f"读不出这个文件：{str(e)[:60]}", kind="error")
        return True
    info["path"] = path
    kind = str(info.get("kind") or "")
    if kind != "modpack":
        cn = KIND_CN.get(kind)
        if cn:
            Toast.show_toast(
                window,
                f"这是{cn}，不是整合包 —— {info.get('why') or ''}"
                "（模组/材质包请到对应下载页安装）"[:90],
                ms=5200, kind="error")
        else:
            Toast.show_toast(window, (info.get("why") or "这个文件不是整合包")[:90],
                             ms=5200, kind="error")
        return True
    if not ask(window, info):
        return True
    page = getattr(window, "page_modpack", None)
    runner = getattr(page, "install_local_pack", None)
    if runner is None:
        Toast.show_toast(window, "整合包页面还没准备好，请稍后再拖一次", kind="error")
        return True
    runner(path)
    return True
