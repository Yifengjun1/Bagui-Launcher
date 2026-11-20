# -*- coding: utf-8 -*-
"""主入口与主窗口"""
import os
import sys
import time

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QEvent,
                            QParallelAnimationGroup, QPoint, QPropertyAnimation,
                            QRect, QRectF, Qt, QTimer)
from PySide6.QtGui import (QAction, QColor, QFont, QGuiApplication, QIcon,
                           QImage, QPainter, QPainterPath)
from PySide6.QtWidgets import (QApplication, QMenu,
                               QStackedWidget, QSystemTrayIcon, QWidget)

from .core import winacrylic
from .core.asyncio_qt import run_async
from .core.config import APP_NAME, APP_VERSION, CONFIG
from .core.session import current_session, fetch_avatar
from .ui import theme, widgets
from .ui.browse import BrowsePage
from .ui.dialogs import CloseConfirmDialog, LoginDialog, SettingsDialog
from .ui.floatorb import FloatOrb
from .ui.launchselect import LaunchSelectPage
from .ui.autohide import ContentAutoHide
from .ui.drawerhandle import HANDLE_W, DrawerHandle
from .ui.pages import HomePage
from .ui.titlebar import TitleBar
from .ui.wallpaper import Wallpaper
from .ui.widgets import Toast

MARGIN = 8          # 边缘可抓取的宽度
CORNER = 16         # 角落可抓取的边长

# 引导教程要不要自动出现（第八十轮）：只有真正从 main() 启动时才置 True。
# 回归脚本直接 new MainWindow()，不能莫名其妙多出一个置顶小窗。
_TOUR_AUTOSTART = False

# 单实例守卫（main() 里赋值）：重启自己之前必须先放掉它，否则新进程会被
# 当成「第二份」直接退出 —— 玩家点了「是，现在重启」，结果启动器没了。
_SINGLE_GUARD = None


class _MoveBusy(QWidget):
    """搬家进度卡（第九十八轮）：只有跨盘拷贝才会出现。

    为什么要有：跨盘搬 Osmanthus flower 可能是几十 GB，这期间主窗口还没建出来，
    屏幕上什么都没有 = 「点了没反应」。无边框 + 置顶：本软件所有窗口都是无边框
    自绘的，带标题栏的那种会被 winwatch 当成野窗口收走。
    """

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool
                         | Qt.WindowStaysOnTopHint)
        from PySide6.QtWidgets import QLabel, QVBoxLayout
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(430, 104)
        box = QVBoxLayout(self)
        box.setContentsMargins(20, 18, 20, 18)
        card = QWidget(self)
        card.setObjectName("moveCard")
        card.setStyleSheet(
            "#moveCard{background:#ffffff;border-radius:14px;"
            "border:1px solid rgba(120,150,190,0.35);}")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 14, 18, 14)
        self.lb = QLabel("正在把 Osmanthus flower 搬到新位置…")
        self.lb.setStyleSheet("color:#2a3442;font-size:12.5px;font-weight:600;")
        self.lb.setWordWrap(True)
        cl.addWidget(self.lb)
        self.sub = QLabel("这个要拷一会儿，关掉窗口也不会中断")
        self.sub.setStyleSheet("color:#7b8794;font-size:11px;")
        cl.addWidget(self.sub)
        box.addWidget(card)
        try:
            scr = QGuiApplication.primaryScreen().availableGeometry()
            self.move(scr.center().x() - self.width() // 2,
                      scr.center().y() - self.height() // 2)
        except Exception:                                # noqa: BLE001
            pass
        self.show()
        QApplication.processEvents()

    def update_progress(self, text, pct=None):
        try:
            if text:
                self.lb.setText(str(text))
        except RuntimeError:
            return
        QApplication.processEvents()

    def close_now(self):
        try:
            self.hide()
            self.deleteLater()
            QApplication.processEvents()
        except RuntimeError:
            pass


class _MoveNotify:
    """总路径搬家的提醒（第九十九轮：改用**系统通知**）。

    玩家要的效果是「别挡着我，但我知道在干活」。第九十八轮做的是一块置顶白色
    卡片飘在屏幕正中（`_MoveBusy`），跨盘拷几十 GB 时它就那么一直挂着。现在改成
    托盘图标 + Windows 系统通知：开始弹一条、每过四分之一弹一条、结束弹一条，
    细进度写在托盘提示里。

    托盘不可用（精简过的系统、远程会话）时**退回那张卡片**：宁可难看，
    也不能让玩家对着黑屏等十分钟不知道在干嘛。
    """

    _STEPS = (25, 50, 75)

    def __init__(self):
        self.card = None
        self.tray = None
        self._hit = set()
        try:
            if QSystemTrayIcon.isSystemTrayAvailable():
                self.tray = QSystemTrayIcon(make_icon(64))
                self.tray.setToolTip("八桂启动器 · 正在搬总路径")
                self.tray.show()
                QApplication.processEvents()
        except Exception:                                # noqa: BLE001
            self.tray = None
        if self.tray is None:
            try:
                self.card = _MoveBusy()
            except Exception:                            # noqa: BLE001
                self.card = None

    def say(self, title: str, text: str, ms: int = 9000):
        """弹一条系统通知（没有托盘就把话写到那张兜底卡片上）"""
        if self.tray is None:
            if self.card is not None:
                try:
                    self.card.lb.setText(str(text)[:200])
                    QApplication.processEvents()
                except RuntimeError:
                    pass
            return
        try:
            self.tray.showMessage(str(title), str(text),
                                  QSystemTrayIcon.Information, int(ms))
            QApplication.processEvents()
        except Exception:                                # noqa: BLE001
            pass

    def update_progress(self, text, pct=None):
        if text and self.tray is not None:
            try:
                self.tray.setToolTip(f"八桂启动器 · {str(text)[:110]}")
            except Exception:                            # noqa: BLE001
                pass
        if self.card is not None:
            self.card.update_progress(text, pct)
        if pct is None:
            return
        for step in self._STEPS:
            if pct >= step and step not in self._hit:
                self._hit.add(step)
                self.say("正在搬 Osmanthus flower",
                         f"{int(pct)}% —— {text or ''}", 6000)

    def close_now(self):
        if self.card is not None:
            try:
                self.card.close_now()
            except Exception:                            # noqa: BLE001
                pass
        if self.tray is not None:
            # 留几秒再收图标：最后那条「搬完了」的通知还要靠它挂出来。
            # 这时候主窗口自己的托盘图标也要出现了，两个会同时闪一下，能接受。
            try:
                QTimer.singleShot(5000, self._drop)
            except Exception:                            # noqa: BLE001
                self._drop()

    def _drop(self):
        try:
            if self.tray is not None:
                self.tray.hide()
                self.tray = None
        except Exception:                                # noqa: BLE001
            self.tray = None


def make_icon(size=128) -> QIcon:
    """应用图标（樱花树叶方块，base64 内嵌，见 app/ui/branding.py）"""
    from .ui.branding import app_icon
    return app_icon(size)


class EdgeGrip(QWidget):
    """边缘 / 角落缩放抓手。

    之前用的是 QWidget.startSystemResize()（Windows 会接管进入系统缩放循环）。
    在「无边框 + 半透明」窗口上它会返回 True 却什么都不发生，用户的感觉就是
    「窗口拉不动」。这里改成自己算几何：按下记起点，移动时按边/角改窗口矩形，
    并夹住最小尺寸 —— 不依赖平台行为，一定生效。
    """

    def __init__(self, edge: Qt.Edge, window, parent=None):
        super().__init__(parent)
        self.edge = edge
        self.main = window
        self._press = None
        self._geo = QRect()
        self.setCursor({
            Qt.LeftEdge: Qt.SplitHCursor,
            Qt.RightEdge: Qt.SplitHCursor,
            Qt.TopEdge: Qt.SplitVCursor,
            Qt.BottomEdge: Qt.SplitVCursor,
            Qt.LeftEdge | Qt.TopEdge: Qt.SizeFDiagCursor,
            Qt.RightEdge | Qt.TopEdge: Qt.SizeBDiagCursor,
            Qt.LeftEdge | Qt.BottomEdge: Qt.SizeBDiagCursor,
            Qt.RightEdge | Qt.BottomEdge: Qt.SizeFDiagCursor,
        }[edge])

    def min_size(self):
        mn = self.main.minimumSize()
        return max(640, mn.width()), max(480, mn.height())

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint()
            self._geo = QRect(self.main.geometry())
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press is None:
            super().mouseMoveEvent(e)
            return
        d = e.globalPosition().toPoint() - self._press
        g = QRect(self._geo)
        minw, minh = self.min_size()
        if self.edge & Qt.LeftEdge:
            g.setLeft(min(self._geo.left() + d.x(), self._geo.right() - minw + 1))
        if self.edge & Qt.RightEdge:
            g.setRight(max(self._geo.right() + d.x(), self._geo.left() + minw - 1))
        if self.edge & Qt.TopEdge:
            g.setTop(min(self._geo.top() + d.y(), self._geo.bottom() - minh + 1))
        if self.edge & Qt.BottomEdge:
            g.setBottom(max(self._geo.bottom() + d.y(), self._geo.top() + minh - 1))
        self.main.setGeometry(g)
        e.accept()

    def mouseReleaseEvent(self, e):
        self._press = None
        super().mouseReleaseEvent(e)


class MainWindow(QWidget):
    # 顶部导航里属于「下载页」的那几个分类（整合包 / 模组 / 数据包 / 光影 / 材质包）
    DOWNLOAD_KEYS = ("modpack", "mod", "datapack", "shader", "resourcepack")

    def __init__(self, defer_show=False):
        super().__init__()
        widgets.CURRENT_WINDOW = self
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowIcon(make_icon())
        self.setWindowTitle(APP_NAME)
        self.resize(1080, 740)
        self.setMinimumSize(980, 660)
        # 拖入整合包就能装（第六十二轮）。事件过滤装在 QApplication 上：
        # 只用窗口自己的 acceptDrops 的话，拖到搜索框上会被 QLineEdit 吃掉
        # （它会把 file:///D:/pack.zip 当文本插进去），根本到不了窗口。
        self.setAcceptDrops(True)
        QApplication.instance().installEventFilter(self)

        self.current_page = "home"
        self._bg = None
        self._bg_blur = None
        self._was_maximized = False
        self._closing = False

        # ---- 布局 ----
        from PySide6.QtWidgets import QVBoxLayout
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(0)

        self.titlebar = TitleBar(self, self._nav_active, self._orb_click, self._orb_long)
        root.addWidget(self.titlebar)
        self.titlebar.avatar_clicked.connect(self.open_login)
        # 第六十八轮：中央精灵球右键 = 打开设置（左键仍是回主页，长按仍是收成浮球）
        self.titlebar.settings_requested.connect(self.open_settings)
        # 窗口控制：- 最小化 / 方形 最大化还原 / × 触发关闭确认弹窗
        self.titlebar.minimize_clicked.connect(self.showMinimized)
        self.titlebar.max_restore_clicked.connect(self._toggle_max_restore)
        self.titlebar.close_clicked.connect(self.close)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background:transparent;")
        root.addWidget(self.stack, 1)

        self.page_home = HomePage(self)
        self.page_modpack = BrowsePage(self, "modpack")
        self.page_mod = BrowsePage(self, "mod")
        self.page_datapack = BrowsePage(self, "datapack")
        self.page_shader = BrowsePage(self, "shader")
        self.page_resourcepack = BrowsePage(self, "resourcepack")
        self.pages = {
            "home": self.page_home, "modpack": self.page_modpack,
            "mod": self.page_mod, "datapack": self.page_datapack,
            "shader": self.page_shader,
            "resourcepack": self.page_resourcepack,
        }
        for w in self.pages.values():
            self.stack.addWidget(w)

        # 独立启动的「游戏启动选择」抽屉：不进 stack（那样会占满整个内容区），
        # 而是浮在下载页右侧一块 —— 左边照样能看/下载东西。
        self.page_launch = LaunchSelectPage(self, self.stack)
        self.page_launch.on_close = self.hide_launch_panel
        self.page_launch.hide()

        # 第一百轮：右侧一根细白竖条，点它把上面的抽屉从竖条里**向左展开**。
        # 以前要「同一个顶部页签再点一下」才开，玩家找不到，而且和「再点一下
        # 应该没变化」的直觉冲突。竖条跟抽屉同挂 stack，绝对定位、不占内容区。
        self._drawer_handle = DrawerHandle(self.stack)
        self._drawer_handle.clicked.connect(self.toggle_launch_panel)
        self._drawer_handle.hide()
        self._drawer_anim = None
        self._drawer_open = False

        # 内容自动隐藏（设置 → 常规 →「卡片隐藏」）：管所有页面 + 右侧抽屉。
        # 顶部状态栏不在它的管辖范围里，永远不藏。
        self.auto_hide = ContentAutoHide(self)

        self._edges = []
        for edge in (Qt.LeftEdge, Qt.RightEdge, Qt.TopEdge, Qt.BottomEdge,
                     Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.TopEdge,
                     Qt.LeftEdge | Qt.BottomEdge, Qt.RightEdge | Qt.BottomEdge):
            g = EdgeGrip(edge, self, self)
            self._edges.append((edge, g))
            g.raise_()
        self._layout_edges()        # 立刻摆位，别等第一次 resizeEvent

        # ---- 系统效果 ----
        # 注意：不使用 SetWindowCompositionAttribute 系统模糊（部分机器 DWM 会崩溃）。
        # 磨砂观感由应用内 FrostCard（背景模糊取景）实现。
        self.wallpaper = Wallpaper()         # 背景图幻灯片（天空仍按现实时间调色）
        self._regen_bg()                     # 首帧即有背景（修复打开渲染慢）
        # 动态天空：随时间推移天空色渐变、云层缓慢漂移（每 90 秒重建一次背景）
        self._sky_timer = QTimer(self)
        self._sky_timer.setInterval(90_000)
        self._sky_timer.timeout.connect(self._sky_tick)
        self._sky_timer.start()
        # 背景幻灯片：到点换一张（淡入淡出由 _fade_timer 每 33ms 推一步）
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(33)
        self._fade_timer.timeout.connect(self._fade_step)
        self._slide_timer = QTimer(self)
        self._slide_timer.timeout.connect(self._slide_next)
        self._restart_slide_timer()
        self.setWindowOpacity(0.0)
        if defer_show:
            # 开场页预热：只把原生窗口建出来（winId() 会强制创建 HWND），**不 show**。
            # 老版本这里直接 show()（透明度 0），但那层「看不见的窗口」盖在开场页
            # 上面，会把点图标的鼠标事件吃走；而且 show 的首次绘制要 170ms，
            # 放到接力那一刻就会在动画尾巴上卡一下。改成 reveal() 时再 show。
            self.winId()
            self._open_fade = None
        else:
            self.show()  # 先创建原生窗口再设置
            fade = QPropertyAnimation(self, b"windowOpacity")
            fade.setDuration(160)
            fade.setStartValue(0.0)
            fade.setEndValue(1.0)
            fade.setEasingCurve(QEasingCurve.OutCubic)
            fade.start()
            self._open_fade = fade
        winacrylic.set_round_corners(self, True)
        winacrylic.set_dark_titlebar(self, False)

        self._build_tray()
        # 悬浮球：左键恢复窗口（老行为），右键在球旁边选页面（第四十六轮）；
        # 第一一一轮：「强制退出」可用性跟随「游戏是否在跑」
        self.float_orb = FloatOrb(self._restore_from_orb, on_menu=self._orb_open_page,
                                  on_can_quit=self._orb_can_quit)
        # 悬浮球上的游戏加载进度（第五十四轮）：懒建，第一次独立启动才建出来
        self.orb_track = None
        self._orb_progress_pending = False
        self._crash_dlg = None           # 崩溃弹窗（第五十五轮）：懒建，只在崩了时建
        self._apply_session_silent()
        # _first_shown 要在任何切页之前就位（switch_page 里会读它）
        self._first_shown = set()
        self._switch_static("home")
        # 内容自动隐藏：按配置装上计时器（默认是关的，这里只是读一次）
        try:
            self.auto_hide.apply()
        except Exception:
            pass
        # 后台先把「官方启动器自带 runtime」那份 Java 目录扫出来（递归 glob 冷盘 240ms）。
        # 放到这里起一次，之后「Java 下载」「设置 → 启动项」都不用再等它。
        try:
            from .core import loader as _ld
            _ld._warm_deep_java()
        except Exception:
            pass
        # 首屏搜索 / 档案自检都等到「真正出场」再跑：预热阶段（开场动画还在放）
        # 让出 CPU 和网络，别跟视频首帧抢时间（第三十八轮）。
        # 第八十四轮：把「欢迎您」开场页合并进主窗口 —— 不再有独立窗口，
        # 而是主窗上的一个覆盖层（铺满内容区），播完淡出露出主界面。
        self._splash = None
        self._splash_done = False
        if _splash_enabled():
            try:
                from .ui.splash import SplashWindow
                ov = SplashWindow(self, as_window=False)
                self._layout_splash(ov)
                ov.handoff.connect(self._on_splash_handoff)
                ov.begin()
                # 覆盖层显示后再次把边缘缩放抓手压到它之上（begin() 内不再 raise
                # 覆盖层，这里补一道，确保欢迎期间窗口边缘仍可拖拽缩放）
                for _, g in self._edges:
                    try:
                        g.raise_()
                    except Exception:                # noqa: BLE001
                        pass
                self._splash = ov
            except Exception:                            # noqa: BLE001
                self._splash = None

        self._startup_jobs_done = False
        if self._splash is not None:
            # 覆盖层形态：主窗必须显示（否则子控件不可见），但自身不淡入
            # （覆盖层自己带淡入）。首屏任务等覆盖层淡出后再启动。
            try:
                if getattr(self, "_open_fade", None) is not None:
                    self._open_fade.stop()
                self.setWindowOpacity(1.0)
                self._open_fade = None
                self.show()
            except Exception:                            # noqa: BLE001
                pass
        elif not defer_show:
            self._start_startup_jobs()
        # else: defer_show 且无覆盖层 → 等外部 reveal() 再 show + startup

    def _start_startup_jobs(self):
        if self._startup_jobs_done:
            return
        self._startup_jobs_done = True
        QTimer.singleShot(120, self._on_first_show)
        # 启动后自检整合包档案：lastVersionId 为空会让官启报 REQUEST_FAILED，
        # 静默修好再提示（放后台，不阻塞首屏）
        QTimer.singleShot(2600, self._auto_repair_profiles)
        # 自动更新（第 125 轮）：延迟到首屏之后，后台静默检查并下载新向导，
        # 关程序时由 schedule_silent_install 静默装。仅安装版 + 开关开才动作。
        QTimer.singleShot(4000, self._auto_check_update)
        # 引导教程（第八十轮）：等主页卡片、整合包首屏都画完再起，否则量到的是
        # 还没摆好的几何，高亮框会瞄到空处。开关关着 / 已经走过就不再起。
        QTimer.singleShot(1500, self._maybe_tour)

    def _auto_check_update(self):
        """第 125 轮：自动更新——后台检查并下载新版向导，关程序时静默装。

        只在**真安装版**（frozen）+ 开关开时动作：源码跑的回归既连不上 GitHub
        （沙箱代理劫持），也没法真静默替换 exe，绝不能触发。全程不弹窗打扰，
        只在下载就绪后发一条 Toast 告知「关程序即更新」。失败一律静默留痕，
        不影响启动；玩家仍可到「关于」页手动检查。
        """
        if not getattr(sys, "frozen", False):
            return
        if not bool(CONFIG.get("auto_update", True)):
            return
        try:
            from .core import update as upd
            from .core import tasks as task_mod
            from .core.tasks import TASKS
        except Exception:                               # noqa: BLE001
            return

        def checked(info):
            if not info:
                return                                   # 已是最新
            # 已有同版本的待装包就不重复下
            pd = upd.pending()
            if pd and str(pd.get("version", "")) == str(info.get("version", "")):
                return
            task = TASKS.create(f"后台下载更新 v{info.get('version', '')}", "update")
            Toast.show_toast(
                self, f"发现新版本 v{info.get('version', '')}，正在后台下载，关闭程序后自动安装",
                kind="info", ms=5000)

            def work():
                TASKS.set_phase(task.tid, "后台下载更新")
                path = upd.download_setup(
                    info, progress_cb=task_mod.progress_for(task.tid),
                    cancel_flag=task.cancel_flag)
                upd.mark_pending(info, path)
                TASKS.set_phase(task.tid, "更新已就绪，关闭程序后安装")
                return path

            def done(_p):
                TASKS.finish(task.tid, "done", "更新包已就绪")

            def err(e):
                upd._log("自动下载更新失败：" + str(e)[:120])
                TASKS.finish(task.tid, "failed", str(e)[:120])

            run_async(work, on_done=done, on_err=err)

        def check_err(e):
            # 自动检查失败静默留痕，不打扰玩家（手动检查会给明确提示）
            try:
                upd._log("自动检查更新失败：" + str(e)[:120])
            except Exception:                            # noqa: BLE001
                pass

        run_async(upd.check_for_update, on_done=checked, on_err=check_err)

    def _maybe_tour(self):
        # 只在「真的从入口启动」时自动走引导：回归脚本会直接 new MainWindow()，
        # 那种场合置顶小窗 / 遮罩会盖住它们要抓的图、还会把页面切回主页。
        if not _TOUR_AUTOSTART:
            return
        try:
            from .ui import tour
            tour.maybe_start(self)
        except Exception as e:                      # noqa: BLE001
            from .core import crashlog
            crashlog.note("引导：启动失败 %s" % e)

    def start_tour(self, force=True):
        """设置里把「引导教程」重新打开时调它：立刻从第一步走一遍。"""
        from .ui import tour
        if force:
            tour.reset()
        return tour.start(self, 0)

    def stop_tour(self):
        from .ui import tour
        tour.stop(self)

    def ensure_on_screen(self):
        """把窗口拉回可见屏幕（更新后残留屏外位置/断开显示器等情况）"""
        from PySide6.QtGui import QGuiApplication
        geo = QRect(self.geometry())
        target = None
        for scr in QGuiApplication.screens():
            av = scr.availableGeometry()
            if geo.intersects(av):
                target = av
                break
        if target is None:                   # 完全在屏外 -> 居中到主屏
            av = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
            target = av
            geo.moveCenter(av.center())
            geo.moveTop(max(av.top(), geo.top()))
            self.setGeometry(geo)
            return True
        # 与当前屏有交集但越界 -> 拉回
        fixed = False
        ng = QRect(geo)
        if ng.right() > target.right():
            ng.moveRight(target.right()); fixed = True
        if ng.bottom() > target.bottom():
            ng.moveBottom(target.bottom()); fixed = True
        if ng.left() < target.left():
            ng.moveLeft(target.left()); fixed = True
        if ng.top() < target.top():
            ng.moveTop(target.top()); fixed = True
        if fixed:
            self.setGeometry(ng)
        return fixed

    # ---------------- 开场页接力 ----------------
    def reveal(self):
        """预热好的主窗口真正出场：show 出来（透明度仍由 crossfade 拉）。"""
        if getattr(self, "_pending_max", False):
            self._pending_max = False
            self.showMaximized()
        else:
            self.show()
        self.raise_()
        winacrylic.set_round_corners(self, not self.isMaximized())
        self._start_startup_jobs()

    # ---------------- 欢迎覆盖层（第八十四 / 八十五轮） ----------------
    def _layout_splash(self, ov):
        """把欢迎覆盖层铺满整个主窗口（含标题栏区域）。

        第八十五轮：之前只铺内容区；现在铺满整窗，让欢迎页 / 开场动画占满主窗。
        同时把边缘缩放抓手压在覆盖层之上 —— 这样欢迎期间主界面（含标题栏拖动 /
        按钮）被挡住无法操作，但鼠标拉窗口边缘仍能缩放（满足「仅可调节窗口大小」）。
        """
        ov.setGeometry(0, 0, self.width(), self.height())
        try:
            ov._relayout()
        except Exception:                            # noqa: BLE001
            pass
        for _, g in getattr(self, "_edges", []):
            try:
                g.raise_()
            except Exception:                        # noqa: BLE001
                pass

    def _on_splash_handoff(self):
        """欢迎页让位（视频尾 / 出错 / Esc）：覆盖层淡出，露出主界面。"""
        if getattr(self, "_splash_done", False):
            return
        self._splash_done = True
        ov = getattr(self, "_splash", None)
        if ov is None:
            self._splash_reveal()
            return
        try:
            ov.fade_out(XFADE_MS, self._splash_reveal)
        except Exception:                            # noqa: BLE001
            self._splash_reveal()

    def _splash_reveal(self):
        """覆盖层淡出完成后：启动首屏任务，并移除覆盖层。"""
        ov = getattr(self, "_splash", None)
        if ov is not None:
            try:
                ov.deleteLater()
            except Exception:                        # noqa: BLE001
                pass
            self._splash = None
        self._start_startup_jobs()

    # ---------------- 首次展示 ----------------
    def _on_first_show(self):
        self.page_modpack.do_search()

    def _auto_repair_profiles(self):
        """后台自检并修复整合包档案（官启启动失败的常见原因）"""
        from .core import installer

        def done(res):
            res = res or []
            hit = [m for m in res if ("已绑定" in m or "已归位" in m or "补回" in m)]
            miss = [m for m in res if "仍缺" in m]
            # 官启运行时无法修复（会被覆盖），启动阶段静默跳过，等用户手动点修复
            if any("正在运行" in m for m in res):
                return
            if hit:
                Toast.show_toast(self, "已修复整合包档案：" + hit[0], ms=5200, kind="success")
            elif miss:
                Toast.show_toast(self, miss[0], ms=7000, kind="error")

        run_async(installer.repair_installed_profiles,
                  on_done=done, on_err=lambda e: None)

    def _nav_active(self, key):
        return self.current_page == key

    # ---------------- 页面切换 ----------------
    LAUNCH_PANEL_W = 468          # 启动选择抽屉的宽度（窄一点，别把下载页完全盖住）

    def _layout_launch_panel(self):
        """把抽屉贴在内容区右侧：上下左右都留边，绝不越出内容区。

        窄窗口下（pw 已经到 320 下限）如果还按「右侧贴边」算 x，x 会变成负数
        → 抽屉左半边跑到窗口外。这里统一夹一遍，宽度也按可用宽度收。
        """
        r = self.stack.rect()
        m = 8
        avail_w = max(200, r.width() - 2 * m)
        pw = min(self.LAUNCH_PANEL_W, avail_w)
        if pw < self.LAUNCH_PANEL_W:
            pw = max(320, pw) if avail_w >= 320 else avail_w
        x = r.width() - pw - m
        x = max(m, min(x, r.width() - pw - m))
        avail_h = max(180, r.height() - 2 * m)
        self._launch_panel_x = x
        self._launch_panel_w = pw
        self._launch_panel_h = avail_h
        self._launch_hidden_x = r.width() - HANDLE_W - 2

        # 白竖条：垂直居中一小段，别做成整条右边（会挡住下载页的滚动条）
        h = getattr(self, "_drawer_handle", None)
        if h is not None:
            bh = max(110, min(170, int(avail_h * 0.24)))
            hy = m + (avail_h - bh) // 2
            h.setGeometry(r.width() - HANDLE_W - 2, hy, HANDLE_W, bh)

        # 动画正在跑时别抢位置（resize 会把 setGeometry 打回终点）
        a = getattr(self, "_drawer_anim", None)
        busy = a is not None and a.state() != QAbstractAnimation.Stopped
        if not busy:
            self.page_launch.setGeometry(x, m, pw, avail_h)

    def _sync_drawer_handle(self):
        """白竖条只在「独立启动开着 + 正在逛下载页」时出现。"""
        h = getattr(self, "_drawer_handle", None)
        if h is None:
            return
        from .core import gamelaunch
        show = bool(gamelaunch.standalone_enabled()
                    and self.current_page in self.DOWNLOAD_KEYS)
        if not show:
            h.hide()
            if self.page_launch.isVisible() or self._drawer_open:
                self.hide_launch_panel(animate=False)
            return
        self._layout_launch_panel()
        h.show()
        h.raise_()

    def _stop_drawer_anim(self):
        # 递增序号：让上一次动画排好的「终态兜底」失效，别把新动画又打回去
        self._drawer_seq = getattr(self, "_drawer_seq", 0) + 1
        a = getattr(self, "_drawer_anim", None)
        if a is not None:
            try:
                a.stop()
            except Exception:                             # noqa: BLE001
                pass
            self._drawer_anim = None

    def _animate_drawer(self, opening: bool):
        """从右侧白竖条里向左展开 / 收回去。

        面板**宽度始终固定**：不让 QVBoxLayout 每帧重排（第九十八轮定的
        「一次点击后同步耗时 ≤100ms」）。只把 x 从右缘滑到目标位置，超出
        stack 的部分被父控件裁掉 —— 看起来就是「从白条里长出来」。
        """
        self._stop_drawer_anim()
        seq = self._drawer_seq
        m = 8
        pw, avail_h = self._launch_panel_w, self._launch_panel_h
        target_x, hidden_x = self._launch_panel_x, self._launch_hidden_x
        cur_x = (self.page_launch.x() if self.page_launch.isVisible()
                 else hidden_x)
        if opening and not self.page_launch.isVisible():
            cur_x = hidden_x
        start = QRect(cur_x, m, pw, avail_h)
        end = QRect(target_x if opening else hidden_x, m, pw, avail_h)
        self.page_launch.setGeometry(start)
        a = QPropertyAnimation(self.page_launch, b"geometry", self)
        dur = 260 if opening else 200
        a.setDuration(dur)
        a.setEasingCurve(QEasingCurve.OutCubic if opening
                         else QEasingCurve.InCubic)
        a.setStartValue(start)
        a.setEndValue(end)

        def _finish():
            """无论动画有没有发 finished，最终状态都得落地。

            窗口最小化 / 离屏环境会把动画定时器节流，光靠 `finished` 会出现
            「停在离终点几像素」或「收起动画跑完了却没 hide」。
            """
            if seq != getattr(self, "_drawer_seq", -1):
                return                       # 已经有更新的动画接管了
            if opening:
                # 归位用**当前**布局算出来的目标，动画期间窗口改过大小也不歪
                self.page_launch.setGeometry(self._launch_panel_x, m,
                                             self._launch_panel_w,
                                             self._launch_panel_h)
                self.page_launch.show()
                self.page_launch.raise_()
                if hasattr(self, "_drawer_handle"):
                    self._drawer_handle.raise_()
            else:
                self.page_launch.hide()
            self._drawer_anim = None

        if opening:
            self.page_launch.show()
            self.page_launch.raise_()
            if hasattr(self, "_drawer_handle"):
                self._drawer_handle.raise_()
        a.finished.connect(_finish)
        a.start()
        self._drawer_anim = a
        QTimer.singleShot(dur + 80, _finish)      # 兜底，幂等

    def show_launch_panel(self, animate=True):
        """打开抽屉。`animate=False` 用于切页 / 刷新，别每次都滑一遍。"""
        self._layout_launch_panel()
        self.page_launch.reload()
        self._drawer_open = True
        if hasattr(self, "_drawer_handle"):
            self._drawer_handle.set_open(True)
        # 已经完全展开就只刷新内容；半开（动画被打断）要接着滑完
        need = (not self.page_launch.isVisible()
                or self.page_launch.x() > self._launch_panel_x + 2)
        if animate and need:
            self._animate_drawer(True)
        else:
            self._stop_drawer_anim()
            self.page_launch.setGeometry(self._launch_panel_x, 8,
                                         self._launch_panel_w,
                                         self._launch_panel_h)
            self.page_launch.show()
            self.page_launch.raise_()
            if hasattr(self, "_drawer_handle"):
                self._drawer_handle.raise_()
        hide = getattr(self, "auto_hide", None)
        if hide is not None:
            hide.reset()             # 刚打开：别一出来就被自动隐藏收走

    def hide_launch_panel(self, animate=True):
        self._drawer_open = False
        if hasattr(self, "_drawer_handle"):
            self._drawer_handle.set_open(False)
        if animate and self.page_launch.isVisible():
            self._animate_drawer(False)
        else:
            self._stop_drawer_anim()
            self.page_launch.hide()

    def toggle_launch_panel(self):
        """白竖条的点击：开着就收，关着就展开。"""
        if self._drawer_open:
            self.hide_launch_panel(True)
        else:
            self.show_launch_panel(True)

    def _close_floating(self):
        """换页前把「浮着不走的弹层」收掉。

        这些弹层不是页面里的普通控件，而是挂在主窗 / 某个按钮身上的独立小窗口
        （白色磨砂卡片）：换页时它们**不会**跟着页面一起隐藏，于是玩家点到别的页
        还能看见它们停在左边，像凭空多出一片窗。收三类：
        游戏管理弹层、各页的收起式下拉（目标平台 / 相关排序 / 隐藏已添加）、小三角菜单。

        引导教程期间整段跳过 —— 那几个「游戏管理」步骤是引导自己按顺序开关弹层的，
        切页时抢先收掉会让下一步没有目标（`_keep_popup` 是它唯一的开关人）。
        """
        try:
            from .ui import tour as _tour
            if _tour.running():
                return
        except Exception:                                # noqa: BLE001
            pass
        page = getattr(self, "page_home", None)
        btn = getattr(page, "btn_games", None)
        gm = getattr(btn, "_popup", None) if btn is not None else None
        closed = []
        try:
            if gm is not None and gm.isVisible():
                gm.close()
                closed.append("游戏管理弹层")
        except RuntimeError:                             # C++ 侧已销毁
            pass
        try:
            from .ui.widgets import DropDown, MiniMenu
        except Exception:                                # noqa: BLE001
            drop, mini = (), ()
        else:
            # MiniMenu 的构造是 `super().__init__(None, ...)`：parent 参数只用来传
            # 「谁开的」，Qt 侧没有父 → findChildren 抓不到，只能全量过一遍（就一个）
            drop = self.findChildren(DropDown)
            mini = [w for w in QApplication.allWidgets() if isinstance(w, MiniMenu)]
        for d in drop:
            m = getattr(d, "menu", None)
            try:
                if m is not None and m.isVisible():
                    m.close()
                    closed.append("下拉 %s" % (getattr(d, "_text", "") or "?"))
            except RuntimeError:
                pass
        for mn in mini:
            try:
                if mn.isVisible():
                    mn.close()
                    closed.append("小三角菜单")
            except RuntimeError:
                pass
        if closed:
            # 留个痕：以后玩家再报「换页后左边多一片弹窗」，日志里直接看是哪个
            try:
                from .core import crashlog
                crashlog.note("切页收起浮层：%s" % "、".join(closed))
            except Exception:                            # noqa: BLE001
                pass

    def switch_page(self, key):
        # 换页先把浮在别的页面上的弹层收掉（否则它们会停在左边不走）
        self._close_floating()
        # 第一百轮：顶部页签**只管切页**。右侧「选择要启动的游戏」改由内容区
        # 右缘那根白竖条开合 —— 以前要「同一个页签再点一下」才展开，玩家找不到，
        # 而且和「再点一下应该没变化」的直觉相反。
        self.hide_launch_panel(animate=False)
        if key == self.current_page:
            self._sync_drawer_handle()
            return
        self._switch_static(key)
        self._refresh_installed(key)

        if key != "home" and key not in self._first_shown:
            self._first_shown.add(key)
            QTimer.singleShot(60, getattr(self.pages[key], "do_search"))

    def _refresh_installed(self, key=None):
        """回到页面时按磁盘实际状态刷新「已安装/下载」按钮（文件被外部删掉也能同步）"""
        targets = [self.pages[key]] if key else list(self.pages.values())
        for p in targets:
            fn = getattr(p, "refresh_installed", None)
            if fn:
                try:
                    fn()
                except Exception:
                    pass

    def _after_activate(self):
        self._act_busy = False
        self._refresh_installed()

    def _switch_static(self, key):
        self.current_page = key
        self.stack.setCurrentWidget(self.pages[key])
        self.titlebar.sync_nav(key)
        self._sync_drawer_handle()
        # 切页算「人在用」：内容恢复 + 重新计时（不恢复的话新页会是空的）
        hide = getattr(self, "auto_hide", None)
        if hide is not None:
            try:
                hide.reset()
            except Exception:
                pass

    def apply_auto_hide(self):
        """设置页保存后调它：按新配置重装自动隐藏（没有它就退回旧的主页卡片接口）"""
        hide = getattr(self, "auto_hide", None)
        if hide is not None:
            hide.apply()
            return
        page = getattr(self, "page_home", None)
        fn = getattr(page, "apply_card_hide", None)
        if fn:
            fn()

    # ---------------- 精灵球 ----------------
    def _orb_click(self):
        self.switch_page("home")

    def _orb_open_page(self, key):
        """悬浮球右键菜单：页面键先切页，再按左键那套动画把窗口收回来。

        先切页再展开 —— 展开动画只有 320ms，等它演完再跳页会看到闪一下。
        已经在那一页就什么都不动（避免同页再点把启动抽屉开关一次）。
        第一一〇轮：动作键（__hide__ / __quit__）在这里分流，不进切页逻辑。
        """
        if key == "__hide__":
            self._hide_orb_to_tray()
            return
        if key == "__quit__":
            # 第一一一轮语义修正：「强制退出」= 强退**游戏**，启动器不动。
            # 游戏没跑时这项在菜单里就是灰的，走不到这里。
            self._force_quit_game()
            return
        if key and key != self.current_page:
            try:
                self.switch_page(key)
            except Exception:
                pass
        self._restore_from_orb()

    def _orb_menu_hint(self):
        """悬浮球的悬停提示（左键恢复 / 右键选页面或隐藏、强制退出）"""
        return ("左键恢复窗口；右键选页面，或隐藏球 / 强制退出游戏。"
                "官方启动器退出后会自动恢复。")

    def _orb_can_quit(self):
        """「强制退出」的可用条件：独立启动的游戏正在跑"""
        try:
            from .core import gamelaunch
            return bool(gamelaunch.is_running())
        except Exception:                                # noqa: BLE001
            return False

    def _force_quit_game(self):
        """球菜单「强制退出」：只杀游戏进程，启动器自己不动（第一一一轮）。

        `kill_running` 是 terminate + 最多 6 秒轮询 —— 按铁律绝不放主线程，
        走 run_async 到后台杀，前后都用桌面级 DeskToast 说话（主窗此刻
        藏着，挂主窗的 Toast 看不见）。杀完由 `_watch_game` 收尾：说了
        「玩家自己强退」就不再当崩溃弹窗。
        """
        from .core import gamelaunch
        if not gamelaunch.is_running():
            return
        name = gamelaunch.running_name() or "游戏"
        self._force_quit_pending = True
        try:
            widgets.DeskToast.show_desktop(f"正在强制退出 {name}…",
                                           ms=2600, kind="info")
        except Exception:                                # noqa: BLE001
            pass

        def _job():
            try:
                return bool(gamelaunch.kill_running(wait=6.0))
            except Exception:                            # noqa: BLE001
                return False

        def _done(ok):
            ok = bool(ok)
            try:
                widgets.DeskToast.show_desktop(
                    f"{name} 已退出" if ok else
                    f"{name} 没能结束，请到任务管理器手动关闭",
                    ms=3600, kind="success" if ok else "error")
            except Exception:                            # noqa: BLE001
                pass
            # 「强退」标记留给 _watch_game 消费（它把这次退出说成玩家主动、
            # 不弹崩溃窗）。游戏监视计时器没在跑（外部杀的）就在这儿清掉，
            # 别污染下一次真崩溃的判断。
            t = getattr(self, "_game_watch", None)
            if t is None or not t.isActive():
                self._force_quit_pending = False

        run_async(_job, on_done=_done)

    def _hide_orb_to_tray(self):
        """球菜单点「隐藏」：球收掉，改走「最小化至托盘」那套兜底（第一一〇轮）。

        第一一一轮修复「点了没反应」：建窗那一刻 `isSystemTrayAvailable()`
        可能误报 False（开机早期 / explorer 重启），托盘对象就一直是 None，
        旧代码直接进 Toast 分支 return —— 而那条 Toast 挂在**藏起来的主窗**
        上，玩家根本看不见，表现就是「点了什么都没发生」。现在：
        ① 托盘对象不在就当场 `_build_tray()` 重试一次；
        ② 反馈一律用桌面级 DeskToast（独立小窗，主窗藏着也看得见），
           失败原因必须说出来，绝不静默吞掉。
        """
        if getattr(self, "tray", None) is None:
            try:
                self._build_tray()
            except Exception:                            # noqa: BLE001
                pass
        if getattr(self, "tray", None) is None:
            try:
                widgets.DeskToast.show_desktop(
                    "系统托盘不可用，悬浮球暂时不能隐藏", ms=4200, kind="error")
            except Exception:                            # noqa: BLE001
                pass
            return
        # mapToGlobal 在 PySide6 返回的就是 QPoint —— 上一版画蛇添足调了
        # .toPoint()，AttributeError 被 floatorb._pick_page 的 try/except
        # 静默吞掉，玩家看到的就是「点了隐藏什么都没发生」（第一一一轮真凶）
        c = self.float_orb.mapToGlobal(self.float_orb.rect().center())
        self._orb_saved_center = QPoint(c)
        self._stop_orb_progress(fade=False)
        self.float_orb.fade_out()
        self.tray.show()
        try:
            widgets.DeskToast.show_desktop("悬浮球已隐藏：托盘右键「显示」可找回",
                                           ms=3000, kind="success")
        except Exception:                                # noqa: BLE001
            pass

    def _show_orb_from_tray(self):
        """托盘菜单点「显示」：把小悬浮球找回来（第一一〇轮）。

        窗口还开着 → 按老路 collapse_to_orb 把窗口收成球；
        窗口已藏（托盘态）→ 在上次球的位置直接亮出来；没记过位置就用
        主屏右上角兜底。游戏若还在加载，进度环跟着重新挂上。
        """
        if self.float_orb.isVisible():
            return
        if self.isVisible():
            try:
                self.collapse_to_orb(auto=False)
            except Exception:                            # noqa: BLE001
                pass
            return
        c = getattr(self, "_orb_saved_center", None)
        if c is None:
            scr = QGuiApplication.primaryScreen()
            g = scr.availableGeometry() if scr is not None else QRect(0, 0, 1920, 1080)
            c = QPoint(g.right() - 80, g.top() + 90)
        self.float_orb.show_at(c)
        self.float_orb.setToolTip(self._orb_menu_hint())
        if getattr(self, "_orb_progress_pending", False):
            QTimer.singleShot(240, self._start_orb_progress)

    def _orb_long(self):
        """长按精灵球 -> 收纳为悬浮球"""
        self.collapse_to_orb(auto=False)

    # ---------------- 启动官启：自动收起 / 退出后恢复 ----------------
    def collapse_to_orb(self, auto=False):
        """把主窗口收成悬浮小精灵（auto=True 表示由启动官启触发）"""
        if self.float_orb.isVisible() or not self.isVisible():
            return False
        center = self.titlebar.orb.mapToGlobal(self.titlebar.orb.rect().center())
        # 窗口要收起来了：停掉自动隐藏的计时与全局事件过滤
        try:
            self.auto_hide.pause()
        except Exception:
            pass
        self.hide_launch_panel()
        self._was_maximized = self.isMaximized()
        self._auto_collapsed = bool(auto)
        # 显式记录收纳前的完整几何（restore 用它作展开终点）
        self._pre_retract_geo = QRect(self.geometry())
        start = QRectF(self.geometry())
        end = QRectF(center.x() - 28, center.y() - 28, 56, 56)
        if self._was_maximized:
            self.showNormal()
            self._pre_retract_geo = QRect(self.geometry())
            start = QRectF(self.geometry())
        self.setEnabled(False)
        g1 = QPropertyAnimation(self, b"geometry")
        g1.setDuration(340)
        g1.setStartValue(start.toRect())
        g1.setEndValue(end.toRect())
        g1.setEasingCurve(QEasingCurve.InOutCubic)
        g2 = QPropertyAnimation(self, b"windowOpacity")
        g2.setDuration(340)
        g2.setStartValue(1.0)
        g2.setEndValue(0.0)
        g2.setEasingCurve(QEasingCurve.OutCubic)
        grp = QParallelAnimationGroup(self)
        grp.addAnimation(g1)
        grp.addAnimation(g2)

        def finish():
            self.hide()
            self.setEnabled(True)
            self.setWindowOpacity(1.0)
            self.float_orb.show_at(center)
            self.float_orb.setToolTip(self._orb_menu_hint())
            # 球真出来的这一刻才挂加载进度（收起动画慢一点也不会漏掉，第五十四轮）
            if getattr(self, "_orb_progress_pending", False):
                QTimer.singleShot(240, self._start_orb_progress)
        grp.finished.connect(finish)
        grp.start()
        self._retract_anim = grp
        return True

    def on_official_launched(self, info=""):
        """官启已确认启动：收起窗口 + 开始监视它何时退出"""
        self._start_launcher_watch()
        QTimer.singleShot(900, lambda: self.collapse_to_orb(auto=True))

    def _start_launcher_watch(self):
        t = getattr(self, "_launch_watch", None)
        if t is None:
            t = QTimer(self)
            t.setInterval(4000)
            t.timeout.connect(self._watch_launcher)
            self._launch_watch = t
        self._watch_seen = True          # launch_official_launcher 已确认进程在跑
        self._watch_busy = False
        self._launch_started = time.time()   # 官启这条路的崩溃报告要按这个时间筛
        if not t.isActive():
            t.start()

    def _stop_launcher_watch(self):
        t = getattr(self, "_launch_watch", None)
        if t is not None:
            t.stop()

    def _watch_launcher(self):
        if getattr(self, "_watch_busy", False):
            return
        self._watch_busy = True
        from .core import installer

        def done(running):
            self._watch_busy = False
            if running:
                self._watch_seen = True
                return
            if not getattr(self, "_watch_seen", False):
                return
            self._stop_launcher_watch()
            self._on_launcher_exit()
            # 官启这条路也能抓崩溃（第五十五轮）：游戏崩了会往 crash-reports
            # 写一份报告，只要它是「这次启动之后」写出来的，就是这次崩的。
            info = self._official_crash_info()
            if info:
                QTimer.singleShot(720, lambda: self._show_crash(info))

        run_async(installer.launcher_running, on_done=done,
                  on_err=lambda e: setattr(self, "_watch_busy", False))

    def _official_crash_info(self):
        """官方启动器退出后：找「这次启动之后」新写出来的崩溃报告。

        只认 mtime 晚于本次启动时刻的报告 —— 目录里躺着的旧报告不能当成这次的，
        否则玩家每次关官启都会看到一个几天前的崩溃弹窗。
        """
        from .core import gamelaunch, paths
        since = float(getattr(self, "_launch_started", 0) or 0)
        best, best_t = "", 0.0
        for gd in (paths.official_game_dir(), paths.app_game_dir()):
            rep = gamelaunch.crash_report(gd, since)
            if not rep:
                continue
            try:
                mt = os.path.getmtime(rep)
            except OSError:
                continue
            if mt > best_t:
                best, best_t = rep, mt
        if not best:
            return {}
        gd = os.path.dirname(os.path.dirname(best))
        return {"name": os.path.basename(gd.rstrip("\\/")) or "游戏",
                "log": "", "report": best, "cwd": gd, "instance": gd,
                "mc": "", "version": "", "java": "", "memory_mb": 0,
                "code": None, "lived": 0, "crashed": True}

    def _on_launcher_exit(self, text="官方启动器已退出", game_over=False):
        """官方启动器 / 独立启动的游戏退出：窗口没在屏幕上就自动展开回来

        game_over=True 表示独立启动的游戏进程确实已经结束，可以立刻把输入法
        切回中文；官方启动器那条路不知道游戏进程，交给 _start_ime_watch 盯着。
        """
        hidden = not self.isVisible()
        if hidden:
            self._restore_from_orb()
            QTimer.singleShot(600, lambda: Toast.show_toast(
                self, f"{text}，窗口已自动恢复", ms=3600, kind="success"))
        else:
            Toast.show_toast(self, text, ms=2600)
        self._restore_ime(game_over=game_over)

    # ---------------- 输入法：游戏退出后切回中文 ----------------
    def _restore_ime(self, game_over=False):
        """把启动游戏前切走的输入法还回去（见 core/ime.py）"""
        from .core import ime
        if not ime.pending():
            return
        if game_over:
            ime.restore()
        else:
            # 官启可能已经退出而游戏还在跑：先看有没有 java，有就等它退干净
            self._start_ime_watch()

    def _start_ime_watch(self):
        from .core import ime
        t = getattr(self, "_ime_watch", None)
        if t is None:
            t = QTimer(self)
            t.setInterval(6000)
            t.timeout.connect(self._ime_watch_tick)
            self._ime_watch = t
        self._ime_xgen = ime.generation()
        t.start()
        self._ime_watch_tick()          # 立刻先探一次，别白等 6 秒

    def _ime_watch_tick(self):
        from .core import ime
        t = getattr(self, "_ime_watch", None)
        if t is None:
            return
        if not ime.pending() or ime.generation() != getattr(self, "_ime_xgen", -1):
            t.stop()                     # 已经切回来了 / 玩家又开了一局
            return
        from .core import installer
        busy = getattr(self, "_ime_busy", False)
        if busy:
            return
        self._ime_busy = True

        def done(names):
            self._ime_busy = False
            if not ime.pending() or ime.generation() != getattr(self, "_ime_xgen", -1):
                t.stop()
                return
            if names & ime.JAVA_PROC_NAMES:
                return                   # 游戏还在跑，继续等
            t.stop()
            ime.restore()

        def err(_e):
            self._ime_busy = False

        # tasklist 最长十几秒，绝不能在 UI 线程里跑
        run_async(installer._all_processes, on_done=done, on_err=err)

    # ---------------- 独立启动：游戏进程退出后恢复窗口 ----------------
    def on_game_launched(self, info=""):
        """独立启动成功：和官启一样把窗口收成浮球，游戏退出后自动恢复"""
        self._dep_warned = False          # 新一轮游戏，缺前置只报一次
        self._dep_killed = False          # 第六十八轮：新进程，别再记着上次关过它
        self._start_game_watch()
        self._orb_progress_pending = True
        QTimer.singleShot(900, lambda: self.collapse_to_orb(auto=True))
        # 球出来之后再挂加载进度：先气泡「游戏进程已启动」，再绕球转一圈，
        # 3 秒后换「游戏加载中… N%」，窗口一出现就 100%（第五十四轮）。
        # 这里只是兜底（球本来就在屏幕上、不需要收起时）—— 正常路径是
        # collapse_to_orb 收尾时叫一次，收起动画慢一点也不会漏挂。
        QTimer.singleShot(1600, self._start_orb_progress)

    # ---------------- 悬浮球上的游戏加载进度 ----------------
    def _start_orb_progress(self):
        from .core import gamelaunch
        if not getattr(self, "_orb_progress_pending", False):
            return
        if not self.float_orb.isVisible():
            return                       # 球还没出来：等收起动画的收尾再叫一次
        pid = gamelaunch.pid()
        if not pid:
            self._orb_progress_pending = False
            return
        try:
            if self.orb_track is None:
                from .ui.orbring import OrbLoadTrack
                self.orb_track = OrbLoadTrack(self.float_orb)
            self.orb_track.start(pid, gamelaunch.running_name())
        except Exception:
            self.orb_track = None
        self._orb_progress_pending = False

    def _stop_orb_progress(self, fade=True):
        self._orb_progress_pending = False
        t = getattr(self, "orb_track", None)
        if t is not None:
            try:
                t.stop(fade=fade)
            except Exception:
                pass

    def _start_game_watch(self):
        t = getattr(self, "_game_watch", None)
        if t is None:
            t = QTimer(self)
            t.setInterval(3000)
            t.timeout.connect(self._watch_game)
            self._game_watch = t
        t.start()

    def _watch_game(self):
        from .core import gamelaunch
        if gamelaunch.is_running():
            # 第五十八轮：进程还活着也可能是「已经废了」—— 卡在缺前置那张
            # 「加载失败」界面上。这时不认出来，玩家只能自己盯着一堆英文发呆。
            self._watch_dep_failure()
            return
        # 第六十八轮：这一次是我们自己把游戏关掉的（缺前置），窗口/输入法照旧恢复，
        # 但别再弹第二个崩溃窗 —— 上面那个已经带着原因摆出来了。
        dep_killed = bool(getattr(self, "_dep_killed", False))
        self._game_watch.stop()
        self._stop_orb_progress()
        if dep_killed:
            self._on_launcher_exit("游戏已关闭（缺少前置模组，已为你停下）",
                                   game_over=True)
            return
        # 第一一一轮：玩家刚在球菜单点了「强制退出」→ 这次退出是主动的，
        # 别走 exit_report/崩溃弹窗那套（杀掉的进程退出码非 0，会被误判成崩了）
        if bool(getattr(self, "_force_quit_pending", False)):
            self._force_quit_pending = False
            self._on_launcher_exit("游戏已被你强制退出", game_over=True)
            return
        try:
            info = gamelaunch.exit_info()
        except Exception:
            info = {}
        try:
            msg = gamelaunch.exit_report()
        except Exception:
            msg = "游戏已退出"
        self._on_launcher_exit(msg, game_over=True)
        # 崩了就别只弹一条 toast（第五十五轮）：把原因摊开给玩家看，
        # 缺前置还能一键补。窗口先恢复好再弹，否则弹窗锚在浮球上会跑到屏幕角。
        if info.get("crashed"):
            QTimer.singleShot(720, lambda: self._show_crash(info))

    def _watch_dep_failure(self):
        """游戏还在跑，但日志里已经写着「缺前置加载失败」→ 立刻摊开给玩家看。

        只报一次（`_dep_warned`）：报完就安静等玩家处理，不能每 3 秒弹一个。

        第六十八轮改成「认出来就直接把游戏关掉」：加载器自己那屏报错是英文长文，
        玩家在那儿既没按钮也没出路；我们的弹窗里有原因、缺什么、能不能自动补。
        所以先取现场（`exit_info` 要趁进程还活着调），再关进程，最后弹窗。
        """
        if getattr(self, "_dep_warned", False):
            return
        from .core import gamelaunch
        try:
            rows = gamelaunch.live_missing()
        except Exception:                              # noqa: BLE001
            return
        if not rows:
            return
        self._dep_warned = True
        self._dep_killed = True        # 关掉游戏这件事只能做一次，别在下次 tick 再弹一遍
        try:
            info = gamelaunch.exit_info()
        except Exception:                              # noqa: BLE001
            info = {}
        info["dep_missing"] = rows
        info["crashed"] = True
        info["running"] = True        # 弹窗据此改口：「加载失败」而不是「意外退出」
        self._stop_orb_progress()     # 已经确定进不去了，进度环再转就是骗人
        # 关进程要等 terminate → poll，最长几秒，绝不能在 UI 线程里阻塞
        try:
            run_async(gamelaunch.kill_running)
        except Exception:                              # noqa: BLE001
            pass
        # 这时窗口大概率收在浮球里。弹窗是挂在主窗上的子窗，主窗藏着的话
        # 玩家既看不见也点不动 —— 先把窗口叫回来（跟第二实例敲门走同一条路）。
        try:
            self.raise_window()
        except Exception:                              # noqa: BLE001
            pass
        QTimer.singleShot(700, lambda: self._show_crash(info))

    def _show_crash(self, info):
        """崩了 → 诊断 + 弹窗（PCLCE 那套：原因 / 自动补充 / 查看日志 / 分享日志）"""
        from .core import crashdiag
        try:
            diag = crashdiag.analyze(info)
        except Exception:                                # noqa: BLE001
            diag = {"summary": "游戏异常退出", "reasons": [], "missing": [],
                    "suspects": [], "text": ""}
        reasons = diag.get("reasons") or []
        # 没有任何实质结论、退出码又是 0（玩家自己关的游戏）→ 不打扰
        if not any(r.get("kind") != "unknown" for r in reasons) and \
                info.get("code") in (0, None) and not info.get("report"):
            return
        try:
            old = getattr(self, "_crash_dlg", None)
            if old is not None:
                try:
                    old.close()
                    old.deleteLater()
                except Exception:
                    pass
            from .ui.crashdlg import CrashDialog
            d = CrashDialog(self, info, diag)
            self._crash_dlg = d
            # 第六十八轮：前置全补好 → 3 秒后自动重启游戏（玩家不用再回去点启动）
            d.fixed_all.connect(self._schedule_relaunch)
            d.show()
            d.raise_()
            d.activateWindow()
        except Exception:                                # noqa: BLE001
            pass

    def _schedule_relaunch(self, count: int = 0):
        """前置补完了：给 3 秒缓冲（玩家正看着弹窗），然后自动再启动一次。"""
        from .core import gamelaunch
        if gamelaunch.is_running():
            return
        Toast.show_toast(self, f"前置已补齐（{count} 个），3 秒后自动重启游戏…",
                         ms=3000, kind="success")
        QTimer.singleShot(3000, self.relaunch_game)

    def relaunch_game(self):
        """再启动一次游戏（用主界面那个启动按钮，走同一条独立启动链路）。"""
        from .core import gamelaunch
        if gamelaunch.is_running():
            return
        btn = getattr(self.page_home, "launch_btn", None)
        if btn is None:
            for _k, p in self.pages.items():
                btn = getattr(getattr(p, "sidebar", None), "launch_btn", None)
                if btn is not None:
                    break
        if btn is None:
            return
        try:
            btn.refresh()                 # 先按当前目标重算文案/可用态
            btn._launch_game()
        except Exception:                                # noqa: BLE001
            pass


    def raise_window(self):
        """把窗口叫到最前：收成浮球 / 最小化了 / 藏起来了都能回来。

        第二实例敲门（core/single.py）和托盘「显示主界面」都走这里，别各写一套 ——
        收成浮球时窗口是 hide() 掉的，只 raise_() 是看不见的，必须先展开。
        """
        try:
            if not self.isVisible():
                if self.float_orb.isVisible():
                    self._restore_from_orb()      # 收成浮球 → 从球下面展开回来
                else:
                    self._restore_normal()        # 藏在托盘 / 被直接 hide 了 → 直接回来
            else:
                if self.isMinimized():
                    self.showNormal()
                self.raise_()
                self.activateWindow()
            from .core.single import flash_taskbar
            flash_taskbar(int(self.winId()))      # 前台锁拿不到焦点时，至少闪一下任务栏
            return True
        except Exception:                         # noqa: BLE001
            return False

    def _restore_from_orb(self):
        """点击悬浮球 -> 从浮球下方展开到完整窗口（始终保持在显示器内）"""
        from PySide6.QtGui import QGuiApplication
        self._stop_orb_progress()        # 球要下去了，绕球的进度 / 气泡一起收
        self.float_orb.fade_out()
        self._regen_bg()

        # 终点：收纳前记录的完整几何（修正进屏幕可用区，防历史位置异常）
        base = getattr(self, "_pre_retract_geo", None) or QRect(self.geometry())
        scr = (QGuiApplication.screenAt(
            self.float_orb.mapToGlobal(self.float_orb.rect().center()))
            or self.screen() or QGuiApplication.primaryScreen())
        avail = scr.availableGeometry()
        target = QRect(base)
        if target.width() > avail.width():
            target.setWidth(avail.width())
        if target.height() > avail.height():
            target.setHeight(avail.height())
        if target.right() > avail.right():
            target.moveRight(avail.right())
        if target.bottom() > avail.bottom():
            target.moveBottom(avail.bottom())
        target.moveLeft(max(avail.left(), target.left()))
        target.moveTop(max(avail.top(), target.top()))

        # 起点：浮球正下方的小窗（水平居中于浮球），同样 clamp 在屏幕内
        oc = self.float_orb.mapToGlobal(self.float_orb.rect().center())
        start = QRect(oc.x() - 28, self.float_orb.mapToGlobal(
            self.float_orb.rect().bottomLeft()).y() + 6, 56, 56)
        if start.right() > avail.right():
            start.moveRight(avail.right())
        if start.bottom() > avail.bottom():
            start.moveBottom(avail.bottom() - 8)
        if start.left() < avail.left():
            start.moveLeft(avail.left())
        if start.top() < avail.top():
            start.moveTop(avail.top())

        self.setGeometry(start)
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self.activateWindow()

        g1 = QPropertyAnimation(self, b"geometry")
        g1.setDuration(320)
        g1.setStartValue(start)
        g1.setEndValue(target)
        g1.setEasingCurve(QEasingCurve.OutCubic)
        g2 = QPropertyAnimation(self, b"windowOpacity")
        g2.setDuration(240)
        g2.setStartValue(0.0)
        g2.setEndValue(1.0)
        grp = QParallelAnimationGroup(self)
        grp.addAnimation(g1)
        grp.addAnimation(g2)

        def finish():
            self.setGeometry(target)
            self.setWindowOpacity(1.0)
            self._regen_bg()
            self.update()
            if self._was_maximized:
                self.showMaximized()
            # 窗口回来了：自动隐藏重新装过滤、重新计时
            try:
                self.auto_hide.resume()
            except Exception:
                pass
        grp.finished.connect(finish)
        grp.start()
        self._restore_anim = grp

    # ---------------- 会话 ----------------
    def apply_session(self):
        s = current_session()
        if s:
            name = s.get("name")

            def _avatar_done(img):
                self.titlebar.set_session(name, img)
            run_async(fetch_avatar, name, s.get("uuid"), s.get("skin_url"),
                      size=96, on_done=_avatar_done)
        else:
            self.titlebar.set_session(None, None)
        self.page_home.refresh()
        for p in self.pages.values():
            sb = getattr(p, "sidebar", None)
            card = getattr(sb, "char_card", None)
            if card is not None:
                card.refresh()

    def _apply_session_silent(self):
        s = current_session()
        if s:
            name = s.get("name")

            def _avatar_done(img):
                self.titlebar.set_session(name, img)
            run_async(fetch_avatar, name, s.get("uuid"), s.get("skin_url"),
                      size=96, on_done=_avatar_done)

    def open_settings(self):
        d = SettingsDialog(self)
        d.exec()
        self.apply_session()
        self.refresh_launch_ui()
        self._hint_standalone_target()

    def _hint_standalone_target(self):
        """开了独立启动、却还没选游戏 —— 按钮是灰的，玩家会以为「开了没用」。

        第九十七轮：玩家反馈「设置里开了独立启动，主页还是启动官方启动器」。
        模式没切换是主因（已修）；这台机器上另一个「看着没反应」的情况就是
        没选目标时按钮直接不可点 —— 所以关掉设置窗后补一句话，别让人自己猜。
        """
        try:
            from .core import gamelaunch
            if gamelaunch.standalone_enabled() and not gamelaunch.launch_target():
                Toast.show_toast(
                    self, "已切到独立启动：先去「游戏管理」点一下要启动的游戏，"
                          "之后按启动就直接进游戏（不经过官方启动器）",
                    ms=5600, kind="info")
        except Exception:                            # noqa: BLE001
            pass

    def refresh_launch_ui(self):
        """设置改了 / 换了独立启动目标之后，把所有启动按钮的文案与状态同步一遍。

        遍历 self.pages（别硬写页面元组：原来漏了 datapack，那一页的按钮永远不刷新）。
        """
        for key, p in self.pages.items():
            sb = getattr(p, "sidebar", None)
            btn = getattr(sb, "launch_btn", None)
            if btn is None:
                continue
            try:
                btn.refresh()
            except Exception:
                pass
        btn = getattr(self.page_home, "launch_btn", None)
        if btn is not None:
            try:
                btn.refresh()
            except Exception:
                pass
        # 第六十一轮：换了启动目标 = 换了「装到哪台游戏」，下载页每张卡片的
        # 「已安装 / 下载 / 原版不可用」都得按新目标重算一遍
        self._refresh_installed()
        # 第六十八轮：设置里改了「独立启动」→ 「官方」胶囊该灰 / 该亮，全部刷一遍
        try:
            from .ui.loaderpanel import refresh_all_locks
            refresh_all_locks()
        except Exception:                            # noqa: BLE001
            pass
        # 独立启动开关被关掉时，「游戏启动选择」抽屉不该还留在屏幕上
        from .core import gamelaunch
        if not gamelaunch.standalone_enabled():
            self.hide_launch_panel(animate=False)
        elif self.page_launch.isVisible():
            self.show_launch_panel(animate=False)   # 还开着就刷新，不重播动画
        self._sync_drawer_handle()

    def _toggle_max_restore(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def open_login(self):
        d = LoginDialog(self)
        d.logged_in.connect(lambda s: self.apply_session())
        d.exec()

    # ---------------- 托盘 ----------------
    def _build_tray(self):
        """建托盘图标（**不显示**）。

        第六十八轮：托盘只在「关闭弹窗点了『是』」那一刻才出现 —— 平时窗口开着时
        任务栏右边多一个图标没意义，玩家也容易被「关了窗口程序还在」搞糊涂。
        所以这里只把对象和菜单备好，显不显示由 `closeEvent` / `_restore_normal` 决定。
        """
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        self.tray = QSystemTrayIcon(make_icon(64), self)
        menu = QMenu()
        # 第一一〇轮：右键菜单 = 打开启动器 / 显示（找回悬浮球）/ 退出。
        # 「打开启动器」恢复主窗口（球顺带收掉）；「显示」只负责把小悬浮球
        # 亮回来 —— 玩家把球隐藏后从托盘这里找回，窗口保持藏着的原样。
        act_open = QAction("打开启动器", menu)
        act_open.triggered.connect(self._restore_normal)
        act_orb = QAction("显示", menu)
        act_orb.triggered.connect(self._show_orb_from_tray)
        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self._really_quit)
        menu.addAction(act_open)
        menu.addAction(act_orb)
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip(f"{APP_NAME}  v{APP_VERSION}")
        self.tray.activated.connect(
            lambda r: self._restore_normal() if r == QSystemTrayIcon.Trigger else None)
        # 不 show()：等玩家真的选择「最小化到托盘」再亮出来

    def _restore_normal(self):
        # 窗口回来了，托盘就没用了 —— 收掉，回到「平时不显示」（第六十八轮）
        if getattr(self, "tray", None) is not None:
            try:
                self.tray.hide()
            except Exception:                        # noqa: BLE001
                pass
        self._stop_orb_progress(fade=False)
        self.float_orb.hide()
        self.setWindowOpacity(1.0)
        self.showNormal()
        pre = getattr(self, "_pre_retract_geo", None)
        if pre is not None and not self.isMaximized():
            self.setGeometry(pre)
        self.raise_()
        self.activateWindow()

    # ---------------- 关闭 ----------------
    _GAME_RUNNING_TIP = "游戏正在运行中暂时无法关闭启动器窗口"

    def _game_running(self):
        """独立启动的游戏是不是还在跑（关窗口前必须问一句）"""
        try:
            from .core import gamelaunch
            return bool(gamelaunch.is_running())
        except Exception:                                # noqa: BLE001
            return False

    def closeEvent(self, e):
        if self._closing:
            e.accept()
            return
        # 第九十三轮：游戏还在跑就不许关 —— 关掉启动器会把这条监控（退出检测、
        # 输入法切回中文、日志收尾）一起带走，玩家的存档/输入法就留在了半路上。
        running = self._game_running()
        # 第四十五轮：默认不弹窗 —— 点 × 直接全部关掉。
        # 想保留「最小化到托盘 / 退出」两个选项，去 设置 → 常规 → 关闭弹窗 打开。
        if not CONFIG.get("close_confirm", False):
            if running:
                widgets.Toast.show_toast(self, self._GAME_RUNNING_TIP,
                                         ms=4200, kind="info")
                e.ignore()
                return
            self._really_quit()
            e.accept()
            return
        d = CloseConfirmDialog(self)
        r = d.exec()
        if r == CloseConfirmDialog.YES:
            self.hide()                       # 最小化至托盘
            if self.tray:
                # 第六十八轮：托盘图标到这一刻才显示（平时不显示）
                self.tray.show()
                # 内置提示条（原来用 tray.showMessage 是 Windows 系统通知气泡）
                widgets.DeskToast.show_desktop("已最小化到托盘，点托盘图标可恢复",
                                               ms=3000, kind="success")
            e.ignore()
        elif r == CloseConfirmDialog.NO:
            if running:
                # 开了弹窗也一样：点「否（退出）」在这时按不下去，但要说清为什么
                widgets.Toast.show_toast(self, self._GAME_RUNNING_TIP,
                                         ms=4200, kind="info")
                e.ignore()
                return
            self._really_quit()
            e.accept()
        else:
            e.ignore()

    def restart_app(self):
        """重启自己（第九十八轮：总路径迁移用）。

        顺序很讲究：**先放掉单实例锁，再起新进程**，最后才退出自己。反过来的话
        新进程 `try_acquire()` 会看到「已经有一个在跑」，把自己当成第二份直接退出
        —— 玩家点了「是，现在重启」，结果启动器凭空消失。

        新进程起来后第一件事就是执行 `core/relocate.py` 里记下的搬迁待办
        （见 main() 里 `relocate.pending()` 那一段）。
        """
        import subprocess
        try:
            if getattr(sys, "frozen", False):
                cmd = [sys.executable]
                cwd = os.path.dirname(os.path.abspath(sys.executable))
            else:
                entry = os.path.join(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))), "run.py")
                cmd = [sys.executable, entry]
                cwd = os.path.dirname(entry)
        except Exception as e:                            # noqa: BLE001
            Toast.show_toast(self, f"重启失败，请手动关掉再打开：{str(e)[:60]}",
                             ms=6000, kind="error")
            return False
        g = globals().get("_SINGLE_GUARD")
        if g is not None:
            try:
                g.release()
            except Exception:                             # noqa: BLE001
                pass
        try:
            subprocess.Popen(cmd, cwd=cwd or None,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        except Exception as e:                            # noqa: BLE001
            Toast.show_toast(self, f"重启失败，请手动关掉再打开：{str(e)[:60]}",
                             ms=6000, kind="error")
            return False
        self._really_quit()
        return True

    def _really_quit(self):
        """真正退出进程。

        注意：本机实测 QApplication.quit() / QCoreApplication.quit() 会被
        系统托盘与浮球的窗口（非主窗口）挡住而不生效，必须用 exit(0)；
        再用一次性定时器兜底，避免 exit 也被卡住时留下僵尸进程。
        """
        self._closing = True
        # 第九十九轮：还欠着「总路径搬家」也**不在这里搬**。
        # 玩家在设置里点「否」的意思就是「路径先保持原样，等我下次重开启动器再说」；
        # 关窗口不等于重开，退出这一刻动目录 = 玩家还没重启路径就变了。
        # 待办原样留在数据目录，由下次 main() 启动时统一落地：同盘改名瞬时，
        # 跨盘那时才带系统通知 + 进度。（第九十八轮关窗时那次搬迁调用已移除。）
        # 关软件时如果还欠着「切回中文」，这时候补上：绝不能让玩家带着
        # en-US 的默认输入语言离开（那是全局设置，影响所有新窗口）
        try:
            from .core import ime
            ime.restore()
        except Exception:
            pass
        try:
            CONFIG.save()
        except Exception:
            pass
        # 第 124 轮：有待安装的更新就在这里调度分离进程 —— 它会等本进程真的
        # 退出后再静默运行新向导覆盖 exe。必须在 exit(0) 之前安排好，
        # 否则主进程一没，没人去装。仅 frozen + 有待安装标记才动作。
        try:
            from .core import update as _upd
            _upd.schedule_silent_install()
        except Exception:
            pass
        if getattr(self, "tray", None):
            self.tray.hide()
        orb = getattr(self, "float_orb", None)
        if orb is not None:
            try:
                orb.hide()
            except Exception:
                pass
        # 绕球那条进度 / 气泡是独立置顶小窗，得一起收掉，否则会挡住退出
        self._stop_orb_progress(fade=False)
        QApplication.exit(0)
        QTimer.singleShot(1200, lambda: os._exit(0))

    # ---------------- 绘制与布局 ----------------
    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._layout_edges()
        # 启动选择抽屉是绝对定位的浮层，窗口变了要跟着贴回右侧
        if hasattr(self, "page_launch"):
            self._layout_launch_panel()
            if self.page_launch.isVisible():
                self.page_launch.raise_()
        if not hasattr(self, "_bg_timer"):
            self._bg_timer = QTimer(self)
            self._bg_timer.setSingleShot(True)
            self._bg_timer.setInterval(60)
            self._bg_timer.timeout.connect(self._regen_bg_after_resize)
        # 立刻重绘一帧（paintEvent 会把旧底图拉伸铺满新尺寸），
        # 别等到防抖到点才动 —— 否则拖动过程中右下角一直是空的（只有
        # 最小化再还原时 changeEvent 里的 update 才补上，就是玩家看到的现象）。
        self.update()
        self._bg_timer.start()
        # 第八十四轮：欢迎覆盖层铺满内容区，窗口尺寸变了要跟着重排
        if getattr(self, "_splash", None) is not None:
            self._layout_splash(self._splash)

    def moveEvent(self, e):
        super().moveEvent(e)

    # ---------------------------------------------------------------- 拖入整合包
    def _drop_target_ok(self, obj) -> bool:
        """这次拖放该不该由我们接：得是本窗口里的控件、没有模态窗挡着"""
        try:
            if not self.isVisible():
                return False
            if QApplication.activeModalWidget() is not None:
                return False
            w = obj if isinstance(obj, QWidget) else None
            return w is not None and (w is self or self.isAncestorOf(w))
        except Exception:                              # noqa: BLE001
            return False

    def eventFilter(self, obj, ev):
        """全局接住「文件拖进来」—— 单独靠 self 的 acceptDrops 不够。

        QLineEdit 之类自己就接受拖放（会把 file:///… 当文本插进去），
        事件根本冒泡不到窗口；这里在应用级先看 MIME 有没有本地文件。
        """
        try:
            t = ev.type()
            if t in (QEvent.DragEnter, QEvent.DragMove, QEvent.Drop):
                md = ev.mimeData()
                if (md is not None and md.hasUrls() and self._drop_target_ok(obj)
                        and any(self._drop_paths(md))):
                    if t == QEvent.Drop:
                        self._handle_drop(md)
                    ev.setDropAction(Qt.CopyAction)
                    ev.accept()          # 必须真 accept，否则 Qt 不会送来 Drop
                    return True
        except Exception:                              # noqa: BLE001
            pass
        return super().eventFilter(obj, ev)

    def _drop_paths(self, mime):
        from .ui import packdrop
        out = []
        try:
            for u in (mime.urls() or [])[:8]:
                if u.isLocalFile():
                    p = u.toLocalFile()
                    if p and packdrop.accepted(p):
                        out.append(p)
        except Exception:                              # noqa: BLE001
            pass
        return out

    def _handle_drop(self, mime):
        from .ui import packdrop
        try:
            packdrop.handle_drop(self, mime)
        except Exception as e:                          # noqa: BLE001
            Toast.show_toast(self, f"这个包没能装上：{str(e)[:70]}", kind="error")

    # 兜底：万一某处事件没被过滤到，窗口自己也能接（不加这个，Qt 连
    # DragEnter 都不往上传，光标一直是「禁止」图标）
    def dragEnterEvent(self, e):
        if self._drop_paths(e.mimeData()):
            e.setDropAction(Qt.CopyAction)
            e.accept()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if self._drop_paths(e.mimeData()):
            e.setDropAction(Qt.CopyAction)
            e.accept()
        else:
            e.ignore()

    def dropEvent(self, e):
        if self._drop_paths(e.mimeData()):
            e.setDropAction(Qt.CopyAction)
            e.accept()
            self._handle_drop(e.mimeData())
        else:
            e.ignore()

    def _layout_edges(self):
        w, h = self.width(), self.height()
        m = MARGIN
        c = CORNER
        for edge, g in self._edges:
            if edge == Qt.LeftEdge:
                g.setGeometry(0, c, m, h - 2 * c)
            elif edge == Qt.RightEdge:
                g.setGeometry(w - m, c, m, h - 2 * c)
            elif edge == Qt.TopEdge:
                g.setGeometry(c, 0, w - 2 * c, m)
            elif edge == Qt.BottomEdge:
                g.setGeometry(c, h - m, w - 2 * c, m)
            elif edge == (Qt.LeftEdge | Qt.TopEdge):
                g.setGeometry(0, 0, c, c)
            elif edge == (Qt.RightEdge | Qt.TopEdge):
                g.setGeometry(w - c, 0, c, c)
            elif edge == (Qt.LeftEdge | Qt.BottomEdge):
                g.setGeometry(0, h - c, c, c)
            elif edge == (Qt.RightEdge | Qt.BottomEdge):
                g.setGeometry(w - c, h - c, c, c)
        vis = not self.isMaximized()
        for _, g in self._edges:
            g.setVisible(vis)

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == e.Type.ActivationChange:
            # 从资源管理器切回来时，按磁盘实际状态同步「已安装 / 下载」按钮
            if self.isActiveWindow() and not getattr(self, "_act_busy", False):
                self._act_busy = True
                QTimer.singleShot(120, self._after_activate)
        if e.type() == e.Type.WindowStateChange:
            maximized = self.isMaximized()
            self.titlebar.set_maximized(maximized)
            self._layout_edges()
            # 最小化再还原：内容不能还停在上次藏着的状态
            hide = getattr(self, "auto_hide", None)
            if hide is not None and not self.isMinimized():
                try:
                    hide.reset()
                except Exception:
                    pass
            # 全屏时 DWM 圆角会在四角留缺口 + 背景需立即重建
            winacrylic.set_round_corners(self, not maximized)
            self._regen_bg()
            self.update()
            if maximized:
                # 无边框窗口最大化时贴满可用区域（避免盖住任务栏）
                from PySide6.QtGui import QGuiApplication
                scr = self.screen() or QGuiApplication.primaryScreen()
                self.setGeometry(scr.availableGeometry())

    def _regen_bg_after_resize(self):
        """防抖到点：按新尺寸重算底图（含磨砂），再刷一帧。

        ⚠️ `_regen_bg()` 只改 `self._bg`，**不会自己触发重绘**。以前这里只连了
        `_regen_bg`，结果拖完窗口得等下一次偶然的 paint（最小化还原 / 天空定时器）
        才看到新背景 —— 这就是「缩放时背景不跟着渲染」的根因。
        """
        try:
            self._regen_bg()
        except Exception:
            pass
        self.update()

    def _regen_bg(self, blur=True):
        from PySide6.QtCore import QRect
        w, h = max(self.width(), 1), max(self.height(), 1)
        img = getattr(self, "_bg_img", None)
        if img is None or img.width() != w or img.height() != h:
            img = QImage(w, h, QImage.Format_ARGB32)
            self._bg_img = img
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(0.97)
        # 有背景图 -> 照片 + 现实时间调色；没有 -> 退回程序化天空（打包漏带也不会白屏）
        if self.wallpaper.active():
            self.wallpaper.render(p, QRectF(0, 0, w, h))
        else:
            theme.paint_background(p, QRectF(0, 0, w, h))
        p.end()
        self._bg = img
        if not blur:
            return          # 淡入过程中只重绘底图，模糊版等淡完再算（那步很贵）
        # 模糊版（磨砂卡取景用）：降采样再放大 = 廉价模糊。
        # 原来走 QGraphicsScene + QGraphicsBlurEffect：每次都要新建一个 scene，
        # effect 的所有权在 Qt（item 接管）与 Python 之间不清不楚，scene 局部析构时
        # 存在重复释放的风险面；而且 1080p 实测要 54ms。降到 1/12 再平滑放大，
        # 视觉接近 26px 高斯，成本只要几毫秒，也不再碰 QGraphicsScene。
        sw, sh = max(1, w // 12), max(1, h // 12)
        small = img.scaled(sw, sh, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self._bg_blur = small.scaled(w, h, Qt.IgnoreAspectRatio,
                                     Qt.SmoothTransformation)

    def blurred_image(self):
        return self._bg_blur

    def _sky_tick(self):
        """定时重建背景：天空随现实时间渐变、云漂移；卡片磨砂取景同步刷新"""
        try:
            self._regen_bg()
            self.titlebar.refresh_ink()      # 白天黑字 / 夜里白字
            self.update()
        except Exception:
            pass

    # -------------------------------------------------------- 背景幻灯片
    def _slide_next(self):
        """到点换下一张背景，并启动淡入（33ms 一步）"""
        try:
            if not self.wallpaper.active() or not self.wallpaper.enabled:
                return
            if self.wallpaper.advance(max(self.width(), 1), max(self.height(), 1)):
                self._fade_timer.start()
                self._fade_step()       # 立刻画第一帧，别等 33ms 后
        except Exception:
            pass

    def _fade_step(self):
        """推进淡入：只重绘底图，模糊版等淡完再算（模糊那步很贵）"""
        try:
            # 模态面板（详情 / 依赖）盖在上面时什么都不画：主窗在它底下根本看不见，
            # 重绘只会白占主线程，还会把 GIL 让给工作线程去触发 GC —— 第 52 轮那次
            # access violation 就发生在「依赖面板开着 + 淡入还在 33ms 一帧重绘」的缝隙里。
            if QApplication.activeModalWidget() is not None:
                if self._fade_timer.interval() != 200:
                    self._fade_timer.setInterval(200)   # 降频空转，关掉立刻接着画
                return
            if self._fade_timer.interval() != 33:
                self._fade_timer.setInterval(33)
            alive = self.wallpaper.step(self._fade_timer.interval() / 1000.0)
            self._regen_bg(blur=not alive)
            self.update()
            if not alive:
                self._fade_timer.stop()
        except Exception:
            self._fade_timer.stop()

    def _restart_slide_timer(self):
        """按当前设置重置幻灯片计时（关掉时停在玩家选的那张）"""
        w, h = max(self.width(), 1), max(self.height(), 1)
        if self.wallpaper.active() and self.wallpaper.enabled:
            self._slide_timer.setInterval(max(3, int(self.wallpaper.secs)) * 1000)
            self._slide_timer.start()
        else:
            self._slide_timer.stop()
            if self.wallpaper.active():
                self.wallpaper.jump(self.wallpaper.pick, w, h)

    def apply_wallpaper(self):
        """设置里改了「幻灯片」或背景图后调用：重读配置 + 重置计时 + 立刻重绘。

        第一百轮：设置每次按「保存」都会**无条件**走到这里，而 `_regen_bg()`
        要重画整屏 + 两次缩放（实测 86ms）；玩家只改个「关闭弹窗」也白挨一下。
        按背景配置签名比对，没变就只同步计时，不重画。
        """
        sig = (CONFIG.get("slideshow", True), CONFIG.get("slide_secs"),
               CONFIG.get("bg_pick"))
        same = (getattr(self, "_wp_sig", None) == sig
                and getattr(self, "_bg", None) is not None)
        self._wp_sig = sig
        try:
            self._fade_timer.stop()
        except Exception:
            pass
        try:
            self.wallpaper.sync()
        except Exception:
            pass
        self._restart_slide_timer()
        if not same:
            self._regen_bg()
            self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        r = 16 if not self.isMaximized() else 0
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), r, r)
        p.setClipPath(path)
        if self._bg:
            # 拖动改大窗口时，底图还是上一帧尺寸的（重建有 60ms 防抖）——
            # 这时不能只 drawImage(0,0)（右下角会留空白/透明），先把旧图拉伸铺满，
            # 防抖到点后再换成按新尺寸重新渲染的清晰版。看起来就是「一直在跟手」。
            if (self._bg.width() != self.width()
                    or self._bg.height() != self.height()):
                p.setRenderHint(QPainter.SmoothPixmapTransform)
                p.drawImage(self.rect(), self._bg)
            else:
                p.drawImage(0, 0, self._bg)
        p.setClipping(False)
        p.setPen(QColor(255, 255, 255, 130))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)


def _splash_enabled():
    """开场动画开关：测试 / 排障可以用 MCASSIST_NO_SPLASH=1 跳过"""
    if os.environ.get("MCASSIST_NO_SPLASH"):
        return False
    try:
        import importlib
        importlib.import_module("app.ui.splash")
    except Exception:
        return False                     # 多媒体组件缺失：直接进软件
    return True


def main():
    global _TOUR_AUTOSTART
    _TOUR_AUTOSTART = True          # 真·启动：允许引导教程自动出现
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("WorkBuddy")
    # 第一百一十六轮：界面简繁切换。必须在建任何控件之前装好运行时文本拦截，
    # 否则构造期文本会漏登记。出问题内部会降级为简体，不影响启动。
    try:
        from .ui import i18n_ui
        i18n_ui.install(app)
    except Exception:
        pass
    # 第八十二轮：野窗口守卫。本软件所有窗口都是无边框自绘的，桌面上只要出现一块
    # **带 Windows 原生标题栏**的顶层控件就一定是意外产物（玩家录屏里那一列列
    # 「空白『八桂启动器』小窗」）。守卫一露面就摁回去，见 `ui/winwatch.py`。
    # 位置尽量靠前：排在后面的话，前头建界面时漏出来的那几块就已经被看见了。
    try:
        from .ui import winwatch
        winwatch.install(app)
    except Exception:                                    # noqa: BLE001
        pass
    # 先把崩溃留痕挂上：原生崩溃（访问冲突 / qFatal）只有 faulthandler 能写出栈，
    # 不挂的话「闪退」永远查不出死在哪个函数。日志：<数据目录>\logs\crash.log
    try:
        from .core import crashlog
        crashlog.install()
    except Exception:
        pass
    # GC 调优必须赶在**建任何控件之前**：gc.freeze() 会把此刻还活着的对象冻进
    # 永久代，之后不再扫描。放晚了就把整棵界面树冻住 = 泄漏。
    # 顺带把阈值调大 —— 游戏多的时候界面一秒建几十行，默认阈值会反复全表扫。
    try:
        from .core import gcguard
        gcguard.tune()
    except Exception:
        pass
    f = QFont(theme.FONT, 10)
    app.setFont(f)
    app.setStyleSheet(theme.qss())
    theme.apply_light_tooltip(app)
    # 自绘提示框（白底黑字）：系统 QTipLabel 会被全局 QLabel 规则弄成透明 → 黑底灰字
    try:
        from .ui import tipbox
        tipbox.install(app)
    except Exception:
        pass
    # 开场页关掉的那一瞬间还没有主窗口，别让 Qt 以为「最后一个窗口关了」就退出
    app.setQuitOnLastWindowClosed(False)

    # 单实例（第五十六轮）：同一个用户只留一个启动器窗口。
    # 第二份进程连上已经在跑的那个、让它把窗口叫出来（顺带闪任务栏），然后自己退出。
    # 位置很关键：必须放在目录迁移 / 整合包瘦身之前 —— 第二份进程绝不能去跑
    # split_legacy() / migrate_pack_shells()，那是在动玩家真实数据。
    guard = None
    try:
        from .core.single import SingleGuard
        guard = SingleGuard()
        if not guard.try_acquire():
            return 0
    except Exception:                                    # noqa: BLE001
        guard = None                                     # 守卫坏了也不能拦着启动
    # 重启自己时要把这把锁先放掉（见 MainWindow.restart_app），所以留一份全局引用
    global _SINGLE_GUARD
    _SINGLE_GUARD = guard

    # 第九十八轮：总路径（Osmanthus flower）搬家 —— 设置里改了位置后在这里落地。
    # 必须排在 split_legacy / 读任何游戏目录**之前**：那两步都是按旧路径算的，
    # 先搬完再走，整轮启动才看得到新位置。
    # 同盘是改名（瞬时）；跨盘要拷，这时主窗口还没建出来 —— 第九十九轮起改用
    # **系统通知**（托盘 + 通知中心）告知进度，不再往屏幕正中飘一张卡片。
    root_note = ""
    note_ui = None
    try:
        from .core import relocate as _rl
        _job = _rl.pending()
        if _job:
            if not _rl.same_volume(_job.get("from", ""), _job.get("to", "")):
                try:
                    note_ui = _MoveNotify()
                except Exception:                        # noqa: BLE001
                    note_ui = None
                if note_ui is not None:
                    note_ui.say(
                        "正在搬 Osmanthus flower",
                        f"从 {_job.get('from')} 搬到 {_job.get('to')}。"
                        "跨盘要拷一会儿，关掉这个通知也不会中断，搬完再通知你。",
                        14000)
            try:
                _ok, _msg = _rl.run_pending(
                    progress=(note_ui.update_progress
                              if note_ui is not None else None))
            except Exception:                            # noqa: BLE001
                _ok, _msg = False, ""
            if _msg:
                root_note = _msg
                if note_ui is not None:
                    note_ui.say("总路径搬家结束", _msg, 12000)
            if note_ui is not None:
                note_ui.close_now()
    except Exception:                                    # noqa: BLE001
        root_note = ""
    globals()["_MOVE_NOTIFY"] = note_ui       # 托盘图标别被 GC 掉

    # 第四十五轮：把老的单一 .minecraft 拆成「应用 / 官方」两条路径。**只跑一次**，
    # 同卷改名（瞬时）或目录联接（零拷贝），异常全吞，绝不拦着软件启动。
    split_note = ""
    try:
        from .core import paths
        split_note = paths.split_legacy() or ""
        # 第四十六轮：清掉上一版写死的 download_target，改回「跟随独立启动」的默认
        paths.init_target_rule()
    except Exception:
        split_note = ""
    if root_note:
        split_note = (root_note + "；" + split_note) if split_note else root_note
    pack_note = ""
    try:
        # 第四十八轮：老整合包的「胖壳」档案瘦身 + 补 <包名>.jar（只跑一次）。
        # 胖壳会让启动器把父版本参数拼两遍 → 两个 --gameDir → 游戏窗口一闪不出。
        from .core import installer as _inst
        pack_note = _inst.migrate_pack_shells() or ""
    except Exception:
        pack_note = ""
    if pack_note:
        split_note = (split_note + "；" + pack_note) if split_note else pack_note

    # 第九十九轮一次性自愈：1.6.8 ~ 1.6.9 那次「总路径搬家」把导入登记表里的
    # name / mc / version_id / kind 全切坏了（见 paths._looks_abs_path 的注释），
    # 于是纯版本也挤进「整合包」那一栏、好几行还长着一模一样的名字。
    # 这里照磁盘把能认回来的字段重建一遍，并把目录已经不在的幽灵记录清掉。
    try:
        from .core import installer as _inst2
        _healed = _inst2.heal_imported()
        if _healed:
            note = f"已修好 {_healed} 条被搬坏的导入记录"
            split_note = (split_note + "；" + note) if split_note else note
    except Exception:                                    # noqa: BLE001
        pass

    # 第一百零一轮：上面那个只修 `imported` 的一部分字段，而且是一次性标记
    # （玩家这份配置早置过 True）。「已导入的游戏」里五个文件夹同名，坏的是
    # `added_groups[gid].name`，加上 `added_dirs` 的 name/kind 和 `imported`
    # 的 time/profile_id —— 这些它一个都没碰。这里补修，每次启动都检测。
    try:
        from .core import installer as _inst3
        _more = _inst3.heal_rebased_metadata()
        if _more:
            note = f"已修好 {_more} 处被搬坏的已导入名称/分类"
            split_note = (split_note + "；" + note) if split_note else note
    except Exception:                                    # noqa: BLE001
        pass

    # 第一百零九轮+：把还留在 .minecraft/versions 里的加载器版本目录（neoforge-xxx /
    # forge-xxx / fabric-xxx / quilt-xxx）挪到全局 loaders/，让它们从「游戏管理 / 选择要
    # 启动的游戏」里消失。幂等、异常全吞，绝不拦着软件启动。
    try:
        from .core import loader as _ld
        _ld.migrate_existing_loaders()
    except Exception:                                    # noqa: BLE001
        pass

    # 第六十七轮补丁：新版本被装到别的目录时，新的 .minecraft 是空的，玩家以前下的
    # 游戏还在旧目录里 → 游戏管理一片空白。这里把应用路径认回那份有游戏的目录
    # （只改配置，不搬一个字节）。**放在 split_legacy 之后**：拆分是只跑一次的旧逻辑，
    # 别让它先把认回来的目录再拆一遍。
    try:
        from .core import paths as _paths
        moved, now_dir, old_dir = _paths.recover_app_game_dir()
        if moved:
            note = f"已把游戏目录认回 {now_dir}（原来指向的空目录：{old_dir}）"
            split_note = (split_note + "；" + note) if split_note else note
    except Exception:                                    # noqa: BLE001
        pass

    # 第六十七轮：概览缓存是「打开设置期间别卡」的临时落脚点，正常关窗会自己清；
    # 上次若是被强杀 / 蓝屏，文件会留在 <安装目录>\cache 里 —— 开机顺手清一遍。
    # 第六十七轮补丁：清完把 cache\import_cache\app_cache\ 三层建出来并写一份说明。
    # 以前这三个目录要等第一次展开概览才出现，玩家在安装目录里翻不到，
    # 会以为「缓存功能没生效」。
    try:
        from .core import gearcache as _gc
        _gc.reset()
    except Exception:                                    # noqa: BLE001
        pass

    holder = {}

    # 第二份进程来敲门时要把窗口叫回来。主窗还没建好（开场动画阶段）就记一笔，
    # 等 boot() 里窗口出来再叫 —— 否则玩家点了图标半天没动静。
    want_front = {"pending": False}

    def bring_to_front():
        win = holder.get("win")
        if win is None:
            want_front["pending"] = True
            return
        win.raise_window()

    if guard is not None:
        try:
            guard.activated.connect(bring_to_front)
            app.aboutToQuit.connect(guard.release)
        except Exception:                        # noqa: BLE001
            pass

    def save_win():
        win = holder.get("win")
        if win is None:
            return
        try:
            CONFIG.set("window", {"x": win.x(), "y": win.y(),
                                  "w": win.width(), "h": win.height(),
                                  "max": win.isMaximized()})
        except Exception:
            pass

    def make_window(defer):
        """建主窗口 + 摆位置 + 预渲染背景（不含 show / 不含淡入）"""
        win = MainWindow(defer_show=defer)
        holder["win"] = win
        saved_win = CONFIG.get("window")
        if saved_win:
            try:
                win.setGeometry(saved_win["x"], saved_win["y"],
                                saved_win["w"], saved_win["h"])
                if saved_win.get("max"):
                    if defer:
                        win._pending_max = True     # 预热阶段不能 show
                    else:
                        win.showMaximized()
            except Exception:
                pass
        # 更新后残留屏外位置 / 显示器变更 -> 拉回可见屏幕
        if not win.isMaximized():
            win.ensure_on_screen()
        # 先把最终尺寸的背景（含磨砂）算出来：否则 resizeEvent 的 90ms 防抖重绘
        # 会在淡入中途「突然变清晰」，一眼就看得出破绽。
        try:
            win._regen_bg()
        except Exception:
            pass
        return win

    def _late_front():
        """开场动画期间第二个实例敲过门：等窗口真出来了再把它叫到前面"""
        if not want_front["pending"]:
            return
        want_front["pending"] = False
        QTimer.singleShot(420, bring_to_front)

    def _tell_split(win):
        """第一次拆目录后告诉玩家一声（不打扰就用 Toast，不弹窗）"""
        if not split_note:
            return
        QTimer.singleShot(900, lambda: Toast.show_toast(win, split_note, 6000, "info"))

    app.aboutToQuit.connect(save_win)

    def warn_prev_crash():
        """上次是不是崩的？崩过就提示一句日志在哪 —— 玩家能直接把文件发过来。"""
        try:
            from .core import crashlog
            if not crashlog.previous_fatal():
                return
            from .ui.widgets import Toast
            Toast.show_toast(holder.get("win"),
                             "上次异常退出，日志已记录：%s" % crashlog.log_path(),
                             ms=6500, kind="error")
        except Exception:
            pass

    # 第八十四轮：欢迎页已合并进主窗口（覆盖层形态，由 MainWindow 内部接管），
    # 这里不再创建独立开场窗口，也不再跨窗口对淡。
    win = make_window(defer=False)
    app.setQuitOnLastWindowClosed(True)
    QTimer.singleShot(1800, warn_prev_crash)
    _tell_split(win)
    _late_front()

    code = app.exec()
    # 退出收尾：事件循环已返回，先把窗口位置/配置落盘再正常结束进程。
    # 注意不要用 os._exit 硬杀——worker 线程还在收尾，硬杀会让进程以非零码
    # （段错误）结束，自然退出才是 0。
    for fn in (save_win, CONFIG.save):
        try:
            fn()
        except Exception:
            pass
    sys.exit(code or 0)


if __name__ == "__main__":
    main()
