# -*- coding: utf-8 -*-
"""第 106 轮共用层：会话缓存 / 分类变更通知 / 批量选中 / 收放式分类 / 拖拽图标。

游戏管理（`gamemanager.py`）、选择要启动的游戏（`launchselect.py`）、已导入
（`importui.py`）三处都要同一套东西，各写一份必然出现「在这边建的分类那边没有」
「这边只读一次那边每次都扫盘」。所以统一放这里：

· `CACHE`：进程级会话缓存。应用启动即为空，第一次打开才真读盘；
  按 `paths.target_dir()` 分键，切官方 / 应用不会串目录。写操作后显式失效。
· `Selection` + `press_action()`：Ctrl 点选批量、松开 Ctrl 点已选项只取消这一项。
· `CategoryFolder`：默认收起的收放式分类，接收批量拖放，逐条做类型校验。
· `drag_pixmap()`：拖动时的图标，多条画成错位叠加的一摞卡片 + 数量角标。

纪律：core 层不依赖 Qt；这个模块不 import 任何具体界面模块（避免环）；
所有异步回来的结果一律由调用方检查控件是否还活着。
"""
import os
import threading
import weakref

from PySide6.QtCore import QEvent, QMimeData, QObject, QPoint, QRect, Qt
# QDrag 在 QtGui —— 第一一一轮修复：漏了它，make_drag 一执行就
# NameError: name 'QDrag' is not defined，被调用方的 try/except 吞掉，
# 表现就是「单个 / 批量都拖不动、拖动也没有图标」
from PySide6.QtGui import (QColor, QDrag, QFontMetrics, QPainter, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from ..core import categories as cats

MIME = "application/x-bagui-game"


# ---------------------------------------------------------------- 会话缓存
def _config_fingerprint():
    """登记 / 安装 / 总路径有没有变 —— 纯内存比对，绝不读盘。

    会话缓存最怕的是「装完新整合包，列表还停在旧数据」。逐个去找所有写回调
    迟早漏一个，所以让缓存结果自带一份指纹：只要 `installed` / `added_dirs` /
    `imported` / 游戏目录这些键的内容变了，缓存自动作废重读一次。
    """
    try:
        from ..core.config import CONFIG
        parts = []
        for k in ("installed", "added_dirs", "imported", "added_groups",
                  "added_removed", "game_settings"):
            v = CONFIG.get(k)
            if isinstance(v, dict):
                parts.append((k, tuple(sorted(str(x) for x in v.keys()))))
            elif isinstance(v, (list, tuple, set)):
                parts.append((k, tuple(sorted(str(x) for x in v))[:200]))
            else:
                parts.append((k, ""))
        for k in ("game_dir", "install_dir", "official_game_dir",
                  "app_game_override", "download_target"):
            parts.append((k, str(CONFIG.get(k) or "")))
        return tuple(parts)
    except Exception:
        return ()


class SessionCache:
    """一次应用会话内只统计一次；`invalidate()` 只清指定命名空间。"""

    def __init__(self):
        self._d = {}
        self._hits = 0
        self._miss = 0
        self._lock = threading.Lock()

    def get(self, ns, key=""):
        k = (ns, str(key))
        with self._lock:
            entry = self._d.get(k)
            if entry is not None:
                fp, v = entry
                if fp == _config_fingerprint():
                    self._hits += 1
                    # 列表可变，界面渲染只读；回浅拷贝免得行控件改到缓存
                    if isinstance(v, tuple) and len(v) == 2 \
                            and isinstance(v[0], list) and isinstance(v[1], list):
                        return (list(v[0]), list(v[1]))
                    return list(v) if isinstance(v, list) else v
                self._d.pop(k, None)     # 配置变了：自动作废，不读盘
            self._miss += 1
            return None

    def put(self, ns, value, key=""):
        with self._lock:
            self._d[(ns, str(key))] = (_config_fingerprint(), value)

    def has(self, ns, key="") -> bool:
        k = (ns, str(key))
        with self._lock:
            entry = self._d.get(k)
            return bool(entry is not None
                        and entry[0] == _config_fingerprint())

    def invalidate(self, ns=None):
        with self._lock:
            if ns is None:
                self._d.clear()
            else:
                for k in [k for k in self._d if k[0] == ns]:
                    self._d.pop(k, None)

    def stats(self) -> dict:
        with self._lock:
            return {"hits": self._hits, "miss": self._miss,
                    "entries": len(self._d)}


CACHE = SessionCache()

# 命名空间：四类视图各自缓存，互不干扰
NS_GM = "games_manager"        # 游戏管理：版本 + 整合包
NS_LS = "launch_select"        # 选择要启动的游戏：版本 + 整合包
NS_ADDED = "added_entries"     # 已导入（窗口与抽屉共用一份）
ADDED_KEY = "all"              # added_entries() 只认官方目录，不随官方/应用切换变


def dir_key() -> str:
    """缓存的第二维：当前看的这条游戏目录。纯配置推导，不碰磁盘。"""
    try:
        from ..core import paths
        return os.path.normcase(os.path.abspath(paths.target_dir() or ""))
    except Exception:
        return ""


def invalidate_games():
    CACHE.invalidate(NS_GM)
    CACHE.invalidate(NS_LS)


def invalidate_added():
    CACHE.invalidate(NS_ADDED)


# ---------------------------------------------------------------- 分类变更通知
def subscribe_widget(widget, method_name="on_categories_changed"):
    """用 WeakMethod 订阅：popup 每次点开都新建，绝不把已销毁的 QWidget 钉在内存里。"""
    fn = getattr(widget, method_name, None)
    if not callable(fn):
        return
    cats.subscribe(weakref.WeakMethod(fn))


def unsubscribe_widget(widget, method_name="on_categories_changed"):
    fn = getattr(widget, method_name, None)
    if callable(fn):
        cats.unsubscribe(weakref.WeakMethod(fn))


# ---------------------------------------------------------------- 批量选中
class Selection:
    """一个视图里的「批量选中」集合，只存 game_key，不存控件。"""

    def __init__(self, on_change=None):
        self.keys = set()
        self._cb = on_change

    def _changed(self):
        if callable(self._cb):
            try:
                self._cb()
            except Exception:
                pass

    def has(self, key) -> bool:
        return str(key) in self.keys

    def count(self) -> int:
        return len(self.keys)

    def add(self, key) -> bool:
        k = str(key)
        if k in self.keys:
            return False
        self.keys.add(k)
        self._changed()
        return True

    def remove(self, key) -> bool:
        if str(key) not in self.keys:
            return False
        self.keys.discard(str(key))
        self._changed()
        return True

    def clear(self) -> int:
        n = len(self.keys)
        if n:
            self.keys.clear()
            self._changed()
        return n

    def keys_snapshot(self):
        return list(self.keys)


def ctrl_down() -> bool:
    """长按 Ctrl（含笔记本上映射到 Control 的键）。"""
    try:
        return bool(QApplication.keyboardModifiers() & Qt.ControlModifier)
    except Exception:
        return False


def press_action(selection, key):
    """一次左键点下去该做什么 → "add" / "remove" / "clear" / "normal"。

    · 按住 Ctrl：只把这一条加进批量集合，**不执行行本来的点击行为**；
      已经选中的再 Ctrl 点一次保持选中（取消统一放到松开 Ctrl 后做）。
    · 松开 Ctrl 后点「已选中的某一条」：只把这一条移出，其余一条都不动；
      这一次点击同样不执行行本来的行为，免得误切启动目标。
    · 松开 Ctrl 后点未选中的行：清掉整批，回到普通点击。
    """
    k = str(key)
    if ctrl_down():
        selection.add(k)
        return "add"
    if selection.has(k):
        selection.remove(k)
        return "remove"
    if selection.count():
        selection.clear()
        return "clear"
    return "normal"


def encode_rows(rows) -> bytes:
    """rows = [(key, kind), ...]；换行分隔，兼容第 105 轮的单条格式。"""
    return "\n".join(f"{k}|{v}" for k, v in rows).encode("utf-8")


def decode_rows(raw) -> list:
    """解回 [(key, kind), ...]；坏数据不抛异常。"""
    out = []
    try:
        s = bytes(raw).decode("utf-8", "replace") if not isinstance(raw, str) else raw
    except Exception:
        return out
    for line in str(s).splitlines():
        key, _sep, kind = line.partition("|")
        if key:
            out.append((key, kind or "pack"))
    return out


def row_kind(kind) -> str:
    """界面里的 "instance" → 分类层的 "pack"。"""
    return "pack" if str(kind) in ("instance", "pack", "modpack") else "version"


def item_name(item) -> str:
    try:
        return str((item or {}).get("name") or (item or {}).get("id") or "游戏")
    except Exception:
        return "游戏"


# ---------------------------------------------------------------- 拖拽图标
def drag_pixmap(names, count=None) -> QPixmap:
    """拖动时跟着鼠标走的图标：一张卡片；多条画成错位叠加的一摞 + 数量角标。"""
    ns = [str(n) for n in (names or []) if n]
    n = int(count if count is not None else len(ns)) or 1
    w, h = 178, 62
    pm = QPixmap(w, h)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        layers = min(3, max(1, n))
        font = QApplication.font()
        fm = QFontMetrics(font)
        card_w, card_h = 146, 34
        # 先画最远的一张，最后画最前那张 —— 视觉上就是一摞错开的卡片
        for i in range(layers - 1, -1, -1):
            dx, dy = i * 7, i * 6
            rect = QRect(3 + dx, 3 + dy, card_w, card_h)
            back = i > 0
            p.setPen(QPen(QColor(47, 124, 246, 110 if back else 190), 1.2))
            p.setBrush(QColor(255, 255, 255, 205 if back else 248))
            p.drawRoundedRect(rect, 9, 9)
            if i == 0:
                p.setPen(QColor(31, 47, 70))
                txt = ns[0] if ns else f"{n} 个游戏"
                p.drawText(rect.adjusted(12, 2, -12, -2), Qt.AlignVCenter,
                           fm.elidedText(txt, Qt.ElideRight, card_w - 26))
        if n > 1:
            badge = QRect(w - 40, h - 30, 36, 26)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(47, 124, 246, 240))
            p.drawRoundedRect(badge, 13, 13)
            f2 = QApplication.font()
            f2.setBold(True)
            p.setFont(f2)
            p.setPen(QColor(255, 255, 255))
            p.drawText(badge, Qt.AlignCenter, f"×{n}" if n < 100 else "99+")
    finally:
        p.end()
    return pm


def make_drag(host_widget, rows, names, count=None):
    """统一造 QDrag：MIME 带全部成员、pixmap 带图标与堆叠角标。"""
    rows = list(rows or [])
    if not rows:
        return None
    mime = QMimeData()
    mime.setData(MIME, encode_rows(rows))
    drag = QDrag(host_widget)
    drag.setMimeData(mime)
    drag.setPixmap(drag_pixmap(names, count if count is not None else len(rows)))
    drag.setHotSpot(QPoint(26, 20))
    return drag


def toast_parent(host):
    """Toast 要挂主窗：抽屉的 parent 是 QStackedWidget，不是主窗。"""
    win = getattr(host, "window_ref", None)
    if win is not None:
        return win
    p = getattr(host, "parent", None)
    if callable(p):
        p = p()
    return p or host          # parent() 也可能 None（顶层裸容器），别把 None 递下去


# ---------------------------------------------------------------- 收放式分类
class CategoryFolder(QWidget):
    """一处分类 = 一个收放式文件夹。**默认收起**（第 106 轮）。

    两个视图共用，所以「在游戏管理里建的分类」和「在选择要启动的游戏里建的
    分类」本来就是同一批 `cats.categories()`；谁改了都走 `cats.notify_changed()`，
    另一边当场重渲染（用缓存，不读盘）。

    host 契约（两边都满足）：`parent()` / `reload()` / 可选 `_fit()`。
    """

    def __init__(self, rec, rows, host, default_open=False):
        super().__init__()
        self.cid = str(rec.get("id") or "")
        self.kind = str(rec.get("kind") or "pack")
        self.host = host
        self._rows = list(rows or [])
        self._hover = False
        self._open = bool(default_open)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("catFolder")
        self.setStyleSheet("#catFolder{background:rgba(47,124,246,13);"
                           "border-radius:11px;}")
        self.setAcceptDrops(True)

        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(5)
        head = QHBoxLayout()
        head.setSpacing(6)
        self.btn_head = QPushButton()
        self.btn_head.setCursor(Qt.PointingHandCursor)
        self.btn_head.setFixedHeight(30)
        self.btn_head.setStyleSheet(
            "QPushButton{background:transparent;border:none;text-align:left;"
            "color:#1f2733;font-size:12px;font-weight:600;padding:0 4px;}")
        self.btn_head.clicked.connect(self.toggle)
        head.addWidget(self.btn_head, 1)
        try:
            from .widgets import IconButton
            btn_del = IconButton("trash", 24, 11, color="#c05a52")
        except Exception:                                    # noqa: BLE001
            btn_del = QPushButton("×")
            btn_del.setFixedSize(24, 24)
            btn_del.setStyleSheet("QPushButton{border:none;color:#c05a52;"
                                  "font-size:16px;background:transparent;}")
        btn_del.setToolTip("移除这个分类（里面的游戏回到未分类，硬盘文件不动）")
        btn_del.clicked.connect(self._remove)
        head.addWidget(btn_del)
        v.addLayout(head)

        self.body = QWidget()
        self.body.setStyleSheet("background:transparent;")
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(14, 0, 0, 0)
        bl.setSpacing(5)
        for r in self._rows:
            bl.addWidget(r)
        if not self._rows:
            lb = QLabel("空分类：按住 Ctrl 点选几个游戏，再长按拖进来")
            lb.setStyleSheet("color:#8a95a3;font-size:11px;"
                             "background:transparent;")
            lb.setWordWrap(True)
            bl.addWidget(lb)
        v.addWidget(self.body)
        self.body.setVisible(self._open)
        self._render_head()

    # ---- 展示 ----
    def _name(self):
        c = cats.get(self.cid)
        return c["name"] if c else "分类"

    def _render_head(self):
        mark = "▾" if self._open else "▸"
        self.btn_head.setText(f"{mark}　{self._name()}　·　{len(self._rows)} 个")
        self.btn_head.setToolTip("点一下展开 / 收起；把游戏拖到这块区域即可归类，"
                                 "拖到列表空白处松手即移出分类")

    def toggle(self):
        # 显式布尔，不读 body.isVisible()：父控件没 show 时它恒 False（第 105 轮）
        self._open = not self._open
        self.body.setVisible(self._open)
        self._render_head()
        fit = getattr(self.host, "_fit", None)
        if callable(fit):
            try:
                fit()
            except Exception:
                pass

    def _remove(self):
        from .widgets import Toast
        cats.remove(self.cid)              # 内部 notify → 两个视图同步重渲染
        Toast.show_toast(toast_parent(self.host), "分类已移除，游戏仍在硬盘上",
                         ms=2800, kind="info")
        if cats.subscriber_count() == 0:  # 单独使用（测试 / 未订阅的宿主）
            try:
                self.host.reload()
            except Exception:
                pass

    # ---- 拖放：一次可接一批 ----
    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME):
            self._hover = True
            self.setStyleSheet("#catFolder{background:rgba(47,124,246,50);"
                               "border-radius:11px;}")
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._hover = False
        self.setStyleSheet("#catFolder{background:rgba(47,124,246,13);"
                           "border-radius:11px;}")

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(MIME):
            e.acceptProposedAction()

    def dropEvent(self, e):
        from .widgets import Toast
        self.dragLeaveEvent(e)
        if not e.mimeData().hasFormat(MIME):
            return
        rows = decode_rows(e.mimeData().data(MIME))
        ok_n, last = 0, ""
        for key, kind in rows:
            ok, msg = cats.assign(self.cid, key, kind, notify=False)
            if ok:
                ok_n += 1
            elif msg:
                last = msg
        # 一批只通知一次，别 N 次重渲染整张表
        if ok_n:
            cats.notify_changed()
        e.acceptProposedAction()
        if ok_n > 1:
            text = f"已把 {ok_n} 个游戏放入「{self._name()}」"
        elif ok_n == 1:
            text = f"已放入「{self._name()}」"
        else:
            text = last or "没有游戏被归类"
        if rows:
            Toast.show_toast(toast_parent(self.host), text, ms=3600,
                             kind="success" if ok_n else "error")


# ---------------------------------------------------------------- 分类渲染
class _DragOutFilter(QObject):
    """第一一二轮：列表空白处接住「拖出分类」。

    事件过滤器而不是改容器类 —— 两个视图的滚动内容都是临时 `QWidget()`，
    换类要动构造链；过滤器挂在 host 上、随 host 一起活。要点：
    · `DragEnter` 必须替容器 accept（QWidget 默认 ignore，一 ignore 后面
      的 DragMove / Drop 全都不来了）；
    · 分类文件夹自己 accept drops 且是更深的子控件，拖进文件夹仍然走
      `CategoryFolder.dropEvent` 归类，不会被这里抢走 —— 只有落到文件夹
      外面的空白处（或没接 drop 的普通行 / 标题上冒泡上来）才算「拖出」。
    """

    def __init__(self, host):
        super().__init__(host)
        self._host = host

    def eventFilter(self, obj, ev):                      # noqa: N802
        t = ev.type()
        if t in (QEvent.DragEnter, QEvent.DragMove):
            if ev.mimeData().hasFormat(MIME):
                ev.acceptProposedAction()
                return True
            ev.ignore()
            return True
        if t == QEvent.Drop:
            self._drop(ev)
            return True
        return False

    def _drop(self, ev):
        rows = decode_rows(ev.mimeData().data(MIME))
        if not rows:
            ev.ignore()
            return
        n = 0
        for key, kind in rows:
            # kind 随行走：拖出来的是整合包就是 pack、版本就是 version，
            # 各归各的 kind，别把两类搅在一起
            n += cats.unassign(key, kind=kind, notify=False)
        if n:
            cats.notify_changed()          # 一批只通知一次，别 N 次重渲染
        ev.acceptProposedAction()
        from .widgets import Toast
        if n > 1:
            text = f"已把 {n} 个游戏移出分类，回到未分类"
        elif n == 1:
            text = "已移出分类，回到未分类"
        else:
            text = "这些游戏本来就不在分类里"
        # host 是滚动内容容器，没有 window_ref：沿 parentWidget 找主窗，
        # 找不到（测试/裸容器）就挂 host 自己，绝不把 None 递给 Toast
        w = getattr(self._host, "window_ref", None)
        if w is None:
            w = self._host.parentWidget() or self._host
        Toast.show_toast(w, text, ms=3200,
                         kind="success" if n else "info")


def enable_drag_out(host_widget):
    """让滚动内容容器接住「拖出分类」：从文件夹拖到列表空白处松手即移出。

    返回过滤器实例（父控件是 host，随 host 存亡；返回值只是给测试抓用）。
    """
    host_widget.setAcceptDrops(True)
    f = _DragOutFilter(host_widget)
    host_widget.installEventFilter(f)
    return f


def claim_categories(layout, kind, pairs, host, rows_out=None):
    """把 [(game_key, row_widget)] 里属于各分类的收进文件夹，返回没被认领的。

    一条游戏只出现一次：按分类创建顺序认领，`used` 去重（第 105 轮铁律）。
    """
    pairs = list(pairs or [])
    used = set()
    for c in cats.categories(kind):
        members = set(c["members"])
        picked = []
        for k, r in pairs:
            if k in members and k not in used:
                picked.append(r)
                used.add(k)
        layout.addWidget(CategoryFolder(c, picked, host))
        if rows_out is not None:
            rows_out.extend(picked)
    return [(k, r) for k, r in pairs if k not in used]


def render_kind(layout, kind, items, host, make_row, rows_out=None):
    """渲染一个 kind 的全部行：分类里的进文件夹，其余平铺。"""
    pairs = []
    for it in items or []:
        row = make_row(it)
        if row is None:
            continue
        pairs.append((cats.game_key(it), row))
        if rows_out is not None:
            rows_out.append(row)
    left = claim_categories(layout, kind, pairs, host)
    for _k, r in left:
        layout.addWidget(r)
    return len(pairs)
