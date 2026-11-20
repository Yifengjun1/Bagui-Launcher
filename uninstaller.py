# -*- coding: utf-8 -*-
"""卸载程序（第五十一轮）：装进 `<安装目录>\\卸载八桂启动器.exe`。

做三件事：
  ① 列清楚「这台机器上跟八桂启动器有关的东西」各占多大；
  ② 删除前让玩家逐项勾选「要保留」—— 游戏存档、官方目录、账号配置默认都保留；
  ③ 点「确认卸载」后先删快捷方式 / 程序本体，再删没保留的数据，
     最后把自己也删掉（运行中的 exe 删不了，交给一个延时 bat 收尾）。

安全约定（重要）：
  · 只允许删「安装目录」和「数据目录」白名单之内的路径，越界一律拒绝；
  · `.minecraft` 可能是目录联接（junction）→ 只摘链接，**绝不**递归删到真实数据；
  · 任何一项失败都记下来给玩家看，不静默吞掉。
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QProgressBar,
                               QStackedWidget, QVBoxLayout, QWidget)

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import paths                                             # noqa: E402
from app.core import winlnk                                            # noqa: E402
from app.core.config import (APP_NAME, APP_VERSION, CONFIG,            # noqa: E402
                             DATA_DIR_NAME, LEGACY_APP_NAMES, UNINSTALL_EXE_NAME)
from app.ui import theme                                               # noqa: E402
from app.ui.theme import paint_background                              # noqa: E402

IS_FROZEN = getattr(sys, "frozen", False)


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return (f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}")
        n /= 1024.0
    return f"{n:.1f} GB"


# ─────────────────────────────── 路径与安全 ────────────────────────────────
def is_link(p):
    """符号链接 / 目录联接：删它只能「摘掉链接」，不能递归跟进去。"""
    try:
        if os.path.islink(p):
            return True
    except OSError:
        pass
    try:
        return bool(os.path.isjunction(p))          # Python 3.12+
    except (AttributeError, OSError):
        pass
    try:                                            # 老办法：FILE_ATTRIBUTE_REPARSE_POINT
        attr = ctypes.windll.kernel32.GetFileAttributesW(str(p))
        return attr not in (0xFFFFFFFF, None) and bool(attr & 0x400)
    except Exception:
        return False


def entry_size(p, limit_mb=20000):
    """统计一个文件/目录的大小（不跟进链接，最多数 20GB 的量级就停）。"""
    cap = int(limit_mb) * 1024 * 1024
    if not os.path.exists(p) and not is_link(p):
        return 0
    if is_link(p):
        return 0                     # 链接本身不占空间，也别去数目标
    if os.path.isfile(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return 0
    total = 0
    try:
        for root, dirs, files in os.walk(p, followlinks=False):
            dirs[:] = [d for d in dirs if not is_link(os.path.join(root, d))]
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
            if total > cap:
                return cap
    except OSError:
        pass
    return total


def shortcut_paths():
    """桌面 / 开始菜单里那两个快捷方式的**确切**路径（主程序图标 + 卸载入口）。

    它们既不在安装目录也不在数据目录下，所以必须单独进白名单 ——
    只放这两条确切路径，**不**放整个桌面/开始菜单目录（那是「防手抖」的前提：
    白名单一旦放宽到桌面根，配置被改坏时能顺手删掉玩家桌面上的东西）。
    """
    out = []
    try:
        bases = (winlnk.desktop_dir(), winlnk.startmenu_dir())
    except Exception:                                  # noqa: BLE001
        return out
    names = (f"{APP_NAME}.lnk", f"卸载{APP_NAME}.lnk")
    for base in bases:
        if not base:
            continue
        for n in names:
            try:
                out.append(os.path.normcase(os.path.abspath(os.path.join(base, n))))
            except OSError:
                pass
    return out


def allowed_roots(install_dir, data_dir, extra=()):
    """白名单根：安装目录 + 数据目录 + 若干**确切**路径（快捷方式）。"""
    dirs = data_dir if isinstance(data_dir, (list, tuple, set)) else [data_dir]
    out = []
    for p in [install_dir] + list(dirs):
        if p:
            try:
                out.append(os.path.normcase(os.path.abspath(p)))
            except OSError:
                pass
    for p in extra or ():
        if p:
            try:
                out.append(os.path.normcase(os.path.abspath(p)))
            except OSError:
                pass
    return out


def app_data_dirs():
    """实际存在的用户数据目录（新名字 + 老名字）。

    注意**不要**调 `config.app_data_dir()`：那会触发「老目录改名」迁移，
    卸载时白复制几百 MB 毫无意义。这里只看谁在，不搬不建。
    """
    base = os.environ.get("APPDATA") or os.path.join(
        os.environ.get("USERPROFILE") or os.path.expanduser("~"), "AppData", "Roaming")
    out = []
    for name in (DATA_DIR_NAME,) + tuple(LEGACY_APP_NAMES):
        p = os.path.join(base, name)
        if os.path.isdir(p) or is_link(p):
            if p not in out:
                out.append(p)
    return out


def within(p, roots):
    """只允许动白名单根目录里的东西（防手抖 / 配置被改坏时误删整个盘）。"""
    try:
        ap = os.path.normcase(os.path.abspath(p))
    except OSError:
        return False
    for r in roots:
        if ap == r or ap.startswith(r + os.sep):
            return True
    return False


def remove_any(p, roots, errors):
    """删掉 p：链接只摘链接，目录递归删（不跟链接），文件直接删。"""
    if not within(p, roots):
        errors.append((p, "不在允许删除的范围内"))
        return False
    if not os.path.exists(p) and not is_link(p):
        return True
    try:
        if is_link(p):
            os.rmdir(p)                      # junction / symlink：摘链接，不碰目标
            return True
        if os.path.isfile(p):
            os.remove(p)
            return True
        for root, dirs, files in os.walk(p, topdown=False, followlinks=False):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    os.remove(fp)
                except OSError as e:
                    errors.append((fp, e.strerror or str(e)))
            for d in dirs:
                dp = os.path.join(root, d)
                try:
                    if is_link(dp):
                        os.rmdir(dp)
                    else:
                        os.rmdir(dp)
                except OSError as e:
                    errors.append((dp, e.strerror or str(e)))
        try:
            os.rmdir(p)
            return True
        except OSError as e:
            errors.append((p, e.strerror or str(e)))
            return False
    except OSError as e:
        errors.append((p, e.strerror or str(e)))
        return False


# ─────────────────────────────── 收集要处理的东西 ──────────────────────────
def resolve_install_dir():
    """卸载程序所在目录就是安装目录；开发模式看配置 / 环境变量。"""
    if IS_FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    env = os.environ.get("MCASSIST_INSTALL_DIR") or ""
    if env and os.path.isdir(env):
        return env
    cfg = str(CONFIG.get("install_dir") or "").strip()
    if cfg and os.path.isdir(cfg):
        return cfg
    return os.path.dirname(os.path.abspath(__file__))


def self_exe_path():
    return os.path.abspath(sys.executable if IS_FROZEN else __file__)


def _official_root(install_dir):
    """官方路径的「根」：一般是 `<安装目录>\\official_assets`。

    玩家可能把官方目录改到别处（D:\\MC\\.minecraft），那就只针对那个目录本身。
    """
    off = str(CONFIG.get("official_game_dir") or "").strip()
    cands = []
    if off:
        cands.append(off)
    cands.append(os.path.join(install_dir, paths.OFFICIAL_DIR, paths.APP_DIR_NAME))
    cands.append(os.path.join(install_dir, paths.OFFICIAL_DIR))
    for c in cands:
        if not c:
            continue
        if not (os.path.isdir(c) or is_link(c)):
            continue
        parent = os.path.dirname(c)
        if os.path.basename(parent).lower() == paths.OFFICIAL_DIR.lower() and \
                within(parent, allowed_roots(install_dir, "")):
            return parent, False
        return c, True          # True = 不在安装目录里，界面要提醒
    return "", False


def collect_items(install_dir=None, data_dir=None):
    """列出所有「可保留 / 可删除」的项。返回 (items, install_dir, data_dirs)。"""
    install_dir = install_dir or resolve_install_dir()
    if data_dir is None:
        datas = app_data_dirs()
    elif isinstance(data_dir, (list, tuple, set)):
        datas = [p for p in data_dir if p]
    else:
        datas = [data_dir] if data_dir else []
    roots = allowed_roots(install_dir, datas)
    items = []

    # ① 程序本体：安装目录里除了「数据目录」之外的一切（含主程序、本卸载程序）
    _off_root, _ = _official_root(install_dir)
    _skip = {os.path.normcase(os.path.abspath(p)) for p in
             (os.path.join(install_dir, ".minecraft"), _off_root) if p}
    app_paths = []
    try:
        for name in sorted(os.listdir(install_dir)):
            p = os.path.join(install_dir, name)
            if os.path.normcase(os.path.abspath(p)) in _skip:
                continue
            app_paths.append(p)
    except OSError:
        pass
    items.append({"key": "app", "label": "程序本体（含本卸载程序）", "paths": app_paths,
                  "size": sum(entry_size(p) for p in app_paths),
                  "keep": False, "can_keep": False, "kind": "files",
                  "note": "只要卸载就一定会删"})

    # ② 应用路径（本启动器专用 .minecraft）
    mc = os.path.join(install_dir, ".minecraft")
    if os.path.isdir(mc) or is_link(mc):
        note = "目录联接（只摘链接，不动真实数据）" if is_link(mc) else ""
        items.append({"key": "app_mc", "label": "应用路径 .minecraft（本启动器专用）",
                      "paths": [mc], "size": entry_size(mc), "keep": True,
                      "can_keep": True, "kind": "dir", "note": note})

    # ③ 官方启动器目录 official_assets
    off_root, outside = _official_root(install_dir)
    if off_root:
        note = "位置在安装目录之外" if outside else ""
        items.append({"key": "official", "label": f"官方游戏目录 {paths.OFFICIAL_DIR}",
                      "paths": [off_root], "size": entry_size(off_root), "keep": True,
                      "can_keep": True, "kind": "dir", "note": note})

    # ④ 用户数据 / 配置 / 账号（新名字 + 可能残留的老名字目录）
    if datas:
        items.append({"key": "data", "label": "配置与账号数据（bgl_datat_config）",
                      "paths": datas, "size": sum(entry_size(p) for p in datas),
                      "keep": True, "can_keep": True, "kind": "dir",
                      "note": "保留的话重装不用重新登录"})

    # ⑤ 快捷方式（总是删）
    lns = []
    try:
        d = winlnk.desktop_dir()
        s = winlnk.startmenu_dir()
        # 主程序图标 + 开始菜单里那个「卸载」入口（第五十一轮）都要清
        names = [f"{APP_NAME}.lnk", f"卸载{APP_NAME}.lnk"]
        lns = [os.path.join(d, n) for n in names if d and os.path.isfile(os.path.join(d, n))]
        lns += [os.path.join(s, n) for n in names if s and os.path.isfile(os.path.join(s, n))]
    except Exception:                                  # noqa: BLE001
        pass
    if lns:
        items.append({"key": "lnk", "label": "桌面 / 开始菜单快捷方式", "paths": lns,
                      "size": sum(entry_size(p) for p in lns), "keep": False,
                      "can_keep": False, "kind": "files", "note": "只要卸载就一定会删"})
    return items, install_dir, datas


# ─────────────────────────────── 执行卸载 ─────────────────────────────────
def do_uninstall(items, install_dir, data_dir, self_exe, progress=None):
    """按 items 的 keep 标记删除。返回结果字典（含需要交给 bat 重试的路径）。"""
    # 快捷方式也要删得掉：它不在安装/数据目录里，得按确切路径单独放行
    roots = allowed_roots(install_dir, data_dir, shortcut_paths())
    res = {"removed": [], "kept": [], "errors": [], "retry": [], "self": self_exe}
    todo = [it for it in items if not it.get("keep")]
    total = max(1, sum(len(it["paths"]) for it in todo))
    done = 0
    for it in items:
        if it.get("keep"):
            res["kept"].append(it)
    for it in todo:
        for p in it["paths"]:
            done += 1
            if progress:
                try:
                    progress(int(done * 100 / total), f"删除 {os.path.basename(p)}…")
                except Exception:                      # noqa: BLE001
                    pass
            tgt = os.path.normcase(os.path.abspath(p))
            if os.path.normcase(os.path.abspath(self_exe)) == tgt:
                res["retry"].append(p)                 # 自己删不掉，最后交给 bat
                continue
            if remove_any(p, roots, res["errors"]):
                res["removed"].append(p)
            else:
                res["retry"].append(p)                 # 可能被占用，bat 再试一次
    # 安装目录：如果删空了，连目录一起清掉（否则会把玩家的数据目录带走）
    try:
        left = [x for x in os.listdir(install_dir)
                if os.path.normcase(os.path.abspath(os.path.join(install_dir, x)))
                != os.path.normcase(os.path.abspath(self_exe))]
        if not left:
            res["install_dir"] = install_dir
    except OSError:
        pass
    return res


def schedule_self_delete(paths_to_retry, install_dir="", self_exe=""):
    """运行中的 exe 删不掉 → 写个延时 bat，等本进程退出后再删干净。"""
    if os.name != "nt":
        return ""
    self_exe = self_exe or self_exe_path()
    work = list(paths_to_retry or [])
    if self_exe and os.path.normcase(self_exe) not in \
            {os.path.normcase(p) for p in work}:
        work.append(self_exe)
    try:
        tmpd = tempfile.mkdtemp(prefix="bgl_uninst_")
    except OSError:
        return ""
    bat = os.path.join(tmpd, "cleanup.bat")
    lines = ["@echo off", "chcp 936 >nul",
             "ping -n 4 127.0.0.1 >nul"]          # 等本进程退出（约 3 秒）
    for p in work:
        if not p:
            continue
        if os.path.isdir(p):
            lines.append(f'if exist "{p}" rmdir /s /q "{p}"')
        else:
            lines.append(f'if exist "{p}" del /f /q "{p}"')
    if install_dir:
        lines.append(f'if exist "{install_dir}" rmdir /s /q "{install_dir}"')
    lines += ['del /f /q "%~f0"', "exit"]
    try:
        # cmd 的默认代码页是 GBK，中文路径必须按 GBK 写，否则 bat 里全是乱码
        with open(bat, "w", encoding="gbk", errors="replace") as f:
            f.write("\r\n".join(lines) + "\r\n")
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(["cmd", "/c", bat], creationflags=flags, close_fds=True)
        return bat
    except Exception:                                  # noqa: BLE001
        return ""


# ─────────────────────────────── 界面 ─────────────────────────────────────
def _bg_window():
    return _win_ref


_win_ref = None


class Card(QWidget):
    """半透明圆角卡片（跟安装向导同一套观感）"""

    def __init__(self, parent):
        super().__init__(parent)
        self.radius = 18
        self.tint = 236

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), self.radius, self.radius)
        p.setClipPath(path)
        win = _bg_window()
        if getattr(win, "_bg", None):
            pos = self.mapTo(win, self.rect().topLeft())
            x0, y0 = max(0, pos.x()), max(0, pos.y())
            x1 = min(win._bg.width(), pos.x() + self.width())
            y1 = min(win._bg.height(), pos.y() + self.height())
            if x1 > x0 and y1 > y0:
                from PySide6.QtCore import QRectF as RF
                p.drawImage(RF(x0 - pos.x(), y0 - pos.y(), x1 - x0, y1 - y0),
                            win._bg, RF(x0, y0, x1 - x0, y1 - y0))
        p.fillRect(self.rect(), QColor(255, 255, 255, self.tint))
        p.setPen(QPen(QColor(255, 255, 255, 150), 1))
        p.drawPath(path)


class Pill(QWidget):
    """胶囊按钮（自绘；kind=accent 蓝底白字 / danger 红底白字 / ghost 白底）"""

    clicked = Signal()

    def __init__(self, text, kind="ghost", parent=None):
        super().__init__(parent)
        self.kind = kind
        self._text = text
        self._down = False
        self._hover = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(36)
        self.setMinimumWidth(84)
        self.setFocusPolicy(Qt.NoFocus)
        self._enabled = True

    def setText(self, t):
        self._text = t
        self.update()

    def text(self):
        return self._text

    def setEnabled(self, v):                              # noqa: N802
        self._enabled = bool(v)
        self.setCursor(Qt.PointingHandCursor if v else Qt.ArrowCursor)
        self.update()

    def isEnabled(self):                                  # noqa: N802
        return self._enabled

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._enabled:
            self._down = True
            self.update()

    def mouseReleaseEvent(self, e):
        was = self._down
        self._down = False
        self.update()
        if was and self._enabled and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if not self._enabled:
            p.setBrush(QColor(255, 255, 255, 90))
            p.setPen(QPen(QColor(255, 255, 255, 90), 1))
        elif self.kind == "danger":
            base = QColor("#c0392b")
            if self._hover:
                base = base.lighter(112)
            if self._down:
                base = base.darker(112)
            p.setBrush(base)
            p.setPen(Qt.NoPen)
        elif self.kind == "accent":
            p.setBrush(QColor(theme.C_ACCENT_DOWN if self._down else
                              (theme.C_ACCENT_HOVER if self._hover else theme.C_ACCENT)))
            p.setPen(Qt.NoPen)
        else:
            p.setBrush(QColor(255, 255, 255, 190 if self._hover else 150))
            p.setPen(QPen(QColor(255, 255, 255, 150), 1))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        if self.kind in ("accent", "danger") and self._enabled:
            p.setPen(QColor("#ffffff"))
        elif not self._enabled:
            p.setPen(QColor("#9aa5b1"))
        else:
            p.setPen(QColor(theme.C_TEXT))
        f = QFont(theme.FONT)
        f.setPixelSize(13)
        f.setBold(self.kind in ("accent", "danger"))
        p.setFont(f)
        p.drawText(r, Qt.AlignCenter, self._text)


class Switch(QWidget):
    """胶囊开关（= 保留 / 不保留）"""

    toggled = Signal(bool)

    def __init__(self, on=False, parent=None, w=46, h=24):
        super().__init__(parent)
        self.setFixedSize(w, h)
        self.setCursor(Qt.PointingHandCursor)
        self._on = bool(on)
        self.setToolTip("点击切换")

    def isChecked(self):
        return self._on

    def setChecked(self, v):                              # noqa: N802
        v = bool(v)
        if v != self._on:
            self._on = v
            self.update()
            self.toggled.emit(v)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.setChecked(not self._on)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height()
        r = QRectF(0.5, 0.5, self.width() - 1, h - 1)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#2f7cf6") if self._on else QColor(190, 198, 208))
        p.drawRoundedRect(r, h / 2, h / 2)
        knob = h - 6
        x = (self.width() - knob - 3) if self._on else 3
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(x, 3, knob, knob))


class KeepRow(QWidget):
    """一行：开关 + 名称 + 路径 + 大小"""

    def __init__(self, item, parent=None):
        super().__init__(parent)
        self.item = item
        self.setObjectName("keepRow")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(10)
        if item.get("can_keep", True):
            self.sw = Switch(bool(item.get("keep")))
            self.sw.toggled.connect(self._on_switch)
            top.addWidget(self.sw)
        else:
            self.sw = None
            lock = QLabel("—")
            lock.setFixedWidth(46)
            lock.setAlignment(Qt.AlignCenter)
            lock.setStyleSheet("color:#9aa5b1;font-size:12px;")
            top.addWidget(lock)
        name = QLabel(item.get("label") or "")
        f = QFont(theme.FONT)
        f.setPixelSize(13)
        f.setBold(True)
        name.setFont(f)
        name.setStyleSheet("color:#1f2733;")
        top.addWidget(name, 1)
        size = QLabel(human_size(item.get("size") or 0))
        size.setStyleSheet("color:#78848f;font-size:12px;")
        top.addWidget(size)
        lay.addLayout(top)
        sub = QLabel(self._sub_text())
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#78848f;font-size:11px;")
        lay.addWidget(sub)
        self.setStyleSheet("")     # 行内不设背景，避免级联命中子标签

    def _sub_text(self):
        it = self.item
        ps = it.get("paths") or []
        first = ps[0] if ps else ""
        more = "" if len(ps) <= 1 else f"（还有 {len(ps) - 1} 项）"
        note = it.get("note") or ""
        tail = f"  ·  {note}" if note else ""
        return f"{first}{more}{tail}"

    def _on_switch(self, on):
        self.item["keep"] = bool(on)
        win = self.window()
        if hasattr(win, "refresh_summary"):
            win.refresh_summary()

    def mousePressEvent(self, e):
        # 点空白/文字也能切（点在开关本身上时，事件被开关自己收走）
        if self.sw is not None and e.button() == Qt.LeftButton:
            p = e.position().toPoint()
            if not self.sw.geometry().contains(p):
                self.sw.setChecked(not self.sw.isChecked())


class Uninstaller(QWidget):
    progress_sig = Signal(int, str)
    done_sig = Signal(object)

    def __init__(self):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint)
        global _win_ref
        _win_ref = self
        self.progress_sig.connect(self._on_progress)
        self.done_sig.connect(self._on_done)
        self.setWindowTitle(f"卸载 {APP_NAME}")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(660, 600)
        self.setMinimumSize(620, 560)
        self._bg = None
        self._drag = None
        self.result = None
        self.busy = False

        self.items, self.install_dir, self.data_dir = collect_items()
        self.self_exe = self_exe_path()

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 14)

        tb = QHBoxLayout()
        t = QLabel(f"卸载 {APP_NAME}   v{APP_VERSION}")
        tf = QFont(theme.FONT)
        tf.setPixelSize(15)
        tf.setBold(True)
        t.setFont(tf)
        t.setStyleSheet("color:#f2f5fa;")
        tb.addWidget(t)
        tb.addStretch(1)
        x = Pill("×", "ghost")
        x.setFixedSize(34, 34)
        x.clicked.connect(self.close)
        tb.addWidget(x)
        root.addLayout(tb)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.stack.addWidget(self._page_pick())
        self.stack.addWidget(self._page_confirm())
        self.stack.addWidget(self._page_progress())
        self.stack.addWidget(self._page_result())
        self.stack.setCurrentIndex(0)

    # ---------- 页 0：勾选保留 ----------
    def _page_pick(self):
        c = Card(self)
        lay = QVBoxLayout(c)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(8)
        title = QLabel("选择要保留的数据")
        f = QFont(theme.FONT)
        f.setPixelSize(18)
        f.setBold(True)
        title.setFont(f)
        title.setStyleSheet("color:#1f2733;")
        lay.addWidget(title)
        hint = QLabel("开关打开 = 保留，关闭 = 删除。程序本体和快捷方式总是会被删除。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#4c5866;font-size:12px;")
        lay.addWidget(hint)
        lay.addSpacing(4)

        self.rows = []
        for it in self.items:
            row = KeepRow(it, c)
            lay.addWidget(row)
            self.rows.append(row)

        lay.addSpacing(6)
        self.lb_sum = QLabel("")
        self.lb_sum.setWordWrap(True)
        self.lb_sum.setStyleSheet("color:#c0392b;font-size:12px;")
        lay.addWidget(self.lb_sum)
        lay.addStretch(1)

        nav = QHBoxLayout()
        b_keep_all = Pill("全部保留")
        b_keep_all.clicked.connect(lambda: self._set_all(True))
        nav.addWidget(b_keep_all)
        b_del_all = Pill("全部不保留")
        b_del_all.clicked.connect(lambda: self._set_all(False))
        nav.addWidget(b_del_all)
        nav.addStretch(1)
        b_cancel = Pill("取消")
        b_cancel.clicked.connect(self.close)
        nav.addWidget(b_cancel)
        b_next = Pill("卸载", "danger")
        b_next.clicked.connect(lambda: self.stack.setCurrentIndex(1))
        nav.addWidget(b_next)
        lay.addLayout(nav)
        self.refresh_summary()
        return c

    def _set_all(self, keep):
        for it in self.items:
            if it.get("can_keep", True):
                it["keep"] = keep
        for r in self.rows:
            if r.sw is not None:
                r.sw.setChecked(keep)
        self.refresh_summary()

    def refresh_summary(self):
        # 「将删除」只统计**可保留但被勾掉**的数据；程序本体/快捷方式总是删，
        # 单独用一句话说明，不混进体积里（免得玩家以为删了那么多数据）。
        drop = [it for it in self.items
                if it.get("can_keep", True) and not it.get("keep")]
        always = [it for it in self.items if not it.get("can_keep", True)]
        tail = ""
        if any(it.get("key") == "lnk" for it in always):
            tail = "（另外还会删除程序本体和快捷方式）"
        elif always:
            tail = "（另外还会删除程序本体）"
        size = sum(it.get("size") or 0 for it in drop)
        if not drop:
            self.lb_sum.setText("数据全部保留，只卸载程序本身" + tail)
            self.lb_sum.setStyleSheet("color:#4c5866;font-size:12px;")
        else:
            names = "、".join(it.get("label", "") for it in drop)
            self.lb_sum.setText(
                f"将删除：{names}　共 {human_size(size)}，不可恢复{tail}")
            self.lb_sum.setStyleSheet("color:#c0392b;font-size:12px;")
        self.lb_sum.setWordWrap(True)

    # ---------- 页 1：确认 ----------
    def _page_confirm(self):
        c = Card(self)
        lay = QVBoxLayout(c)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(8)
        title = QLabel("确认卸载")
        f = QFont(theme.FONT)
        f.setPixelSize(18)
        f.setBold(True)
        title.setFont(f)
        title.setStyleSheet("color:#1f2733;")
        lay.addWidget(title)
        self.lb_conf_del = QLabel("")
        self.lb_conf_del.setWordWrap(True)
        self.lb_conf_del.setStyleSheet("color:#c0392b;font-size:12px;")
        lay.addWidget(self.lb_conf_del)
        self.lb_conf_keep = QLabel("")
        self.lb_conf_keep.setWordWrap(True)
        self.lb_conf_keep.setStyleSheet("color:#2f7cf6;font-size:12px;")
        lay.addWidget(self.lb_conf_keep)
        tip = QLabel("删除之后无法恢复。存档 / 官方目录想留着的话，点「返回」把对应开关打开。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#78848f;font-size:11px;")
        lay.addWidget(tip)
        lay.addStretch(1)
        nav = QHBoxLayout()
        nav.addStretch(1)
        back = Pill("返回")
        back.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        nav.addWidget(back)
        go = Pill("确认卸载", "danger")
        go.clicked.connect(self._start)
        nav.addWidget(go)
        lay.addLayout(nav)
        return c

    def _refresh_confirm(self):
        drop = [it for it in self.items if not it.get("keep")]
        keep = [it for it in self.items if it.get("keep")]
        if drop:
            self.lb_conf_del.setText("将被删除：" + "；".join(
                f"{it['label']}（{human_size(it.get('size') or 0)}）" for it in drop))
        else:
            self.lb_conf_del.setText("将被删除：程序本体、快捷方式")
        self.lb_conf_keep.setText("将保留：" + ("；".join(it["label"] for it in keep)
                                              if keep else "（无）"))

    # ---------- 页 2：进度 ----------
    def _page_progress(self):
        c = Card(self)
        lay = QVBoxLayout(c)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        self.lb_p_title = QLabel("正在卸载…")
        f = QFont(theme.FONT)
        f.setPixelSize(18)
        f.setBold(True)
        self.lb_p_title.setFont(f)
        self.lb_p_title.setStyleSheet("color:#1f2733;")
        lay.addWidget(self.lb_p_title)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setFixedHeight(10)
        self.bar.setTextVisible(False)
        self.bar.setStyleSheet(
            "QProgressBar{background:rgba(255,255,255,170);border:1px solid rgba(255,255,255,150);"
            "border-radius:5px;}"
            "QProgressBar::chunk{background:#c0392b;border-radius:5px;}")
        lay.addWidget(self.bar)
        self.lb_p_status = QLabel("准备中…")
        self.lb_p_status.setWordWrap(True)
        self.lb_p_status.setStyleSheet("color:#5a6675;font-size:12px;")
        lay.addWidget(self.lb_p_status)
        lay.addStretch(1)
        return c

    # ---------- 页 3：结果 ----------
    def _page_result(self):
        c = Card(self)
        lay = QVBoxLayout(c)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(8)
        self.lb_r_title = QLabel("卸载完成")
        f = QFont(theme.FONT)
        f.setPixelSize(18)
        f.setBold(True)
        self.lb_r_title.setFont(f)
        self.lb_r_title.setStyleSheet("color:#1f2733;")
        lay.addWidget(self.lb_r_title)
        self.lb_r_body = QLabel("")
        self.lb_r_body.setWordWrap(True)
        self.lb_r_body.setStyleSheet("color:#4c5866;font-size:12px;")
        lay.addWidget(self.lb_r_body)
        lay.addStretch(1)
        nav = QHBoxLayout()
        nav.addStretch(1)
        self.btn_finish = Pill("完成", "accent")
        self.btn_finish.clicked.connect(self._finish)
        nav.addWidget(self.btn_finish)
        lay.addLayout(nav)
        return c

    # ---------- 流程 ----------
    def _start(self):
        if self.busy:
            return
        self.busy = True
        self.stack.setCurrentIndex(2)
        self.bar.setValue(0)
        self.lb_p_status.setText("正在删除…")

        def work():
            try:
                def prog(v, t):
                    self.progress_sig.emit(int(v), str(t))
                res = do_uninstall(self.items, self.install_dir, self.data_dir,
                                   self.self_exe, progress=prog)
                self.progress_sig.emit(100, "收尾…")
                self.done_sig.emit(res)
            except Exception as e:                     # noqa: BLE001
                self.done_sig.emit({"errors": [("", f"{type(e).__name__}: {e}")],
                                    "removed": [], "kept": [], "retry": []})
        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, v, t):
        self.bar.setValue(max(0, min(100, int(v))))
        self.lb_p_status.setText(t)

    def _on_done(self, res):
        self.result = res or {}
        self.busy = False
        self.stack.setCurrentIndex(3)
        rm = self.result.get("removed") or []
        kp = self.result.get("kept") or []
        er = self.result.get("errors") or []
        rt = self.result.get("retry") or []
        lines = []
        if rm:
            names = "、".join(os.path.basename(p.rstrip("\\/")) or p for p in rm[:6])
            lines.append(f"已删除 {len(rm)} 项：{names}" + ("…" if len(rm) > 6 else ""))
        if kp:
            lines.append("已保留：" + "、".join(it.get("label", "") for it in kp))
        if rt:
            lines.append(f"{len(rt)} 项正被占用，已安排退出后清理（关掉启动器即可）")
        if er:
            lines.append(f"{len(er)} 项没能删除，例如：{er[0][0]}（{er[0][1]}）")
        if not lines:
            lines.append("没有需要删除的内容。")
        lines.append("")
        lines.append("点「完成」退出" + ("；本卸载程序会在退出后自动删除自己。"
                                     if IS_FROZEN else "。"))
        self.lb_r_body.setText("\n".join(lines))
        if er:
            self.lb_r_title.setText("卸载完成，但有项目没删掉")
            self.lb_r_title.setStyleSheet("color:#c0392b;")

    def _finish(self):
        res = self.result or {}
        retry = list(res.get("retry") or [])
        inst = res.get("install_dir") or ""
        if IS_FROZEN and (retry or os.path.isfile(self.self_exe) or inst):
            schedule_self_delete(retry, inst, self.self_exe)
        self.close()
        QApplication.quit()

    # ---------- 窗口：拖动 / 背景 ----------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() < 52:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def resizeEvent(self, e):
        self._bg = None
        self._regen()

    def _regen(self):
        from PySide6.QtCore import QRect
        w, h = max(self.width(), 1), max(self.height(), 1)
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(0.97)
        paint_background(p, QRect(0, 0, w, h))
        p.end()
        self._bg = img

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        p.setClipPath(path)
        if self._bg:
            p.drawImage(0, 0, self._bg)
        p.setClipping(False)
        p.setPen(QColor(255, 255, 255, 130))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

    def showEvent(self, e):
        super().showEvent(e)
        self._regen()
        self._refresh_confirm()
        self.stack.currentChanged.connect(self._on_page)


    def _on_page(self, i):
        if i == 1:
            self._refresh_confirm()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(f"卸载 {APP_NAME}")
    app.setFont(QFont(theme.FONT, 10))
    app.setStyleSheet(theme.qss())
    try:
        theme.apply_light_tooltip(app)
        from app.ui import tipbox
        tipbox.install(app)
    except Exception:                                  # noqa: BLE001
        pass
    w = Uninstaller()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
