# -*- coding: utf-8 -*-
"""界面层简繁切换：运行时拦截控件文本，语言切换时统一刷新。

第一百一十六轮建立；第一百一十七轮补全**自绘控件**：

* 标准控件（QLabel/QPushButton/...）走 setText 补丁，原文存 ``_i18n_src_*``；
* 自绘控件（PillButton / DropDown / ChipLabel / NavDropButton / SplitPill / ...）
  把文本存在自己的 ``_text`` / ``items`` 里、由 paintEvent 直接 drawText，
  绕开 setText —— 第一百一十七轮起由 ``_sync_paint`` 统一登记/转换；
* 输入框 placeholderText 单独拦截（绝不碰玩家输入的内容）；
* 语言切换时 refresh() 除了遍历已登记控件，还做一次**兜底扫描**
  （隐藏页 / 懒建控件 / 构造期文本），确保 102 处遗漏清零。

原文永远是简体，显示时再 conv —— 反复切换不叠加。
全程 try/except：国际化出问题就保持简体，绝不搞崩界面。
"""
import weakref

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (QApplication, QLabel, QPushButton, QToolButton,
                               QRadioButton, QCheckBox, QGroupBox, QWidget,
                               QLineEdit, QTextEdit, QPlainTextEdit, QComboBox,
                               QTabBar)

from ..core import i18n

_SRC_TXT = "_i18n_src_text"
_SRC_TITLE = "_i18n_src_title"
_SRC_TIP = "_i18n_src_tip"
_SRC_ITEM = "_i18n_src_items"
_SRC_PH = "_i18n_src_ph"          # placeholderText 原文
_SRC_ROWS = "_i18n_src_rows"      # 自绘 items [(key, 文案)] 的文案原文
_SRC_DICROW = "_i18n_src_dictrow" # 自绘 dict 行 {"label","sub"} 的原文快照

# 允许拦截 setText 的**显示类**控件（不含任何可编辑控件）
_TEXT_CLASSES = (QLabel, QPushButton, QToolButton, QRadioButton, QCheckBox, QGroupBox)
# 可编辑控件：text() 是玩家输入，绝不登记 / 转换；只处理 placeholder
_EDITABLE = (QLineEdit, QTextEdit, QPlainTextEdit)

_installed = False
_widgets = weakref.WeakSet()


def _has_cn(s) -> bool:
    return isinstance(s, str) and any("\u4e00" <= ch <= "\u9fff" for ch in s)


def original_text(w) -> str:
    """取控件的简体原文（而非界面上显示的繁体）。

    给「文本既要显示、又被代码拿来比较」的地方用（如导入按钮的
    `startswith("导入选中")`）—— 繁体显示下这些判断必须按原文做。
    """
    try:
        src = getattr(w, _SRC_TXT, None)
        if src is not None:
            return src
    except Exception:
        pass
    try:
        return w.text()
    except Exception:
        return ""


def _orig_of(w, name):
    return getattr(type(w), "_i18n_orig_" + name, None)


def _remember(w, key, text, name, value):
    """登记原文（含中文才登记），返回要显示的转换值。"""
    if _has_cn(text):
        setattr(w, key, str(text))
        return i18n.conv(str(text))
    return value


def _wrap_settext(self, orig, text, *a, **kw):
    if _has_cn(text):
        text = _remember(self, _SRC_TXT, text, "setText", text)
    try:
        return orig(self, text, *a, **kw)
    finally:
        _widgets.add(self)


def _wrap_title(self, orig, text, *a, **kw):
    if _has_cn(text):
        text = _remember(self, _SRC_TITLE, text, "setWindowTitle", text)
    return orig(self, text, *a, **kw)


def _wrap_tip(self, orig, text, *a, **kw):
    if _has_cn(text):
        text = _remember(self, _SRC_TIP, text, "tip", text)
    return orig(self, text, *a, **kw)


def _wrap_placeholder(self, orig, text, *a, **kw):
    """QLineEdit.setPlaceholderText：提示文案可转，玩家输入绝不可转。"""
    if _has_cn(text):
        text = _remember(self, _SRC_PH, text, "setPlaceholderText", text)
    try:
        return orig(self, text, *a, **kw)
    finally:
        _widgets.add(self)


def _wrap_additem(self, orig, text, *a, **kw):
    if _has_cn(text):
        src = getattr(self, _SRC_ITEM, None)
        if src is None:
            src = []
            setattr(self, _SRC_ITEM, src)
        src.append(str(text))
        text = i18n.conv(str(text))
    try:
        return orig(self, text, *a, **kw)
    finally:
        _widgets.add(self)


def _wrap_indexed(self, orig, index, text, *a, **kw):
    """setItemText / setTabText：原文按 index 存，显示转换值。"""
    if _has_cn(text):
        src = getattr(self, _SRC_ITEM, None)
        if src is None:
            src = []
            setattr(self, _SRC_ITEM, src)
        while len(src) <= index:
            src.append("")
        src[index] = str(text)
        text = i18n.conv(str(text))
    return orig(self, index, text, *a, **kw)


def _patch(cls, name, wrapper):
    """给类方法打补丁（幂等），原函数存到 ``_i18n_orig_<name>``。"""
    orig = getattr(cls, name, None)
    if orig is None or getattr(orig, "_i18n_patched", False):
        return

    def wrapped(self, *a, **kw):
        return wrapper(self, orig, *a, **kw)

    wrapped._i18n_patched = True
    setattr(cls, "_i18n_orig_" + name, orig)
    setattr(cls, name, wrapped)


# ---------------------------------------------------------------- 自绘控件
def _rows_like(obj):
    """NavDropButton.items 这类 [(key, 文案), ...] 结构，且文案含中文。"""
    items = getattr(obj, "items", None)
    if not isinstance(items, (list, tuple)) or not items:
        return False
    return all(isinstance(it, (list, tuple)) and len(it) >= 2 and _has_cn(it[1])
               for it in items)


# MiniMenu / _MiniMenuRow：文案在 dict 的 label / sub 里，paintEvent 直接画，
# 不走 setText / _text / items tuple。第 118 轮：下载页/详情页点开的下拉选项
# 就是这类，必须在 show 时登记原文并写回译文。
_DICTROW_KEYS = ("label", "sub")


def _dictrow_like(obj):
    """单个 dict 行（_MiniMenuRow.row）或菜单的 rows 列表，含中文文案。"""
    row = getattr(obj, "row", None)
    if isinstance(row, dict) and any(_has_cn(row.get(k)) for k in _DICTROW_KEYS):
        return True
    rows = getattr(obj, "rows", None)
    if isinstance(rows, (list, tuple)):
        return any(isinstance(r, dict) and _has_cn(r.get("label")) for r in rows)
    return False


def _sync_dictrow(w, lang, *, register_only=False):
    """登记 / 刷新 dict 行里的中文（label / sub），原文按 key 存快照。"""
    cv = lambda s: i18n.conv(s, lang)

    def one(row):
        if not isinstance(row, dict):
            return
        snap = row.get(_SRC_DICROW)
        if snap is None:
            snap = {k: str(row[k]) for k in _DICTROW_KEYS if _has_cn(row.get(k))}
            if not snap:
                return
            row[_SRC_DICROW] = snap
        for k, src in snap.items():
            val = cv(src)
            if row.get(k) != val:
                row[k] = val

    row = getattr(w, "row", None)
    if isinstance(row, dict):
        one(row)
    rows = getattr(w, "rows", None)
    if isinstance(rows, (list, tuple)):
        for r in rows:
            one(r)
        # 弹层里每个 _MiniMenuRow 也各自持有同一 dict 的引用，统一 update
        for child in getattr(w, "_items", []) or []:
            try:
                child.update()
            except Exception:
                pass
    try:
        w.update()
    except Exception:
        pass
    _widgets.add(w)


def _sync_paint(w, lang, *, register_only=False):
    """登记 / 刷新自绘控件：``_text`` 与 ``items`` 里的中文。

    PillButton / DropDown / ChipLabel / SplitPill / LaunchSplitButton 等把
    文本存在 ``_text``；NavDropButton 存在 ``items``。这些都由 paintEvent
    直接 drawText，setText 补丁够不到 —— 在这里统一登记原文并写回转换值。

    register_only=True 时只在首次 show 登记并立即转换（新页懒建的场景）。
    """
    cv = lambda s: i18n.conv(s, lang)

    # ① 自绘 _text
    raw = getattr(w, "_text", None)
    if _has_cn(raw):
        if getattr(w, _SRC_TXT, None) is None:
            setattr(w, _SRC_TXT, str(raw))
            val = cv(str(raw))
            try:
                # 走控件自己的 setText（可能是原生 Qt，也可能是 PillButton 的
                # 自定义实现）——它会同步 _text 并 update()；这里用原始方法
                # 避免再次经过补丁把译文当原文登记。
                orig = _orig_of(w, "setText")
                (orig or w.setText)(w, val)
            except Exception:
                w._text = val
                try:
                    w.update()
                except Exception:
                    pass
        else:
            # 已登记也要刷新：懒建页首次 show 时 _SRC_TXT 可能已被
            # 事件过滤器写入，但 _text（paintEvent 读它）还没转成繁体。
            src = getattr(w, _SRC_TXT)
            val = cv(src)
            if getattr(w, "_text", None) != val:
                w._text = val
                # 同步 Qt 内部 text（ChipLabel 等 text() 读它，paintEvent 读 _text，
                # 两条路不一致会让别处 text() 拿到简体）
                try:
                    orig = _orig_of(w, "setText")
                    if orig:
                        orig(w, val)
                except Exception:
                    pass
                try:
                    w.update()
                except Exception:
                    pass

    # ② 自绘 items（NavDropButton：[(key, 文案), ...]，元素是 tuple）
    if _rows_like(w):
        items = w.items
        src = getattr(w, _SRC_ROWS, None)
        if src is None:
            src = [it[1] for it in items]
            setattr(w, _SRC_ROWS, src)
        changed = False
        for i, (it, orig_text) in enumerate(zip(items, src)):
            val = cv(orig_text)
            if it[1] != val:
                try:
                    if isinstance(it, list):
                        it[1] = val                  # list 项：原地改
                    else:
                        items[i] = tuple(val if j == 1 else x
                                         for j, x in enumerate(it))
                    changed = True
                except Exception:
                    pass
        if changed:
            try:
                w.update()
            except Exception:
                pass

    # ③ dict 行（MiniMenu / _MiniMenuRow 的 label / sub）
    if _dictrow_like(w):
        _sync_dictrow(w, lang, register_only=register_only)

    _widgets.add(w)


def _scan_unregistered():
    """兜底扫描主窗所有控件：隐藏页 / 懒建页 / 构造期文本都补登记。

    只在语言切换时跑一次；可编辑控件只处理 placeholder，绝不读它的 text()。
    """
    lang = i18n.current_lang()
    cv = lambda s: i18n.conv(s, lang)

    def visit(w):
        try:
            editable = isinstance(w, _EDITABLE)
            # 标准显示控件的构造期文本（事件过滤器没赶上的）
            if not editable and isinstance(w, _TEXT_CLASSES) and getattr(w, _SRC_TXT, None) is None:
                t = w.text() if hasattr(w, "text") else None
                if _has_cn(t):
                    setattr(w, _SRC_TXT, t)
                    _widgets.add(w)
            # 自绘 _text / items / dict 行
            if hasattr(w, "_text") or _rows_like(w) or _dictrow_like(w):
                _sync_paint(w, lang, register_only=True)
            # placeholder（输入框只走这里）
            if hasattr(w, "placeholderText") and getattr(w, _SRC_PH, None) is None:
                p = w.placeholderText()
                if _has_cn(p):
                    setattr(w, _SRC_PH, p)
                    _widgets.add(w)
        except Exception:
            pass

    for top in QApplication.topLevelWidgets():
        try:
            visit(top)
            for child in top.findChildren(QWidget):
                visit(child)
        except Exception:
            pass


class _Filter(QObject):
    """首次 show 时补登记「构造期文本」；只认白名单显示类控件。"""

    def eventFilter(self, obj, event):
        try:
            if event.type() != QEvent.Show or not isinstance(obj, QWidget):
                return False
            if isinstance(obj, _TEXT_CLASSES) and not hasattr(obj, _SRC_TXT):
                t = obj.text() if hasattr(obj, "text") else None
                if _has_cn(t):
                    setattr(obj, _SRC_TXT, t)
                    orig = _orig_of(obj, "setText")
                    if orig:
                        orig(obj, i18n.conv(t))
            if hasattr(obj, "_text") or _rows_like(obj) or _dictrow_like(obj):
                _sync_paint(obj, i18n.current_lang(), register_only=True)
            _widgets.add(obj)
        except Exception:
            pass
        return False


_filter = None


def install(app: QApplication):
    """安装全局拦截（幂等），必须在建任何控件之前调用。"""
    global _installed, _filter
    if _installed:
        return
    _installed = True

    for cls in _TEXT_CLASSES:
        try:
            _patch(cls, "setText", _wrap_settext)
        except Exception:
            pass

    for name, wrapper in (("setWindowTitle", _wrap_title),
                          ("setToolTip", _wrap_tip),
                          ("setStatusTip", _wrap_tip)):
        try:
            _patch(QWidget, name, wrapper)
        except Exception:
            pass

    # 输入框提示文案（不是输入内容，可以转）
    try:
        _patch(QLineEdit, "setPlaceholderText", _wrap_placeholder)
    except Exception:
        pass

    try:
        _patch(QComboBox, "addItem", _wrap_additem)
        _patch(QComboBox, "setItemText", _wrap_indexed)
    except Exception:
        pass
    try:
        _patch(QTabBar, "setTabText", _wrap_indexed)
    except Exception:
        pass

    _filter = _Filter(app)
    app.installEventFilter(_filter)
    i18n.on_changed(refresh)


def refresh(lang: str = None):
    """按当前语言刷新所有已登记控件。语言切换信号会自动调用。"""
    lang = lang or i18n.current_lang()

    # 兜底：隐藏页 / 懒建页 / 构造期漏登记的，先补进 _widgets
    _scan_unregistered()

    def cv(s):
        return i18n.conv(s, lang)

    for w in list(_widgets):
        try:
            src = getattr(w, _SRC_TXT, None)
            if src is not None:
                orig = _orig_of(w, "setText")
                val = cv(src)
                if getattr(w, "_text", None) is not None:
                    # 自绘控件：_text（paintEvent 读）与 Qt 内部 text（text() 读）
                    # 两条路都要写，否则别处 text() 拿到简体
                    w._text = val
                    if orig:
                        orig(w, val)
                    try:
                        w.update()
                    except Exception:
                        pass
                elif orig:
                    orig(w, val)
                else:
                    w.setText(val)
                continue
            src = getattr(w, _SRC_TITLE, None)
            if src is not None:
                _orig_of(w, "setWindowTitle")(w, cv(src))
                continue
            src = getattr(w, _SRC_TIP, None)
            if src is not None:
                orig = _orig_of(w, "setToolTip") or _orig_of(w, "setStatusTip")
                if orig:
                    orig(w, cv(src))
                continue
            src = getattr(w, _SRC_PH, None)
            if src is not None:
                orig = _orig_of(w, "setPlaceholderText")
                if orig:
                    orig(w, cv(src))
                continue
            src = getattr(w, _SRC_ITEM, None)
            if src is not None:
                _refresh_items(w, src, cv)
            if hasattr(w, "_text") or _rows_like(w) or _dictrow_like(w):
                _sync_paint(w, lang)
        except Exception:
            continue


def _refresh_items(w, src, cv):
    for name in ("setItemText", "setTabText"):
        if not hasattr(w, name):
            continue
        orig = _orig_of(w, name)
        if not orig:
            continue
        try:
            for i, s in enumerate(src):
                if s:
                    orig(w, i, cv(s))
        except Exception:
            pass
