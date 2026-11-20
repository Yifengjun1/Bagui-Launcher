# -*- coding: utf-8 -*-
"""依赖明细列表：进度弹窗与「下载详情」弹窗共用。

只展示 Task.details（纯数据），由主线程轮询刷新，工作线程不碰任何控件。
大整合包可能有 300+ 条依赖，所以这里是**虚拟列表**：只建视口装得下的那几行，
滚动/刷新只改「这一行显示第几条」，不重建控件（详见 `TaskDetailList` 的说明）。
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from ..core import tasks as task_mod
from . import theme

STATUS_META = {
    "wait": ("○", "#9aa4b0", "等待"),
    "run": ("◍", "#2f7cf6", "下载中"),
    "done": ("●", "#2e9e5b", "完成"),
    "fail": ("×", "#d9534f", "失败"),
    "skip": ("–", "#9aa4b0", "跳过"),
}

# 行内三个 label 的字体只用一份 QFont（**不要**给 host 挂样式表）。
#
# 踩过的坑：一开始把 `QLabel#td_mk{font-size:10px;…}` 挂在 host 上，靠 objectName
# 命中 —— 结果每往布局里塞一个 label，Qt 都要拿父控件的样式表给它做一遍
# polish，303 行 = 1200 多次 CSS 解析：光「塞进布局」就 74ms、随后的布局激活
# 500ms（实测）。改成 QFont 之后这两项掉到 5ms / 280ms。
def _row_font():
    f = QFont(theme.FONT)
    f.setPixelSize(10)
    return f


class TaskDetailList(QWidget):
    """可滚动依赖明细：等待 / 下载中 / 完成 / 失败 一目了然

    **虚拟列表**（第七十四轮）：整合包动辄 300+ 条依赖，以前是「一条一个
    QWidget」全建出来 —— 打开进度窗那一刻要建 1200 多个控件（实测首帧
    777ms），之后每 300ms 轮询还要给每一行刷一遍样式表（每拍 125ms，
    折算主线程占用 41%，玩家感受就是「下载整合包时整只卡住」）。

    现在只建**视口装得下的那么几行**（约 20 行，多备 3 行防边缘闪烁），
    滚动时改的是「这一行显示第几条数据」，而不是「再建一批控件」：

      | 指标                        | 改前    | 改后   |
      |-----------------------------|---------|--------|
      | 首帧（303 条）              | 777 ms  | ~10 ms |
      | 稳态每拍（300ms 轮询）      | 125 ms  | <1 ms  |

    上下各放一个占位控件撑出总高度，滚动条长度照样是 303 行的手感。
    """

    ROW_H = 15          # 单行高度（写死才好算滚动位置）
    ROW_GAP = 2         # 行间距
    POOL_EXTRA = 3      # 视口外多备几行，滚起来不闪

    def __init__(self, parent=None, height=132):
        super().__init__(parent)
        self._sig = None
        self._max_h = height      # 滚动区上限，行数少时自动收紧
        self._data = []           # 最近一次的明细数据（纯 dict 列表）
        self._pool = []           # [(row, mark, name, st)]，只建视口那么多
        self._tint = []           # 每行当前颜色（只在变的时候才改调色板）
        self._first = -1          # 当前窗口第一条对应的数据下标
        self._follow = True       # 自动跟着「正在下载」那一行走（用户自己滚过就不跟了）
        self._setting = False
        self._font = _row_font()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        self.head = QLabel("")
        self.head.setStyleSheet("color:#4c5866;font-size:11px;font-weight:600;")
        outer.addWidget(self.head)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(
            "QScrollArea{border:none;background:rgba(31,39,51,10);border-radius:9px;}")
        self.host = QWidget()
        self.host.setObjectName("td_host")
        self.lay = QVBoxLayout(self.host)
        self.lay.setContentsMargins(8, 6, 6, 6)
        self.lay.setSpacing(self.ROW_GAP)
        self._top = QWidget()
        self._bottom = QWidget()
        for w in (self._top, self._bottom):
            w.setFixedHeight(0)
            w.setStyleSheet("background:transparent;")
        self.lay.addWidget(self._top)
        self.lay.addWidget(self._bottom)
        self.lay.addStretch(1)
        self.scroll.setWidget(self.host)
        self.scroll.setFixedHeight(height)
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)
        outer.addWidget(self.scroll)
        self.setVisible(False)

    # ---------------- 对外 ----------------
    def update_from(self, task):
        rows = getattr(task, "details", None) or []
        self.setVisible(bool(rows))
        if not rows:
            if self._data:
                self._clear()
            self._sig = None
            return
        done = sum(1 for d in rows if d.get("status") in ("done", "fail", "skip"))
        total = len(rows)
        failed = sum(1 for d in rows if d.get("status") == "fail")
        head = f"依赖明细　{done} / {total}"
        if total:
            head += f"　{int(done * 100 / total)}%"
        if failed:
            head += f"　（{failed} 个失败）"
        # 第一百一十六轮：按简体原文比较，避免繁体显示下反复误判「变了」
        from . import i18n_ui as _iu
        if _iu.original_text(self.head) != head:
            self.head.setText(head)

        active = -1
        for i, d in enumerate(rows):
            if d.get("status") == "run":
                active = i
                break
        # 明细里的 note 常是「45%」「3.2/8.1 MB」这种实时进度：不进签名的话
        # 状态没变就整表不刷新，看上去又是一张定格照片。
        sig = (total, done, failed, active,
               tuple(str(d.get("note") or "") for d in rows))
        if sig == self._sig and self._pool:
            return
        first_time = (self._sig is None)
        self._sig = sig
        self._data = rows
        if first_time:
            self.set_scroll_top(0)
        self._fit_height(total)
        self._render(active)

    def set_scroll_top(self, value):
        """程序自己滚（跟随时用）；不把「用户滚过」算进去"""
        self._setting = True
        try:
            self.scroll.verticalScrollBar().setValue(int(value))
        finally:
            self._setting = False

    # ---------------- 内部 ----------------
    @property
    def STRIDE(self):
        return self.ROW_H + self.ROW_GAP

    def _fit_height(self, count):
        """行数少时收紧滚动区，避免弹窗里出现大片空白。"""
        want = min(self._max_h, 14 + self.STRIDE * max(1, count))
        if want != self.scroll.height():
            self.scroll.setFixedHeight(want)

    def _on_scroll(self, _v=0):
        if self._setting:
            return
        # 用户自己拖了滚动条 / 滚了滚轮 → 不再自动跳回「正在下载」那一行
        self._follow = False
        self._render(-1)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._render(-1)

    def _clear(self):
        for row, _m, _n, _s in self._pool:
            row.hide()              # 第八十二轮：先 hide，别让排队 show 把它变窗口
            row.setParent(None)
            row.deleteLater()
        self._pool = []
        self._tint = []
        self._data = []
        self._first = -1
        self._top.setFixedHeight(0)
        self._bottom.setFixedHeight(0)

    def _span(self, active):
        """算出「该显示哪一段」：先看视口能放几行，再按滚动位置/跟随状态定起点"""
        n = len(self._data)
        if n <= 0:
            return 0, 0
        view_h = max(self.ROW_H, self.scroll.viewport().height())
        fit = max(1, (view_h + self.ROW_GAP) // self.STRIDE) + self.POOL_EXTRA
        fit = min(fit, n)
        if self._follow and active >= 0:
            first = max(0, active - fit + 2)       # 让「下载中」那行靠近底部
        else:
            first = int(self.scroll.verticalScrollBar().value() // self.STRIDE)
        first = max(0, min(first, max(0, n - fit)))
        return first, min(n, first + fit)

    def _render(self, active):
        if not self._data:
            return
        # 先把滚动条范围撑到「全量」高度，否则 value() 一直被夹在旧范围内，
        # 滚动位置算出来是错的（虚拟列表最常见的坑）
        self._setup_scroll_range()
        first, last = self._span(active)
        if self._follow and active >= 0:
            self.set_scroll_top(first * self.STRIDE)
        want = max(0, last - first)
        while len(self._pool) < want:
            self._pool.append(self._make_row())
            self._tint.append("")
        for k in range(len(self._pool)):
            row = self._pool[k][0]
            if k < want:
                if not row.isVisible():
                    row.setVisible(True)
                self._bind(k, self._data[first + k])
            elif row.isVisible():
                row.setVisible(False)
        self._first = first
        self._top.setFixedHeight(first * self.STRIDE)
        self._bottom.setFixedHeight(max(0, (len(self._data) - last) * self.STRIDE))

    def _setup_scroll_range(self):
        """用上下占位撑出「全量」高度（滚动条长度才对）"""
        total = len(self._data) * self.STRIDE
        margins = self.lay.contentsMargins()
        need = total + margins.top() + margins.bottom() + 2
        if self.host.minimumHeight() != need:
            self.host.setMinimumHeight(need)

    def _make_row(self):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        row.setFixedHeight(self.ROW_H)
        mark = QLabel("○")
        mark.setObjectName("td_mk")
        mark.setFont(self._font)
        mark.setFixedWidth(12)
        h.addWidget(mark)

        name = QLabel("")
        name.setObjectName("td_nm")
        name.setFont(self._font)
        name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        h.addWidget(name, 1)

        st = QLabel("")
        st.setObjectName("td_st")
        st.setFont(self._font)
        st.setMinimumWidth(42)        # 「下载中」三个字不能被裁成「下载」
        h.addWidget(st)
        # 插在 [上占位] 和 [下占位] **之间**：布局顺序是
        # [top, 行…, bottom, stretch]，所以第 k 行的下标是 1+k = count()-2。
        # （写成 count()-1 会插到 bottom 后面，行全被顶到内容最底下 —— 表现为
        #   「明细一片空白、滚动条却很长」，踩过一次。）
        self.lay.insertWidget(max(1, self.lay.count() - 2), row)
        return (row, mark, name, st)

    def _bind(self, k, d):
        _row, mark, name, st = self._pool[k]
        sym, color, word = STATUS_META.get(str(d.get("status") or "wait"),
                                           STATUS_META["wait"])
        note = str(d.get("note") or "")
        # 第一百一十六轮：繁体显示下按简体原文比较，避免反复误判「变了」
        from . import i18n_ui as _iu
        if _iu.original_text(mark) != sym:
            mark.setText(sym)
        nm = str(d.get("name") or "")
        if _iu.original_text(name) != nm:
            name.setText(nm)
            name.setToolTip(nm)
        word2 = note or word
        if _iu.original_text(st) != word2:
            st.setText(word2)
            st.setToolTip(note or word)
        if self._tint[k] != color:
            self._tint[k] = color
            self._paint(mark, color)
            self._paint(st, color)

    @staticmethod
    def _paint(lb, color):
        """改文字颜色 —— 走调色板，不走样式表。

        `setStyleSheet` 每次都要重新解析 CSS + polish 控件，303 行轮询下来
        一拍要 100ms 以上；`setPalette` 只是改一份调色板再重绘，差两个数量级。
        """
        pal = lb.palette()
        pal.setColor(QPalette.WindowText, QColor(color))
        lb.setPalette(pal)


class TaskDetailDialog:
    """「下载详情」对话框（延迟导入避免 dialogs ↔ taskpanel 循环导入）。

    第四十四轮修的是**不同步**：以前进来时 `update_from(task)` 只调一次，
    弹窗里就是一张定格照片 —— 主界面进度在动、详情里纹丝不动。现在挂一个
    300ms 的轮询定时器，每拍从任务中心取一份新的 `copy()`（纯数据，工作线程
    不碰控件），进度条与百分比跟着主界面一起走；任务结束后再刷一次就停表。
    """

    POLL_MS = 300

    @staticmethod
    def open(parent, task):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QProgressBar

        from .dialogs import _DialogBase

        d = _DialogBase(parent, width=470, title=str(task.title or "下载详情"))

        head_row = QHBoxLayout()
        head_row.setSpacing(8)
        head = QLabel("")
        head.setStyleSheet("color:#5a6675;font-size:11px;")
        head_row.addWidget(head, 1)
        pct = QLabel("")
        pct.setStyleSheet("color:#2f7cf6;font-size:11px;font-weight:600;")
        head_row.addWidget(pct)
        d.body.addLayout(head_row)

        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setFixedHeight(8)
        bar.setTextVisible(False)
        d.body.addWidget(bar)

        lst = TaskDetailList(height=280)
        d.body.addWidget(lst)

        tip = QLabel("")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8a95a3;font-size:11px;")
        d.body.addWidget(tip)
        d.add_buttons([("关闭", "ghost", d.accept)])

        state = {"last": task}

        def render(t):
            head.setText(task_mod.STATUS_CN.get(t.status, t.status))
            frac = t.fraction
            if not t.total_bytes and not t.total_items and t.status == "running":
                bar.setRange(0, 0)                     # 未知总量：滚动条
            else:
                if bar.maximum() != 1000:
                    bar.setRange(0, 1000)
                bar.setValue(int(frac * 1000))
            color = {"done": "#2e9e5b", "failed": "#d9534f",
                     "cancelled": "#9aa4b0"}.get(t.status, "#2f7cf6")
            bar.setStyleSheet(
                "QProgressBar{background:rgba(31,39,51,32);border-radius:4px;}"
                f"QProgressBar::chunk{{background:{color};border-radius:4px;}}")
            if t.total_bytes or t.total_items:
                pct.setText(f"{int(round(frac * 100))}%")
            else:
                pct.setText("—")
            parts = []
            if t.total_bytes:
                parts.append(f"{task_mod.human_size(t.done_bytes)} / "
                             f"{task_mod.human_size(t.total_bytes)}")
            elif t.total_items:
                parts.append(f"{t.done_items} / {t.total_items}")
            sp = task_mod.human_speed(t.speed)
            if sp:
                parts.append(sp)
            eta = task_mod.human_eta(t.eta)
            if eta:
                parts.append(eta)
            if t.phase:
                parts.append(t.phase)
            tip.setText(t.message or " · ".join(parts))
            lst.update_from(t)

        render(task)
        d.adjustSize()

        timer = QTimer(d)
        timer.setInterval(TaskDetailDialog.POLL_MS)

        def tick():
            cur = task_mod.TASKS.get(task.tid)
            if cur is None:
                # 任务已被清理（面板那边 dismiss 掉）→ 保留最后一帧，停表
                state["last"] = state["last"]
                timer.stop()
                return
            snap = cur.copy()
            state["last"] = snap
            try:
                render(snap)
            except Exception:
                timer.stop()
                return
            if snap.status != "running":
                timer.stop()

        timer.timeout.connect(tick)
        timer.start()

        d.exec()
        timer.stop()
        return d

