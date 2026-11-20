# -*- coding: utf-8 -*-
"""主页下载任务面板：展示后台下载/安装进度（详情：阶段、大小、速度、剩余时间）。

任务数据来自 app/core/tasks.py 的全局任务中心（纯 Python，线程安全）。
本面板只在主线程轮询渲染，因此后台线程永远不会直接操作控件 —— 这也是
「下载途中取消导致闪退」的根治手段。
"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QProgressBar, QSizePolicy,
                               QVBoxLayout, QWidget)

from ..core import tasks as task_mod
from . import theme
from .widgets import IconButton, PillButton

STATUS_TEXT = {"done": "已完成", "failed": "失败", "cancelled": "已取消"}
KIND_TEXT = {"modpack": "整合包", "mod": "模组", "shader": "光影",
             "resourcepack": "材质包", "loader": "加载器", "vanilla": "原版"}


def _fmt_detail(t):
    if t.status == "failed":
        return t.message or "下载失败"
    if t.status == "cancelled":
        return t.message or "已取消"
    if t.status == "done":
        size = task_mod.human_size(t.total_bytes) if t.total_bytes else ""
        return (t.message or "已放入官方启动器") + (f" · {size}" if size else "")
    parts = []
    if t.total_bytes:
        parts.append(f"{task_mod.human_size(t.done_bytes)} / {task_mod.human_size(t.total_bytes)}")
    elif t.done_bytes:
        parts.append(task_mod.human_size(t.done_bytes))
    elif t.total_items:
        parts.append(f"{t.done_items} / {t.total_items}")
    sp = task_mod.human_speed(t.speed)
    if sp:
        parts.append(sp)
    eta = task_mod.human_eta(t.eta)
    if eta:
        parts.append(eta)
    if t.phase and t.phase not in ("已完成",):
        parts.append(t.phase)
    return " · ".join(parts) if parts else (t.message or "准备中…")


class TaskRow(QWidget):
    def __init__(self, task, panel):
        super().__init__(panel)
        self.tid = task.tid
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 6)
        lay.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(6)
        name = QLabel(task.title)
        f = QFont(theme.FONT)
        f.setPixelSize(12)
        f.setBold(True)
        name.setFont(f)
        name.setStyleSheet("color:#2a3442;")
        name.setToolTip(task.title)
        name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        head.addWidget(name, 1)

        self._task = task
        has_details = bool(getattr(task, "details", None))
        # 有「详情」按钮时不再显示类型角标：面板窄，两者挤在一起会互相压字
        if not has_details:
            badge = QLabel(KIND_TEXT.get(task.kind, ""))
            badge.setStyleSheet("color:#7b8794;font-size:10px;")
            head.addWidget(badge)
        else:
            more = PillButton("详情", "ghost")
            more.setFixedSize(46, 20)
            more.setToolTip("查看依赖下载明细")
            more.clicked.connect(self._open_detail)
            head.addWidget(more)

        if task.status == "running":
            b = IconButton("close", 20, 9)
            b.setToolTip("取消此任务")
            b.clicked.connect(lambda: panel.cancel(task.tid))
        else:
            b = IconButton("close", 20, 9)
            b.setToolTip("移除")
            b.clicked.connect(lambda: panel.dismiss(task.tid))
        head.addWidget(b)
        lay.addLayout(head)

        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setTextVisible(False)
        bar.setFixedHeight(6)
        color = {"done": "#2e9e5b", "failed": "#d9534f",
                 "cancelled": "#9aa4b0"}.get(task.status, "#2f7cf6")
        bar.setStyleSheet(
            "QProgressBar{background:rgba(31,39,51,32);border-radius:3px;}"
            f"QProgressBar::chunk{{background:{color};border-radius:3px;}}")
        bar.setValue(int(task.fraction * 1000))
        if task.status == "running" and not task.total_bytes and not task.total_items:
            bar.setRange(0, 0)          # 未知进度：滚动条
            bar.setFixedHeight(6)
        lay.addWidget(bar)

        detail = QLabel(_fmt_detail(task))
        detail.setStyleSheet(
            "color:#5a6675;font-size:10px;" if task.status == "running"
            else f"color:{ {'done': '#2e9e5b', 'failed': '#d9534f'}.get(task.status, '#7b8794') };font-size:10px;")
        detail.setWordWrap(False)
        detail.setToolTip(detail.text())
        lay.addWidget(detail)

    def _open_detail(self):
        from .taskdetail import TaskDetailDialog
        t = task_mod.TASKS.get(self.tid) or self._task
        snap = t.copy() if hasattr(t, "copy") else t
        TaskDetailDialog.open(self.window(), snap)


class TaskListPanel(QWidget):
    """放在主页按钮下方：有任务时出现，全部结束后自动隐藏。"""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window_ref = window
        self._sig = None
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 2, 0, 0)
        self._lay.setSpacing(6)
        self._header = QHBoxLayout()
        self._title = QLabel("下载任务")
        tf = QFont(theme.FONT)
        tf.setPixelSize(12)
        tf.setBold(True)
        self._title.setFont(tf)
        self._title.setStyleSheet("color:#2a3442;")
        self._header.addWidget(self._title)
        self._sub = QLabel("")
        self._sub.setStyleSheet("color:#7b8794;font-size:10px;")
        self._header.addWidget(self._sub)
        self._header.addStretch(1)
        self._btn_all = PillButton("全部取消", "ghost")
        self._btn_all.setFixedHeight(24)
        self._btn_all.setMinimumWidth(70)
        self._btn_all.clicked.connect(self._cancel_all)
        self._header.addWidget(self._btn_all)
        self._lay.addLayout(self._header)
        self._rows_host = QVBoxLayout()
        self._rows_host.setSpacing(2)
        self._lay.addLayout(self._rows_host)

        self._rows = {}
        self._timer = QTimer(self)
        self._timer.setInterval(380)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.setMaximumHeight(168)
        self.setVisible(False)

    # ---------------- 操作 ----------------
    def cancel(self, tid):
        task_mod.TASKS.cancel(tid)
        self._sig = None
        self._tick()

    def dismiss(self, tid):
        task_mod.TASKS.dismiss(tid)
        self._sig = None
        self._tick()

    def _cancel_all(self):
        for t in task_mod.TASKS.snapshot():
            if t.status == "running":
                task_mod.TASKS.cancel(t.tid)
        self._sig = None
        self._tick()

    # ---------------- 渲染 ----------------
    def _tick(self):
        snap = task_mod.TASKS.snapshot()
        task_mod.TASKS.clear_finished(8.0)
        # 只显示最近 3 条（再多会挤压主页布局）
        snap = sorted(snap, key=lambda t: t.created, reverse=True)[:3]
        if not snap:
            if self.isVisible():
                self.setVisible(False)
            self._clear_rows()
            self._sig = None
            return
        sig = tuple((t.tid, t.status, int(t.fraction * 100), t.phase,
                     t.done_bytes // 262144, int(t.speed // 65536),
                     t.done_items, t.total_items, t.message,
                     len(t.details or []),
                     sum(1 for d in (t.details or [])
                         if d.get("status") in ("done", "fail", "skip"))) for t in snap)
        if sig == self._sig:
            return
        self._sig = sig
        self._clear_rows()
        run = sum(1 for t in snap if t.status == "running")
        self._sub.setText(f"{run} 个进行中" if run else "全部完成")
        self._btn_all.setVisible(run > 0)
        for t in snap:
            row = TaskRow(t, self)
            self._rows_host.addWidget(row)
            self._rows[t.tid] = row
        self.setVisible(True)

    def _clear_rows(self):
        while self._rows_host.count():
            item = self._rows_host.takeAt(0)
            w = item.widget()
            if w:
                w.hide()            # 第八十二轮：先 hide，别让排队 show 把它变成窗口
                w.setParent(None)
                w.deleteLater()
        self._rows.clear()
