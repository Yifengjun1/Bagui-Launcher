# -*- coding: utf-8 -*-
"""「启动」按钮：官方启动器 / 独立启动 两种方式，右侧小三角切换。

为什么单独一个控件：侧栏和主页各有一个启动按钮，两处的文案、灰态、
小三角显示条件必须完全一致（设置里关掉独立启动 → 两处都不该有三角）。
放到一处，就不会出现「主页能独立启动、侧栏不能」的错位。

规则（对应需求第 2~4 条）：
  · 设置里「独立启动」关掉 → 不显示小三角，永远只能启动官方启动器；
  · 小三角点开有两项：官方启动器 / 独立启动，当前方式打勾；
  · 切到独立启动后，按钮文案变成「启动 <游戏名>」；
    还没在「游戏管理」里选游戏时是「选择游戏启动」，且呈灰色不可点。
"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QSizePolicy

import threading
import time

from ..core import gamelaunch
from ..core.asyncio_qt import run_async
from .widgets import MiniMenu, SplitPill, Toast


# 启动准备阶段的进度文本：工作线程写、界面线程读。
# 规矩还是第 52 轮那条 —— 进度回调**绝不碰 QWidget**，界面自己轮询。
_STAGE = {"text": "", "ts": 0.0}
_STAGE_LOCK = threading.Lock()


def _stage_sink(text):
    with _STAGE_LOCK:
        _STAGE["text"] = str(text or "")
        _STAGE["ts"] = time.time()


def _stage_text():
    with _STAGE_LOCK:
        return _STAGE["text"]


class LaunchSplitButton(SplitPill):
    def __init__(self, window, parent=None, height=46, kind="accent",
                 official_text="启动官方启动器", empty_text="选择游戏启动",
                 prefix="启动 "):
        super().__init__(official_text, kind, icon="play", parent=parent, height=height)
        self.window_ref = window
        self.official_text = official_text
        self.empty_text = empty_text
        self.prefix = prefix
        self._menu = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.primary.connect(self._run)
        self.arrow.connect(self._open_menu)
        self.blocked.connect(self._blocked)
        self.refresh()

    # ------------------------------------------------------------ 状态
    def refresh(self):
        """按当前配置重算文案 / 灰态 / 三角（设置改了、换了游戏都要调）"""
        enabled = gamelaunch.standalone_enabled()
        mode = gamelaunch.launch_mode()
        self.set_arrow_visible(enabled)

        if mode == "official":
            self.setText(self.official_text)
            self.set_primary_enabled(True)
            self.setToolTip("启动官方启动器" +
                            ("　·　点右侧小三角可切换为独立启动" if enabled else ""))
        else:
            t = gamelaunch.launch_target()
            ready, why = gamelaunch.target_ready() if t else (False, "")
            if t and ready:
                self.setText(self.prefix + str(t.get("name") or t.get("version_id")))
                self.set_primary_enabled(True)
                self.setToolTip(f"直接启动「{t.get('name')}」，不经过官方启动器")
            else:
                self.setText(self.empty_text)
                self.set_primary_enabled(False)
                self.setToolTip(why or "先到「游戏管理」里点一下要启动的游戏")
        self.set_arrow_up(bool(self._menu is not None and self._menu.isVisible()))
        self.updateGeometry()
        self.update()

    # ------------------------------------------------------------ 主按钮
    def _run(self):
        if gamelaunch.launch_mode() == "standalone":
            self._launch_game()
        else:
            from .widgets import launch_official_async
            launch_official_async(self.window_ref)

    def _launch_game(self):
        win = self.window_ref
        if gamelaunch.is_running():
            Toast.show_toast(win, f"「{gamelaunch.running_name()}」正在运行，别急",
                             ms=2600, kind="info")
            return
        Toast.show_toast(win, "正在启动游戏（独立启动，不经过官方启动器）…", ms=1800)
        self._start_slow_hint()

        def done(res):
            self._stop_slow_hint()
            ok, msg = res if isinstance(res, tuple) and len(res) == 2 else (False, str(res))
            Toast.show_toast(win, msg, ms=3600 if ok else 6000,
                             kind="success" if ok else "error")
            if ok:
                hook = getattr(win, "on_game_launched", None)
                if hook:
                    try:
                        hook(msg)
                    except Exception:
                        pass

        def err(e):
            self._stop_slow_hint()
            Toast.show_toast(win, f"独立启动失败：{e}", ms=6000, kind="error")

        # progress 必须传进去：干净设备首次启动要补几百 MB 的库和资源
        # （新版 MC 的 assets 就有 460 MB），不给进度玩家只会以为卡死了。
        run_async(gamelaunch.launch, progress=_stage_sink,
                  on_done=done, on_err=err)

    # ------------------------------------------------------------ 慢启动提示
    def _start_slow_hint(self):
        """补库 / 补资源可能几十秒到几分钟 —— 把实时进度钉在屏幕上。

        第五十九轮之前这里是个 singleShot，3.6 秒后弹一句「有进度就别关」，
        可那时压根没有进度可看，等于骗人。现在改成周期轮询进度文本，
        用 Toast.update_text 原地改字，不重播淡入。
        """
        win = self.window_ref
        _stage_sink("")
        if getattr(self, "_slow", None) is None:
            self._slow = QTimer(self)
            self._slow.setInterval(1100)
            self._slow.timeout.connect(self._tick_slow)
        self._slow.start()
        return win

    def _tick_slow(self):
        txt = _stage_text()
        if not txt:
            txt = "正在准备启动…（首次可能要补库和资源，别急着关）"
        try:
            Toast.update_text(self.window_ref, txt, ms=9000, kind="info")
        except Exception:
            pass

    def _stop_slow_hint(self):
        t = getattr(self, "_slow", None)
        if t is not None:
            t.stop()

    def _blocked(self):
        """没选游戏就点主按钮"""
        Toast.show_toast(self.window_ref,
                         "先到「游戏管理」里点一下要启动的游戏，再来独立启动",
                         ms=3800, kind="info")

    # ------------------------------------------------------------ 小三角菜单
    def _open_menu(self):
        if self._menu is not None and self._menu.isVisible():
            self._menu.close()
            self.set_arrow_up(False)
            return
        mode = gamelaunch.launch_mode()
        t = gamelaunch.launch_target()
        name = str((t or {}).get("name") or "")
        rows = [
            {"label": "启动官方启动器",
             "sub": "打开 Minecraft Launcher，在里面选档案启动",
             "checked": mode == "official", "cb": lambda: self._switch("official")},
            {"label": "独立启动",
             "sub": (f"直接启动：{name}" if name else "还没选游戏 · 到「游戏管理」里选"),
             "checked": mode == "standalone",
             "cb": lambda: self._switch("standalone")},
        ]
        self._menu = MiniMenu(rows, parent=self)
        self._menu.on_closed = lambda: self.set_arrow_up(False)
        self._menu.show_at(self)
        self.set_arrow_up(True)

    def _switch(self, mode):
        gamelaunch.set_launch_mode(mode)
        self.set_arrow_up(False)
        win = self.window_ref
        if mode == "standalone" and not gamelaunch.launch_target():
            Toast.show_toast(win, "已切到独立启动：先到「游戏管理」里选一个要启动的游戏",
                             ms=4200, kind="info")
        else:
            Toast.show_toast(win, "已切换到独立启动" if mode == "standalone"
                             else "已切换回官方启动器", ms=2200)
        refresh = getattr(win, "refresh_launch_ui", None)
        if refresh:
            try:
                refresh()
            except Exception:
                pass
        else:
            self.refresh()
