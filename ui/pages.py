# -*- coding: utf-8 -*-
"""侧栏（3D 角色卡 + 官启 + 设置）与主页"""
import os

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..core import installer
from ..core.asyncio_qt import run_async
from ..core.session import current_session, fetch_avatar
from . import theme
from .dialogs import LoginDialog
from .skinview import SkinView
from .widgets import (AsyncImageView, FlowLayout, FrostCard, IconButton,
                      PillButton, Toast, launch_official_async)


class CharCard(FrostCard):
    """左侧角色卡：皮肤 3D 渲染（浮动动画）+ 名称，点击登录"""

    def __init__(self, window, parent=None):
        super().__init__(parent, radius=18, tint=120)
        self.window_ref = window
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 18, 16, 16)
        lay.setSpacing(10)

        self.img = SkinView(radius=12, placeholder="")
        self.img.setMinimumHeight(330)
        lay.addWidget(self.img, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.name = QLabel("未登录")
        f = QFont(theme.FONT)
        f.setPixelSize(15)
        f.setBold(True)
        self.name.setFont(f)
        row.addWidget(self.name)
        row.addStretch(1)
        self.logout_btn = IconButton("logout", 30, 14)
        self.logout_btn.setToolTip("退出登录")
        self.logout_btn.clicked.connect(self._logout)
        row.addWidget(self.logout_btn)
        lay.addLayout(row)

        self.uuid_label = QLabel("登录后显示皮肤信息")
        self.uuid_label.setProperty("dim", True)
        self.uuid_label.setStyleSheet("color:#5a6675;font-size:11px;")
        lay.addWidget(self.uuid_label)

        self.tip = QLabel("点击卡片登录微软账户")
        self.tip.setStyleSheet("color:#2f7cf6;font-size:12px;font-weight:600;")
        lay.addWidget(self.tip)

        self.refresh()

    def mousePressEvent(self, e):
        if not current_session():
            d = LoginDialog(self.window_ref)
            d.logged_in.connect(lambda s: self.refresh())
            d.exec()
        else:
            # 已登录：点击查看/刷新
            self.refresh()

    def _logout(self):
        from ..core.session import logout
        logout()
        self.refresh()
        self.window_ref.apply_session()
        Toast.show_toast(self.window_ref, "已退出登录")

    def refresh(self):
        s = current_session()
        if s:
            mode_txt = {"microsoft": "微软账户 · 已登录",
                        "thirdparty": "第三方登录 · " + (s.get("server_name") or "外置站点"),
                        "online": "正版账户 · 已登录"}.get(s.get("mode"), "离线模式 · 未验证")
            self.name.setText(s.get("name", ""))
            self.uuid_label.setText(s.get("uuid", ""))
            self.tip.setText(mode_txt)
            self.img.load_skin(s.get("name"), s.get("uuid"), s.get("skin_url"))
            self.logout_btn.show()
        else:
            self.name.setText("未登录")
            self.uuid_label.setText("登录后显示皮肤信息")
            self.tip.setText("点击卡片登录微软账户")
            self.img.clear()
            self.logout_btn.hide()


class Sidebar(QWidget):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window_ref = window
        self.setFixedWidth(300)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        self.char_card = CharCard(window)
        lay.addWidget(self.char_card, 1)

        # 「官启」按钮：右侧小三角可在「官方启动器 / 独立启动」之间切
        #（设置里没开独立启动时，三角自动不显示 —— 见 launchbtn.refresh）
        from .launchbtn import LaunchSplitButton
        self.launch_btn = LaunchSplitButton(
            window, official_text="官  启", empty_text="选择游戏启动", height=46)
        lay.addWidget(self.launch_btn)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        btn_settings = PillButton("设置", "ghost", icon="gear")
        btn_settings.setFixedHeight(40)
        btn_settings.clicked.connect(window.open_settings)
        bottom.addWidget(btn_settings, 1)
        btn_dir = IconButton("folder", 40, 16)
        btn_dir.setToolTip("打开游戏目录")
        btn_dir.clicked.connect(self._open_game_dir)
        bottom.addWidget(btn_dir)
        lay.addLayout(bottom)

    def _open_game_dir(self):
        d = installer.detect_game_dir()
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)  # noqa: S606
        except Exception:
            Toast.show_toast(self.window_ref, "无法打开游戏目录")


class HomePage(QWidget):
    """主页：欢迎卡 + 3D 角色 + 快捷操作"""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window_ref = window
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.card = FrostCard(radius=20, tint=120)
        lay.addWidget(self.card)
        self.refresh()

    def refresh(self):
        # 重建卡片内容：整体替换
        lay = self.layout()
        idx = lay.indexOf(self.card)
        if idx >= 0:
            lay.removeWidget(self.card)
        # 旧卡片已从布局摘掉，但 parent 还是本页 → 不 hide 会留在原位当「幽灵」
        # （deleteLater 要等事件循环回到空闲才真正销毁，这期间会多画一层）
        self.card.hide()
        self.card.deleteLater()
        self.card = FrostCard(radius=20, tint=120)
        lay.addWidget(self.card)
        outer = QVBoxLayout(self.card)
        outer.setContentsMargins(40, 36, 40, 32)
        outer.setSpacing(18)

        s = current_session()
        f = QFont(theme.FONT)
        f.setPixelSize(22)
        f.setBold(True)
        title = QLabel(f"欢迎回来，{s['name']}" if s else "欢迎来到 MC 官启辅助工具")
        title.setFont(f)
        outer.addWidget(title)

        sub = QLabel("头像与 3D 角色来自你在官方上传的皮肤。下载的整合包 / 模组 / 光影 / 材质包\n都会自动安排到微软官方启动器，无需繁琐操作。")
        sub.setStyleSheet("color:#4c5866;font-size:12px;")
        outer.addWidget(sub)

        mid = QHBoxLayout()
        mid.setSpacing(28)
        img = SkinView(radius=14, placeholder="")
        img.setMinimumSize(210, 300)
        if s:
            img.load_skin(s.get("name"), s.get("uuid"), s.get("skin_url"))
        mid.addWidget(img)

        side = QVBoxLayout()
        side.setSpacing(12)
        if s:
            box = FrostCard(radius=14, tint=200)
            box.setFixedHeight(96)
            bl = QHBoxLayout(box)
            bl.setContentsMargins(16, 12, 16, 12)
            av = AsyncImageView(radius=10, placeholder="")
            av.setFixedSize(64, 64)

            def avdone(image):
                if image:
                    av.setImage(image)
            run_async(fetch_avatar, s.get("name"), s.get("uuid"), s.get("skin_url"),
                      size=128, on_done=avdone)
            bl.addWidget(av)
            meta = QVBoxLayout()
            meta.setSpacing(3)
            nm = QLabel(s["name"])
            nf = QFont(theme.FONT)
            nf.setPixelSize(16)
            nf.setBold(True)
            nm.setFont(nf)
            meta.addWidget(nm)
            uid = QLabel(s.get("uuid", ""))
            uid.setStyleSheet("color:#6a7482;font-size:11px;")
            meta.addWidget(uid)
            mode = QLabel({"microsoft": "微软账户", "online": "正版账户",
                           "thirdparty": "第三方登录"}.get(
                s.get("mode"), "离线模式"))
            mode.setStyleSheet("color:#2f7cf6;font-size:11px;font-weight:600;")
            meta.addWidget(mode)
            bl.addLayout(meta, 1)
            side.addWidget(box)
        else:
            tip = QLabel("登录后，这里会显示你的头像与官方皮肤 3D 模型。")
            tip.setStyleSheet("color:#4c5866;font-size:13px;")
            side.addWidget(tip)
            login_btn = PillButton("登录微软账户", "accent")
            login_btn.setFixedHeight(46)
            login_btn.clicked.connect(self._login)
            side.addWidget(login_btn)

        from .launchbtn import LaunchSplitButton
        self.launch_btn = LaunchSplitButton(self.window_ref)
        side.addWidget(self.launch_btn)

        # 第一百零九轮：这一排改用流式布局 —— 以前是 QHBoxLayout + addStretch(1)，
        # 玩家把窗口拖窄时右边的「游戏管理」直接被顶出可见区裁掉。现在按可用宽度
        # 自动折行，拉宽又回到一行（FlowLayout 见 ui/widgets.py）。
        row2 = FlowLayout(h_spacing=12, v_spacing=8)
        btn_browse = PillButton("浏览整合包", "ghost")
        btn_browse.clicked.connect(lambda: self.window_ref.switch_page("modpack"))
        row2.addWidget(btn_browse)
        btn_dir = PillButton("打开游戏目录", "ghost")
        btn_dir.clicked.connect(self._open_dir)
        row2.addWidget(btn_dir)
        from .loaderpanel import LoaderButton
        self.btn_loader = LoaderButton(self.window_ref)
        row2.addWidget(self.btn_loader)
        # 第四十五轮：下载目标（官方 / 应用）**不在主页**，放在点了「下载游戏」之后
        # 展开的那个弹层里（「下载游戏」字样右边），见 loaderpanel.LoaderPopup
        from .javapanel import JavaButton
        self.btn_java = JavaButton(self.window_ref)
        row2.addWidget(self.btn_java)
        from .gamemanager import GameManagerButton
        self.btn_games = GameManagerButton(self.window_ref)
        row2.addWidget(self.btn_games)
        side.addLayout(row2)

        # 下载任务面板：在按钮下方展示后台下载详情（无任务时自动隐藏）
        from .taskpanel import TaskListPanel
        self.task_panel = TaskListPanel(self.window_ref)
        side.addWidget(self.task_panel)
        side.addStretch(1)

        mid.addLayout(side, 1)
        outer.addLayout(mid, 1)
        outer.addStretch(0)
        # refresh 会整块重建卡片，重建后要把「已隐藏」的状态接上，
        # 否则改个设置/换账号就把卡片又亮出来了

    def _login(self):
        d = LoginDialog(self.window_ref)
        d.logged_in.connect(lambda s: (self.refresh(), self.window_ref.apply_session()))
        d.exec()

    def _open_dir(self):
        d = installer.detect_game_dir()
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)  # noqa: S606
        except Exception:
            Toast.show_toast(self.window_ref, "无法打开游戏目录")
