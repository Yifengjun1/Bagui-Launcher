# -*- coding: utf-8 -*-
"""下载前的「版本匹配确认」（第六十轮）。

为什么要有这一步：
    模组 / 数据包 / 光影 / 材质包都是**绑死某个 MC 版本 + 加载器**的。以前点
    「下载」直接挑最新文件 —— 装进 1.21.1 的实例里却下到 1.20.1 的 jar，游戏
    一启动就崩（玩家看到的是「刚加的模组会让游戏闪退」）。现在点下载先停一下，
    把「你选的是哪个游戏」和「这个文件支持哪些版本」摆在同一块卡片上，让玩家
    自己核对；觉得不对可以就地**换目标游戏**，确认之后才真正开始下载。

对外只有一个函数：
    ask(parent, kind, hit, name) -> (target, True) / (None, False)
    target = {"instance", "version_id", "mc", "loader", "loader_ver", "label"}
"""
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QVBoxLayout, QWidget)

from ..core import gamelaunch
from . import theme
from .dialogs import _DialogBase
from .widgets import ChoiceExpander, PillButton

CN = {"mod": "模组", "datapack": "数据包",
      "shader": "光影", "resourcepack": "材质包", "modpack": "整合包"}

LOADERS = ("forge", "neoforge", "fabric", "quilt", "liteloader", "rift")
LOADER_CN = {"forge": "Forge", "neoforge": "NeoForge", "fabric": "Fabric",
             "quilt": "Quilt", "liteloader": "LiteLoader", "rift": "Rift"}

MATCH, MISMATCH, UNKNOWN = "match", "mismatch", "unknown"


# ---------------------------------------------------------------- 纯逻辑（探针直接调）
def supported_versions(hit):
    """这个项目在搜索结果里声明的 MC 版本列表（没有就空表）"""
    raw = (hit or {}).get("versions") or []
    out = []
    for v in raw:
        s = str(v or "").strip()
        if s and s not in out:
            out.append(s)
    # 正式版号在前、同类按版本号倒序，读起来最顺
    def key(s):
        parts = []
        for p in s.split("."):
            digits = "".join(ch for ch in p if ch.isdigit())
            parts.append(int(digits) if digits else -1)
        return (0 if s[:1].isdigit() else 1, [-x for x in parts])
    try:
        out.sort(key=key)
    except Exception:                                  # noqa: BLE001
        pass
    return out


def supported_loaders(hit):
    """这个项目声明的加载器（没有就从 categories 里捞）"""
    hit = hit or {}
    out = []
    for src in (hit.get("loaders") or []):
        s = str(src or "").strip().lower()
        if s in LOADERS and s not in out:
            out.append(s)
    if not out:
        for c in (hit.get("categories") or []):
            s = str(c or "").strip().lower()
            if s in LOADERS and s not in out:
                out.append(s)
    return out


def _mc_eq(target, cand):
    """两个 MC 版本号算不算同一个（1.21 / 1.21.1 视为同族，别的必须相等）"""
    a = str(target or "").strip()
    b = str(cand or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    return a.startswith(b + ".") or b.startswith(a + ".")


def target_note(kind, target):
    """这台游戏根本装不了这类内容吗？返回原因（能装 / 判不了 → 空串）

    第六十一轮：玩家在确认窗里可以把「装到哪个游戏」换成一台原版实例 ——
    那台没有加载器，模组装进去只是白占地方，得当场说清楚。
    """
    target = target or {}
    loader = str(target.get("loader") or "").lower()
    mc = str(target.get("mc") or "")
    if kind in ("mod", "shader") and not loader:
        return ("这台是原版（没有加载器），%s装不进去"
                % ("模组" if kind == "mod" else "光影（要靠 Iris / OptiFine）"))
    if kind == "datapack" and not gamelaunch.datapack_supported(mc):
        return "MC %s 还没有「数据包」这个玩法（1.13 起才有）" % mc
    return ""


def judge(target, hit):
    """判断「目标游戏」和「这个文件」对不对得上。

    返回 (状态, 一句话说明)；状态是 MATCH / MISMATCH / UNKNOWN。
    """
    target = target or {}
    vers = supported_versions(hit)
    loaders = supported_loaders(hit)
    mc = str(target.get("mc") or "")
    ld = str(target.get("loader") or "").lower()
    if not vers and not loaders:
        return UNKNOWN, ""
    if vers and mc and not any(_mc_eq(mc, v) for v in vers):
        return MISMATCH, "这个文件没有 MC %s 的版本" % mc
    if loaders and ld and ld not in loaders:
        return MISMATCH, "这个文件是给 %s 用的，你的游戏是 %s" % (
            " / ".join(LOADER_CN.get(x, x) for x in loaders),
            LOADER_CN.get(ld, ld))
    if vers and not mc:
        return UNKNOWN, ""
    if loaders and not ld:
        return UNKNOWN, ""
    return MATCH, ""


def describe(target, hit):
    """给对话框用的一行一句：「你的游戏」 / 「这个文件支持」"""
    target = target or {}
    vers = supported_versions(hit)
    loaders = supported_loaders(hit)
    mine = gamelaunch._ver_text(target)                # noqa: SLF001
    sup = []
    if vers:
        sup.append("、".join(vers[:6]) + ("…" if len(vers) > 6 else ""))
    if loaders:
        sup.append(" / ".join(LOADER_CN.get(x, x) for x in loaders))
    return mine, (" · ".join(sup) if sup else "")


# ---------------------------------------------------------------- 对话框
class _Tick(QWidget):
    """18×18 自绘勾选框 —— 系统 QCheckBox 的方框在磨砂卡片上几乎看不见"""

    def __init__(self, parent=None, size=18):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.on = False

    def set_on(self, v):
        v = bool(v)
        if self.on != v:
            self.on = v
            self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        if self.on:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#2f7cf6"))
            p.drawRoundedRect(r, 5, 5)
            p.setPen(QPen(QColor("#ffffff"), 1.8, Qt.SolidLine,
                          Qt.RoundCap, Qt.RoundJoin))
            sz = self.width()
            p.drawPolyline(QPolygonF([
                QPointF(sz * 0.24, sz * 0.52),
                QPointF(sz * 0.43, sz * 0.70),
                QPointF(sz * 0.77, sz * 0.31)]))
        else:
            p.setPen(QPen(QColor(120, 132, 148, 190), 1.4))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r, 5, 5)
        p.end()


class _Confirm(QWidget):
    """勾选框 + 一句话（整行可点，命中面积大一点）"""

    changed = Signal(bool)

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(8)
        self.box = _Tick(self, 18)
        lay.addWidget(self.box, 0, Qt.AlignVCenter)
        self.lb = QLabel(text)
        self.lb.setWordWrap(True)
        self.lb.setStyleSheet("color:#2a3442;font-size:12px;"
                              "background:transparent;")
        lay.addWidget(self.lb, 1)

    def isChecked(self):
        return bool(self.box.on)

    def setChecked(self, v, notify=True):
        v = bool(v)
        was = self.box.on
        self.box.set_on(v)
        if notify and was != v:
            self.changed.emit(v)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.setChecked(not self.isChecked())


class MatchDialog(_DialogBase):
    """「装到哪个游戏 + 版本对不对得上」的确认窗。

    `targets` 来自 `gamelaunch.install_targets()`；玩家可以在下拉里换一个目标，
    换完判定会立刻重算。没勾「我确认」之前「开始下载」是灰的 —— 这一步就是要
    让玩家真的看一眼，而不是无脑点过去。
    """

    def __init__(self, parent, kind, hit, name, targets):
        cn = CN.get(kind, "内容")
        super().__init__(parent, width=580, title=f"下载{cn} · 先确认版本")
        self.kind = kind
        self.hit = hit or {}
        self.targets = list(targets or [])
        self._by_key = {str(t["key"]): t for t in self.targets}
        self.chosen = self.targets[0] if self.targets else {}
        self.ok = False
        self._blocked = False        # 选中的目标根本装不了这类内容 → 勾了也不给下

        # ---- 名字 ----
        qt = QLabel(str(name or "内容").strip() or "内容")
        f = QFont(theme.FONT)
        f.setPixelSize(13.5)
        f.setBold(True)
        qt.setFont(f)
        qt.setWordWrap(True)
        qt.setStyleSheet("color:#1f2733;background:transparent;")
        self.body.addWidget(qt)

        # ---- 装到哪个游戏（可手动改）----
        opts = [(str(t["key"]), t["label"], t.get("note") or "")
                for t in self.targets]
        self.picker = ChoiceExpander("装到哪个游戏", opts,
                                     str(self.chosen.get("key") or ""))
        self.picker.changed.connect(self._on_target)
        self.body.addWidget(self.picker)

        # ---- 判定结果 ----
        self.card = QWidget()
        self.card.setAttribute(Qt.WA_StyledBackground, True)
        cl = QVBoxLayout(self.card)
        cl.setContentsMargins(12, 10, 12, 10)
        cl.setSpacing(6)
        self.lb_state = QLabel("")
        self.lb_state.setWordWrap(True)
        sf = QFont(theme.FONT)
        sf.setPixelSize(12.5)
        sf.setBold(True)
        self.lb_state.setFont(sf)
        self.lb_state.setStyleSheet("background:transparent;")
        cl.addWidget(self.lb_state)
        self.lb_mine = QLabel("")
        self.lb_mine.setWordWrap(True)
        self.lb_mine.setStyleSheet("background:transparent;font-size:11.5px;"
                                   "color:#3a4757;")
        cl.addWidget(self.lb_mine)
        self.lb_sup = QLabel("")
        self.lb_sup.setWordWrap(True)
        self.lb_sup.setStyleSheet("background:transparent;font-size:11.5px;"
                                  "color:#3a4757;")
        cl.addWidget(self.lb_sup)
        self.body.addWidget(self.card)

        # ---- 确认勾选 ----
        # 自绘勾选框：系统 QCheckBox 的方框在磨砂卡片上几乎看不见（只剩一个 ✓）
        self.chk = _Confirm("我核对过了，这个版本和我的游戏匹配")
        self.chk.changed.connect(self._sync_ok)
        self.body.addWidget(self.chk)

        self.lb_hint = QLabel("")
        self.lb_hint.setWordWrap(True)
        self.lb_hint.setStyleSheet("background:transparent;font-size:11px;"
                                   "color:#7b8794;")
        self.body.addWidget(self.lb_hint)

        row = QHBoxLayout()
        row.addStretch(1)
        row.setSpacing(10)
        b_cancel = PillButton("取消", "ghost")
        b_cancel.setFixedHeight(36)
        b_cancel.setMinimumWidth(92)
        b_cancel.clicked.connect(self.reject)
        row.addWidget(b_cancel)
        self.b_ok = PillButton("开始下载", "accent")
        self.b_ok.setFixedHeight(36)
        self.b_ok.setMinimumWidth(92)
        self.b_ok.clicked.connect(self._ok)
        row.addWidget(self.b_ok)
        self.body.addLayout(row)
        self._refresh()

    # ------------------------------------------------------------ 内部
    def _on_target(self, key):
        self.chosen = self._by_key.get(str(key)) or {}
        self._refresh()

    def _refresh(self):
        state, why = judge(self.chosen, self.hit)
        # 换到一台原版游戏时，「版本对得上」也得改成「装不进去」
        cap = target_note(self.kind, self.chosen)
        if cap:
            state, why = MISMATCH, cap
        self._blocked = bool(cap)
        mine, sup = describe(self.chosen, self.hit)
        if state == MATCH:
            self.card.setStyleSheet(
                "QWidget{background:rgba(47,160,92,26);border-radius:10px;}")
            self.lb_state.setText("✓ 版本对得上")
            self.lb_state.setStyleSheet("background:transparent;color:#1f7a44;")
            self.chk.setChecked(True)
        elif state == MISMATCH:
            self.card.setStyleSheet(
                "QWidget{background:rgba(179,55,43,24);border-radius:10px;}")
            self.lb_state.setText("⚠ 版本对不上：" + (why or "请手动核对"))
            self.lb_state.setStyleSheet("background:transparent;color:#b3372b;")
            self.chk.setChecked(False)
        else:
            self.card.setStyleSheet(
                "QWidget{background:rgba(138,109,59,26);border-radius:10px;}")
            self.lb_state.setText("这个问题没声明支持哪些版本，请自己核对")
            self.lb_state.setStyleSheet("background:transparent;color:#8a6d3b;")
            self.chk.setChecked(False)
        self.lb_mine.setText("你的游戏：%s" % mine)
        self.lb_sup.setText("这个文件支持：%s" % (sup or "未提供（不会替你判断）"))
        if state == MISMATCH:
            if cap:
                self.lb_hint.setText(
                    "换一台装了加载器的游戏（比如带 Forge / NeoForge / Fabric "
                    "的整合包实例），或者去主页先装一个加载器版本再回来下。")
            else:
                self.lb_hint.setText(
                    "装进去大概率一进游戏就崩。可以在上面换一个游戏，"
                    "或者回详情页的「往期版本」里挑一个对得上的文件。")
        elif state == UNKNOWN:
            self.lb_hint.setText(
                "上面可以换目标游戏；确认无误后勾选上面的方框，"
                "「开始下载」才会亮起来。")
        else:
            self.lb_hint.setText("确认无误就可以开始下载了。")
        self._sync_ok()

    def _sync_ok(self, *_a):
        # 「这台游戏装不了」是硬拦：勾了也不放行（勾选只表示「我看过了」）
        self.b_ok.setEnabled(bool(self.chk.isChecked())
                             and not getattr(self, "_blocked", False))

    def _ok(self):
        if not self.chk.isChecked() or getattr(self, "_blocked", False):
            return
        self.ok = True
        self.accept()


# ---------------------------------------------------------------- 对外
def ask(parent, kind, hit, name):
    """弹确认窗 -> (target, True)；玩家取消 -> (None, False)

    没有可选目标（连官方目录都拿不到）时**不弹**，直接放行：
    这时候弹出来只是一张全是「未知」的空卡片，白白拦一次下载。
    """
    try:
        targets = gamelaunch.install_targets()
    except Exception:                                  # noqa: BLE001
        targets = []
    if not targets:
        return {}, True
    d = MatchDialog(parent, kind, hit, name, targets)
    d.exec()
    if d.ok:
        return dict(d.chosen), True
    return None, False
