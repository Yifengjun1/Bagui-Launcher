# -*- coding: utf-8 -*-
"""主页「Java 下载」展开面板：选主版本，下载官方 OpenJDK 并自动装进系统 Java 目录。

纯 Java 运行环境，不含任何游戏内容。装完会写 JAVA_HOME / PATH，
官方启动器、整合包、模组加载器安装器都能直接用上。
"""
import os

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QVBoxLayout,
                               QWidget)

from ..core import javamanager
from ..core.asyncio_qt import run_async
from ..core.tasks import TASKS
from . import theme
from .dialogs import ProgressDailog
from .loaderpanel import COMBO_QSS
from .widgets import FrostCard, PillButton, PopupPanel, Toast


class JavaPopup(PopupPanel):
    def __init__(self, parent, anchor):
        super().__init__(parent, anchor)
        self._busy = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = FrostCard(self, radius=16, tint=246)
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(9)

        t = QLabel("下载 Java 运行环境")
        f = QFont(theme.FONT)
        f.setPixelSize(14)
        f.setBold(True)
        t.setFont(f)
        lay.addWidget(t)

        sub = QLabel("官方 OpenJDK，装完自动配置 JAVA_HOME / PATH，"
                     "官方启动器与整合包直接可用。")
        sub.setStyleSheet("color:#5a6675;font-size:11px;")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        self.lb_now = QLabel("")
        self.lb_now.setStyleSheet("color:#2e9e5b;font-size:11px;")
        self.lb_now.setWordWrap(True)
        lay.addWidget(self.lb_now)
        self._refresh_now()

        h = QHBoxLayout()
        h.setSpacing(8)
        lb = QLabel("Java 版本")
        lb.setFixedWidth(58)
        lb.setStyleSheet("color:#4c5866;font-size:12px;")
        h.addWidget(lb)
        self.cb_major = QComboBox()
        self.cb_major.setStyleSheet(COMBO_QSS)
        self.cb_major.setFixedHeight(28)
        for maj, label in javamanager.MAJORS:
            self.cb_major.addItem(label, maj)
        # 换版本时把「哪个 MC 版本要它」写出来（下拉里的字放不下这么多）
        self.cb_major.currentIndexChanged.connect(lambda _i: self._refresh_dst())
        h.addWidget(self.cb_major, 1)
        lay.addLayout(h)

        self.lb_dst = QLabel("")
        self.lb_dst.setStyleSheet("color:#8a95a3;font-size:11px;")
        self.lb_dst.setWordWrap(True)
        # Popup 里带 wordWrap 的 QLabel 定不出高度（会跟下一行叠起来），
        # 先给死宽度，heightForWidth 才有意义 —— 同 loaderpanel 的写法。
        self.lb_dst.setFixedWidth(344)
        lay.addWidget(self.lb_dst)

        self.tip = QLabel("")
        self.tip.setStyleSheet("color:#8a95a3;font-size:11px;")
        self.tip.setWordWrap(True)
        self.tip.setFixedWidth(344)
        # 提示会随着选的版本变一到两行，高度定住面板才不会跳
        self.tip.setMinimumHeight(48)
        lay.addWidget(self.tip)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addStretch(1)
        cancel = PillButton("关闭", "ghost")
        cancel.setFixedHeight(30)
        cancel.setMinimumWidth(64)
        cancel.clicked.connect(self.close)
        btns.addWidget(cancel)
        self.btn_go = PillButton("开始下载", "accent")
        self.btn_go.setFixedHeight(30)
        self.btn_go.setMinimumWidth(88)
        self.btn_go.clicked.connect(self._start)
        btns.addWidget(self.btn_go)
        lay.addLayout(btns)

        self.setFixedWidth(380)
        self._refresh_dst()
        self.adjustSize()

    # ---------------- 状态 ----------------
    def _refresh_now(self):
        path, major, ver = javamanager.current_java()
        if path:
            self.lb_now.setText(f"✓ 本机已有 Java {major}"
                                f"{('（' + ver + '）') if ver else ''}\n{path}")
        else:
            self.lb_now.setStyleSheet("color:#c8811a;font-size:11px;")
            self.lb_now.setText("○ 未检测到 Java，Forge/NeoForge 安装器与游戏都需要它")

    def _refresh_dst(self):
        root = javamanager.install_root()
        self.lb_dst.setText(f"安装位置：{root}")
        # 第四十三轮起软件本身不再要求管理员：Java 默认就装用户目录、
        # JAVA_HOME/PATH 写 HKCU。这句只是把「不动系统环境」说清楚，
        # 不能写成「未以管理员运行」那种像少了权限的口气。
        maj = int(self.cb_major.currentData() or 21)
        need = javamanager.MAJOR_NOTE.get(maj, "")
        self.tip.setText((f"{need}\n" if need else "")
                         + "Java 装在用户目录、JAVA_HOME 只对当前用户生效 —— "
                           "不需要管理员权限，也不会改系统环境变量。")
        if self.isVisible():
            self.adjustSize()

    # ---------------- 下载 ----------------
    def _start(self):
        if self._busy:
            return
        major = int(self.cb_major.currentData() or 21)
        self._busy = True
        self.btn_go.setEnabled(False)
        self.btn_go.setText("下载中…")
        self.close()

        task = TASKS.create(f"Java {major} 运行环境", "java")
        dlg = ProgressDailog(self.parent(), title=f"下载 Java {major}")

        def finished(t):
            self._busy = False
            self.btn_go.setEnabled(True)
            self.btn_go.setText("开始下载")
            self._refresh_now()
            if t.status == "done":
                Toast.show_toast(self.parent(), t.message or f"Java {major} 已安装",
                                 ms=6000, kind="success")
            elif t.status == "failed":
                Toast.show_toast(self.parent(), t.message or "Java 安装失败",
                                 ms=6500, kind="error")

        dlg.bind(task, on_finished=finished)
        dlg.show()

        def work():
            return javamanager.install(major, task.tid)

        def done(res):
            ok, info = res if isinstance(res, tuple) else (False, str(res))
            TASKS.finish(task.tid, "done" if ok else "failed", info)

        def err(e):
            msg = str(e)
            if "已取消" in msg:
                TASKS.finish(task.tid, "cancelled", "已取消")
            else:
                TASKS.finish(task.tid, "failed", msg[:150])

        run_async(work, on_done=done, on_err=err)

    # ---------------- 定位 ----------------
class JavaButton(PillButton):
    """主页「模组加载器」右侧的 Java 下载按钮"""

    def __init__(self, window, parent=None):
        super().__init__("Java 下载", "ghost", icon="download", parent=parent)
        self.window_ref = window
        self._popup = None
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("下载并安装 Java 运行环境（21 / 17 / 8）")
        self.clicked.connect(self._toggle)

    def _toggle(self):
        if self._popup is not None and self._popup.isVisible():
            self._popup.close()
            return
        self._popup = JavaPopup(self.window_ref, self)
        self._popup.popup()
