# -*- coding: utf-8 -*-
"""崩溃弹窗（第五十五轮，参照 PCLCE）：把「闪退」讲清楚，并给出能点的下一步。

四个按钮：自动补充 / 查看日志 / 分享日志 / 取消。
  · 自动补充：只在「缺前置」时可点 —— 去 Modrinth 按当前游戏版本 + 加载器
    下好缺的前置，直接放进 mods；
  · 查看日志：用系统默认程序打开日志 / 崩溃报告；
  · 分享日志：把日志整理成一份 .txt（部分 AI 不认 .log），复制路径并在资源
    管理器里选中，玩家拖给外置 AI 就行。
"""
import os

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QPainter,
                           QPainterPath, QPen, QPolygonF)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QScrollArea,
                               QVBoxLayout, QWidget)

from ..core.asyncio_qt import run_async
from . import theme
from .dialogs import _DialogBase
from .widgets import PillButton, Toast

# `setFixedWidth` 之后要还原成「可自由伸缩」，QWidget 的语义上限是 16777215
_MAXW = 16777215


class _RingProgress(QWidget):
    """贴着「自动补充」按钮内沿走一圈的进度环 + 左侧文字 + 右侧百分比。

    为什么画在按钮**内部**边缘、而不是外面一圈：
      QWidget 的 paintEvent 会被 Qt 裁剪到自身 rect，子控件想画到父控件外面也会被
      父的 rect 裁掉。真要在按钮外面画，只有两条路 —— 把按钮改小（整行高度跟着变，
      四个按钮就不齐了），或者把画布提到对话框那一层手动跟布局同步位置（换个字体或
      缩放就偏）。所以环贴着按钮内沿走，缝 3px、笔宽 2.6 —— 视觉上就是「绕着这个
      按钮转一圈」，而且零布局改动。

    这层是**点击穿透**的（WA_TransparentForMouseEvents），按钮照样能收到 hover/click。
    """

    GAP = 3.0          # 环离按钮边缘的缝（「间隔不要太大」）
    PEN = 2.6

    def __init__(self, btn):
        super().__init__(btn)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._pct = 0.0
        self._text = "补充中…"
        self.hide()

    # ---------------- 对外 ----------------
    def begin(self, text="补充中…"):
        self._pct = 0.0
        self._text = text
        self._sync()
        self.show()
        self.raise_()
        self.update()

    def set_pct(self, v):
        v = float(v)
        v = 0.0 if v < 0 else (1.0 if v > 1 else v)
        if abs(v - self._pct) < 1e-4:
            return
        self._pct = v
        self._sync()
        self.update()

    def end(self):
        self.hide()

    def _sync(self):
        """按钮尺寸变了（换文案 / 缩放）时跟上，否则环会错位"""
        if self.parentWidget() is not None:
            r = self.parentWidget().rect()
            if self.geometry() != r:
                self.setGeometry(r)

    # ---------------- 画 ----------------
    @staticmethod
    def _arc_points(path, start, length, steps=180):
        """沿路径取一段折线（自己采样，不用 `QPainterPath.trimmed()`）。

        为什么不用 trimmed：实测 PySide6 6.12 / Qt 6.10 上，当起点正好落在圆角矩形
        顶部那条**水平直线段**、且 length 较小时，它会连方向一起搞反 —— 请求
        `trimmed(0.25, 0.06)`（25%~31%）实际返回 8%~25%，于是进度 6% 的白线画到了
        左上角。`pointAtPercent()` 一直是准的，所以按点采样，顺便天然支持跨过 0%
        绕回起点。
        """
        n = max(2, int(steps * min(1.0, max(0.0, length))) + 1)
        pts = []
        for k in range(n):
            t = start + length * (k / (n - 1))
            pts.append(path.pointAtPercent(t - int(t)))
        return pts

    def paintEvent(self, ev):
        w, h = self.width(), self.height()
        if w < 24 or h < 14:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        g, pw = self.GAP, self.PEN
        rect = QRectF(g, g, w - g * 2, h - g * 2)
        rad = rect.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(rect, rad, rad)

        # 底轨：淡淡一整圈，玩家一眼看得出「还差多少」
        base = QPen(QColor(255, 255, 255, 64), pw)
        base.setCapStyle(Qt.RoundCap)
        p.strokePath(path, base)

        if self._pct > 0.002:
            # `addRoundedRect` 的路径起点在**左边中点（9 点方向）**，顺时针走一圈：
            # 0% = 9 点，25% = 12 点，50% = 3 点，75% = 6 点。进度环的习惯起点是
            # 12 点，所以整体从 25% 起步，否则 0% 时左上一小段就亮着，像「没开始就跑」。
            pts = self._arc_points(path, 0.25, self._pct)
            if len(pts) >= 2:
                run = QPen(QColor(255, 255, 255, 246), pw)
                run.setCapStyle(Qt.RoundCap)
                run.setJoinStyle(Qt.RoundJoin)
                p.setPen(run)
                p.drawPolyline(QPolygonF(pts))

        # 左侧文字 + 右侧百分比
        f = QFont(theme.FONT)
        f.setPixelSize(11)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255, 240))
        pad = g + pw + 7.0
        right_w = 36.0
        p.drawText(QRectF(pad, 0, max(8.0, w - pad * 2 - right_w), h),
                   Qt.AlignLeft | Qt.AlignVCenter, self._text)
        p.drawText(QRectF(w - pad - right_w, 0, right_w, h),
                   Qt.AlignRight | Qt.AlignVCenter,
                   f"{int(round(self._pct * 100))}%")


class _ReasonCard(QWidget):
    """一条崩溃原因：标题（粗）+ 细节（灰）+ 建议（浅色小字）"""

    def __init__(self, r, first=False):
        super().__init__()
        self.setAttribute(Qt.WA_StyledBackground, True)
        # 第一条（就是「结论」那条）用淡红底，其余用白底 —— 一眼能分主次
        bg = "rgba(190,58,48,28)" if first else "rgba(255,255,255,120)"
        self.setStyleSheet(
            "QWidget{background:%s;border-radius:10px;}" % bg)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(4)
        t = QLabel(r.get("title") or "未知原因")
        f = QFont(theme.FONT)
        f.setPixelSize(13)
        f.setBold(True)
        t.setFont(f)
        t.setWordWrap(True)
        t.setStyleSheet("background:transparent;color:%s;"
                        % ("#b3372b" if first else "#1f2733"))
        lay.addWidget(t)
        if r.get("detail"):
            d = QLabel(r["detail"])
            d.setWordWrap(True)
            d.setStyleSheet("background:transparent;color:#3a4757;font-size:12px;")
            lay.addWidget(d)
        if r.get("hint"):
            h = QLabel(r["hint"])
            h.setWordWrap(True)
            h.setStyleSheet("background:transparent;color:#6b7684;font-size:11px;")
            lay.addWidget(h)


class _RawCard(QWidget):
    """加载器原始报错（第六十八轮）。

    加载器自带的那屏「加载失败」我们不再显示（进程会被关掉），但原文得留个地方看：
    玩家核对「到底哪个 jar 不满足」「要求什么范围」时只认原文，不认我们的归纳。
    用 QLabel + 可选中复制、不用 QPlainTextEdit：这里只读，多一个滚动条还会跟
    外层 QScrollArea 抢滚轮。
    """

    def __init__(self, text):
        super().__init__()
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            "QWidget{background:rgba(31,39,51,20);border-radius:10px;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(5)
        t = QLabel("加载器原始报错（已内置，游戏里那屏不用再看了）")
        f = QFont(theme.FONT)
        f.setPixelSize(11)
        f.setBold(True)
        t.setFont(f)
        t.setStyleSheet("background:transparent;color:#6b7684;")
        lay.addWidget(t)
        body = QLabel(str(text or ""))
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.setStyleSheet("background:transparent;color:#3a4757;font-size:11px;")
        lay.addWidget(body)


class CrashDialog(_DialogBase):
    """game crashed：原因 + 一键补前置 + 日志三件套"""

    # 第六十八轮：前置全补好时发一次（带着补了几个）——主界面据此 3 秒后自动重启游戏
    fixed_all = Signal(int)

    def __init__(self, parent, info, diag):
        title = str(info.get("name") or "游戏")
        # 第五十八轮：卡在「加载失败」界面上游戏还没退，说「意外退出」是错的
        self._running = bool(info.get("running"))
        head_t = f"「{title}」加载失败" if self._running else f"「{title}」意外退出"
        super().__init__(parent, width=600, title=head_t)
        self.info = info or {}
        self.diag = diag or {}
        missing = self.diag.get("missing") or []

        # ---- 一句话结论 ----
        head = QLabel(self.diag.get("summary") or "游戏异常退出")
        hf = QFont(theme.FONT)
        hf.setPixelSize(15)
        hf.setBold(True)
        head.setFont(hf)
        head.setWordWrap(True)
        head.setStyleSheet("color:#b3372b;")
        self.body.addWidget(head)

        meta = QLabel(self._meta_text())
        meta.setWordWrap(True)
        meta.setStyleSheet("color:#6b7684;font-size:11px;")
        self.body.addWidget(meta)

        # ---- 原因列表（多了就滚） ----
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollBar:vertical{background:transparent;width:6px;margin:2px 0 2px 2px;}"
            "QScrollBar::handle:vertical{background:rgba(31,39,51,64);"
            "border-radius:3px;min-height:26px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        inner = QWidget()
        inner.setStyleSheet("background:transparent;")
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 6, 0)
        il.setSpacing(7)
        for i, r in enumerate(self.diag.get("reasons") or []):
            il.addWidget(_ReasonCard(r, first=(i == 0)))
        sus = self.diag.get("suspects") or []
        if sus:
            chips = QLabel("可疑模组：" + " · ".join(s["jar"] for s in sus))
            chips.setWordWrap(True)
            chips.setStyleSheet("color:#8a6a2a;font-size:11px;"
                                "background:rgba(232,164,66,26);border-radius:8px;"
                                "padding:7px 9px;")
            il.addWidget(chips)
        # 第六十八轮：加载器那屏原文摆进来（我们把它关了，但内容不能丢）
        raw = str(self.diag.get("loader_text") or "")
        if raw:
            il.addWidget(_RawCard(raw))
        il.addStretch(1)
        sa.setWidget(inner)
        # 高度贴着内容走（上限 268 才开始滚）：否则只有一条原因时，
        # 按钮上方会空出一大块，看着像没加载完。
        inner.adjustSize()
        sa.setFixedHeight(max(96, min(268, inner.sizeHint().height() + 6)))
        self.body.addWidget(sa)

        # ---- 下载进度文字（第六十八轮）：紧贴在缺前置清单下方 ----
        # 为什么单独一行而不是塞进按钮：按钮上的环只画得下「一圈 + 一个百分比」，
        # 「正在下第几个、下的哪个文件」放不下；玩家要的正是这两样。
        self.fill_status = QLabel("")
        self.fill_status.setWordWrap(True)
        self.fill_status.setStyleSheet(
            "color:#2f7cf6;font-size:11.5px;font-weight:600;"
            "background:rgba(47,124,246,22);border-radius:8px;padding:7px 10px;")
        self.fill_status.hide()
        self.body.addWidget(self.fill_status)

        # ---- 按钮 ----
        row = QHBoxLayout()
        row.setSpacing(9)
        has_dep = bool(missing)
        self.btn_fix = PillButton(
            f"自动补充 {len(missing)}" if has_dep else "自动补充", "accent")
        self.btn_fix.setFixedHeight(36)
        self.btn_fix.setMinimumWidth(96)
        self.btn_fix.setEnabled(has_dep)
        self.btn_fix.setToolTip(
            "去 Modrinth 按当前游戏版本和加载器找缺的前置，下好放进 mods。" if has_dep
            else "这次崩溃不是「缺前置」，没有能自动补的东西。")
        self.btn_fix.clicked.connect(self._do_fill)
        row.addWidget(self.btn_fix)
        # 进度环叠在按钮上（点击穿透），平时隐藏
        self._ring = _RingProgress(self.btn_fix)
        self._filling = False
        self._prog = {"i": 0, "n": 0, "frac": 0.0}
        self._tick = None

        self.btn_log = PillButton("查看日志", "ghost")
        self.btn_log.setFixedHeight(36)
        self.btn_log.setMinimumWidth(88)
        self.btn_log.setToolTip("用系统默认程序打开这次启动的日志 / 崩溃报告")
        self.btn_log.clicked.connect(self._do_open_log)
        row.addWidget(self.btn_log)

        self.btn_share = PillButton("分享日志", "ghost")
        self.btn_share.setFixedHeight(36)
        self.btn_share.setMinimumWidth(88)
        self.btn_share.setToolTip("复制一份 .txt 日志（有些 AI 不认 .log），"
                                  "复制后路径已在剪贴板，直接拖给 AI 即可")
        self.btn_share.clicked.connect(self._do_share)
        row.addWidget(self.btn_share)
        row.addStretch(1)
        self.btn_cancel = PillButton("取消", "ghost")
        self.btn_cancel.setFixedHeight(36)
        self.btn_cancel.setMinimumWidth(76)
        self.btn_cancel.clicked.connect(self.reject)
        row.addWidget(self.btn_cancel)
        self.body.addLayout(row)
        self.adjustSize()

    def _meta_text(self):
        bits = []
        if self._running:
            bits.append("游戏仍在运行，补完前置要重启游戏")
        ver = self.info.get("version") or ""
        mc = self.info.get("mc") or ""
        if ver:
            bits.append(f"版本 {ver}")
        if mc and mc != ver:
            bits.append(f"原版 {mc}")
        code = self.info.get("code")
        if code is not None:
            bits.append(f"退出码 {code}")
        lived = int(self.info.get("lived") or 0)
        if lived:
            bits.append(f"存活 {lived} 秒")
        return " · ".join(bits)

    # ---------------- 自动补充 ----------------
    def _btn_idle_text(self) -> str:
        n = len(self.diag.get("missing") or [])
        return f"自动补充 {n}" if n else "自动补充"

    def _begin_fill(self):
        """把按钮切进「补充中」形态：文字让位给环上那层（要同时放百分比），宽度锁死不跳"""
        self._filling = True
        # 「自动补充 6」和「补充中… 45%」两种文案宽度差得多，先按够宽的锁住，
        # 否则字样一换按钮会缩一下，旁边的按钮全跟着挪。
        self.btn_fix.setFixedWidth(max(self.btn_fix.width(), 138))
        self.btn_fix.setText("")
        self.btn_fix.setCursor(Qt.ArrowCursor)
        self._prog.update({"i": 0, "n": 0, "frac": 0.0, "text": ""})
        self._ring.begin("补充中…")
        self.fill_status.setText("正在准备下载…")
        self.fill_status.show()
        if self._tick is None:
            self._tick = QTimer(self)
            self._tick.setInterval(60)
            self._tick.timeout.connect(self._paint_progress)
        self._tick.start()

    def _end_fill(self):
        """不管成功失败都要收回进度形态，否则按钮永远卡在「补充中」"""
        self._filling = False
        if self._tick is not None:
            try:
                self._tick.stop()
            except RuntimeError:      # 弹窗已销毁
                pass
        try:
            self.fill_status.hide()
        except RuntimeError:
            pass
        try:
            self._ring.end()
            self.btn_fix.setMaximumWidth(_MAXW)       # 先放开上限再收下限
            self.btn_fix.setMinimumWidth(96)
            self.btn_fix.setText(self._btn_idle_text())
            self.btn_fix.setCursor(Qt.PointingHandCursor)
        except RuntimeError:
            pass

    def _paint_progress(self):
        """主线程 60ms 一次：把工作线程写下的 (第几个, 这个文件下到几分) 折算成一条环

        环画的是**总进度**（多个前置一起下时按「已完成个数 + 当前这个的字节比」
        折成一条），文字那行写的是「正在下第几个 / 共几个 + 当前文件 + 总百分比」。
        """
        st = self._prog
        n = int(st.get("n") or 0)
        if n <= 0:
            pct = 0.0
        else:
            i = int(st.get("i") or 0)
            frac = float(st.get("frac") or 0.0)
            pct = (i + (0.0 if frac < 0 else (1.0 if frac > 1 else frac))) / n
        self._ring.set_pct(pct)
        if not self._filling:
            return
        txt = str(st.get("text") or "").strip()
        short = txt.removeprefix("下载 ").removeprefix("查找 ")
        short = short.rstrip("…").strip()
        if n <= 0:
            self.fill_status.setText(short or "正在准备下载…")
            return
        cur = max(1, min(n, int(st.get("i") or 0) + 1))
        head = f"正在下载 {cur}/{n}"
        if short:
            head += f"（{short}）"
        self.fill_status.setText(f"{head} · 总进度 {int(round(pct * 100))}%")

    def _do_fill(self):
        from ..core import crashdiag
        if self._filling:
            return
        self._begin_fill()

        # ⚠ 下面两个回调都在**工作线程**里跑，只许写 self._prog 这个 dict
        # （单次赋值是原子的），绝不许碰任何 QWidget。
        def on_prog(i, total, text=""):
            self._prog["i"] = int(i)
            self._prog["n"] = int(total)
            self._prog["frac"] = 0.0          # 换下一个文件了，字节进度归零
            self._prog["text"] = str(text or "")

        def on_bytes(i, done, total):
            try:
                t = float(total or 0)
                self._prog["frac"] = (float(done) / t) if t > 0 else 0.0
            except (TypeError, ValueError):
                self._prog["frac"] = 0.0

        def done(res):
            res = res or {}
            ok = res.get("installed") or []
            bad = res.get("failed") or []
            self._end_fill()
            if ok and not bad:
                # 第六十八轮：补完不再让玩家自己回去点启动 —— 3 秒后自动重启。
                # 计时器交给主界面（弹窗马上 accept，自己挂的 QTimer 容易随后被回收）。
                try:
                    self.fill_status.setText(
                        f"已补好 {len(ok)} 个前置，3 秒后自动重启游戏…")
                    self.fill_status.show()
                except RuntimeError:
                    pass
                Toast.show_toast(self, f"已补好 {len(ok)} 个前置，即将自动重启游戏",
                                 ms=2600, kind="success")
                self.fixed_all.emit(len(ok))
                self.accept()
                return
            if ok:
                Toast.show_toast(
                    self, f"补好 {len(ok)} 个，还差 {len(bad)} 个：{bad[0][0]}（{bad[0][1]}）",
                    ms=6000, kind="error")
            else:
                why = bad[0][1] if bad else "没找到可下载的版本"
                Toast.show_toast(self, f"自动补充失败：{why}", ms=6000, kind="error")

        def err(e):
            self._end_fill()
            Toast.show_toast(self, f"自动补充失败：{str(e)[:80]}", ms=5000,
                             kind="error")

        run_async(crashdiag.fill_missing, self.info, self.diag,
                  progress=on_prog, bytes_cb=on_bytes,
                  on_done=done, on_err=err)

    # ---------------- 日志 ----------------
    def _target_log(self) -> str:
        for k in ("report", "log"):
            p = str(self.info.get(k) or "")
            if p and os.path.isfile(p):
                return p
        return ""

    def _do_open_log(self):
        p = self._target_log()
        if not p:
            Toast.show_toast(self, "这次没留下日志文件", kind="error")
            return
        try:
            os.startfile(p)                       # noqa: S606  (Windows)
        except AttributeError:
            import subprocess
            subprocess.Popen(["xdg-open", p])
        except Exception as e:                    # noqa: BLE001
            Toast.show_toast(self, f"打不开日志：{str(e)[:60]}", kind="error")

    def _do_share(self):
        from ..core import crashdiag
        self.btn_share.setEnabled(False)
        self.btn_share.setText("整理中…")

        def done(res):
            path, ok = res if isinstance(res, tuple) else (res, False)
            self.btn_share.setEnabled(True)
            self.btn_share.setText("分享日志")
            if not ok:
                Toast.show_toast(self, "日志写不出来（目录只读？）", ms=5000,
                                 kind="error")
                return
            try:
                QGuiApplication.clipboard().setText(path)
            except Exception:
                pass
            crashdiag.reveal(path)
            Toast.show_toast(
                self, "已生成 txt 日志并在资源管理器里选中，路径已复制；"
                      "直接把这个文件拖给 AI 就行", ms=6000, kind="success")

        def err(e):
            self.btn_share.setEnabled(True)
            self.btn_share.setText("分享日志")
            Toast.show_toast(self, f"生成日志失败：{str(e)[:80]}", ms=5000,
                             kind="error")

        run_async(crashdiag.share, self.info, self.diag,
                  on_done=done, on_err=err)
