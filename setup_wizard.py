# -*- coding: utf-8 -*-
"""中文安装向导：欢迎 -> 路径与游戏目录 -> 选项 -> 安装 -> 完成
安装内容：主程序 exe、开始菜单/桌面快捷方式、config.json（游戏目录等）
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget, QCheckBox, QMenu)

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import (APP_NAME, APP_VERSION, INSTALL_DIR_NAME,
                             LEGACY_INSTALL_NAMES, UNINSTALL_EXE_NAME, app_data_dir)
from app.core import gamedir
from app.core import paths
from app.core import winacrylic
from app.core.installer import find_launcher_exe
from app.ui import theme
from app.ui.theme import paint_background

IS_FROZEN = getattr(sys, "frozen", False)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 第四十九轮：安装向导必须**自带**主程序 ────────────────────────────────
# 以前向导只会在「自己旁边」找 `八桂启动器.exe`。别人单独拿到一个新版向导时，
# 那里根本没有主程序（或者躺着上一版的老 exe），于是「向导是新版、装完还是旧版」，
# 甚至直接复制失败。现在 build.py 用 `--add-data` 把主程序 exe + 清单一起打进
# 向导包里，向导优先用包内的那一份，并**按 SHA256 校验**，保证装上去的就是它自带的。
PAYLOAD_DIR = "payload"             # 包内目录（onefile 解包后 <_MEIPASS>\payload）
PAYLOAD_META = "payload_meta.json"  # 清单：name / version / size / sha256
MIN_PAYLOAD_SIZE = 8 * 1024 * 1024  # 真 exe 至少这么大，拦掉 0 字节 / 占位文件


class PayloadError(RuntimeError):
    """随包主程序缺失或校验不过 —— 明确报错，绝不拿旧文件顶替。"""

    def __init__(self, why, how):
        super().__init__(why)
        self.why = why
        self.how = how


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _read_meta(root):
    try:
        with open(os.path.join(root, PAYLOAD_META), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _copy_one(src, dst, want_sha=""):
    """单个文件：原子替换 + 回读校验（旧版正开着也能覆盖 → 改名让位）。"""
    if os.path.abspath(src) == os.path.abspath(dst):
        return                                    # 自己覆盖自己，跳过
    tmp = dst + ".new"
    shutil.copy2(src, tmp)
    for _ in range(3):
        try:
            os.replace(tmp, dst)                  # 正常路径：原子替换
            break
        except OSError:
            # 旧版正开着 → 覆盖会失败，但**改名**是允许的：让它让位
            old = dst + ".old"
            try:
                if os.path.exists(old):
                    os.remove(old)
                if os.path.exists(dst):
                    os.replace(dst, old)
            except OSError:
                pass
    if os.path.exists(tmp):                       # 三轮都没成功
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise OSError(32, "文件写不进去（可能正开着旧版）", dst)
    if want_sha:                                  # 回读校验：确认落地的是包里那个
        try:
            got = _sha256(dst)
        except OSError:
            got = ""
        if got and got != want_sha.lower():
            raise PayloadError(
                f"{os.path.basename(dst)} 写入后校验不一致（可能被杀毒软件拦了）",
                f"把安装目录里的 {os.path.basename(dst)} 删掉，"
                "关掉杀毒实时防护后再装一次。")


def payload_roots():
    """可能放着「随包主程序」的目录，按可信度排序。

    ① 向导包自带（onefile 解包目录里的 `payload/`）—— 唯一能保证版本一致的来源；
    ② 向导 exe 旁边（把两个 exe 摆一起的老分发方式 / 开发调试）；
    ③ 源码树 `dist/`。
    """
    roots = []
    if IS_FROZEN:
        meipass = getattr(sys, "_MEIPASS", "") or ""
        me = os.path.dirname(sys.executable)
        for base in (meipass, me):
            if not base:
                continue
            for sub in (PAYLOAD_DIR, "app", ""):
                p = os.path.join(base, sub) if sub else base
                if p not in roots:
                    roots.append(p)
    else:
        roots += [os.path.join(HERE, "dist"), HERE]
    return roots


def _payload_files(root, meta):
    """清单 → 每个要装进安装目录的文件（含校验结果）。

    第五十一轮起清单里带 `files`（主程序 + 卸载程序）；老清单只有
    name/size/sha256 一个主程序，这里兼容处理。
    """
    lst = meta.get("files")
    if not isinstance(lst, list) or not lst:
        lst = [{"name": meta.get("name") or f"{APP_NAME}.exe",
                "size": meta.get("size"), "sha256": meta.get("sha256")}]
    out = []
    for ent in lst:
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("name") or "")
        if not name:
            continue
        p = os.path.join(root, name)
        item = {"src": p, "name": name,
                "size": int(ent.get("size") or 0),
                "sha256": str(ent.get("sha256") or ""), "ok": False}
        if os.path.isfile(p):
            item["ok"] = True
            try:
                if item["size"] and os.path.getsize(p) != item["size"]:
                    item["ok"] = False
                if item["ok"] and item["sha256"] and \
                        _sha256(p).lower() != item["sha256"].lower():
                    item["ok"] = False
            except OSError:
                item["ok"] = False
        out.append(item)
    return out


def resolve_payload():
    """找到要装的文件 → {exe, version, verified, files, error}。

    `verified=True` = 主程序和随包清单（大小 + SHA256）完全对得上，
    也就是「装上去的一定是向导自带的那个版本」。
    `files` = 全部要装的 exe（主程序排第一），每项带 `ok`（该文件校验过没过）。
    """
    unverified = ""
    broken = []
    for root in payload_roots():
        meta = _read_meta(root)
        name = (meta or {}).get("name") or f"{APP_NAME}.exe"
        p = os.path.join(root, name)
        if not os.path.isfile(p):
            continue
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        if size < MIN_PAYLOAD_SIZE:      # 空壳 / 没下载完的占位文件，跳过
            continue
        if meta:
            files = _payload_files(root, meta)
            primary = next((f for f in files if f["name"] == name), None)
            if not primary or not primary["ok"]:
                broken.append(p)
                continue
            return {"exe": p,
                    "version": str(meta.get("version") or APP_VERSION),
                    "verified": True, "error": None, "files": files}
        if not unverified:               # 没有清单（老分发方式 / 开发模式）
            unverified = p
    if broken:
        # 包里明明有清单却对不上 = 安装包坏了。**不许**退回去用旁边那个
        # 「没有清单的 exe」——那正是「装出来还是旧版」的老路。
        return {"exe": "", "version": APP_VERSION, "verified": False, "files": [],
                "error": PayloadError(
                    "安装包不完整：随包的主程序校验不通过（文件可能在传输中损坏了）",
                    "重新下载一次安装向导再运行 —— 别用被截断或被改过的包，"
                    "也不要用旁边那个旧的主程序 exe 顶替。")}
    files = []
    if unverified:
        files = [{"src": unverified, "name": os.path.basename(unverified),
                  "size": 0, "sha256": "", "ok": True}]
    return {"exe": unverified, "version": APP_VERSION,
            "verified": False, "error": None, "files": files}


GUIDE_DOCX = "八桂启动器使用指南.docx"
GUIDE_PDF = "八桂启动器使用指南.pdf"
# 第一百一十六轮：简繁各一份 docx+pdf，共 4 份
GUIDE_DOCX_TRAD = "八桂启动器使用指南-繁体.docx"
GUIDE_PDF_TRAD = "八桂启动器使用指南-繁体.pdf"
GUIDE_FILES = (GUIDE_DOCX, GUIDE_PDF, GUIDE_DOCX_TRAD, GUIDE_PDF_TRAD)


def guide_sources():
    """随包指南文档的候选来源（简体/繁体各 docx+pdf 共 4 份）。

    PyInstaller 把 `guide/` 解到 `_MEIPASS/guide/`（也兼容向导 exe 旁边）；
    源码运行就是项目根的 `guide/`。返回 [(src, dest_name), ...] 只含真实存在的。
    """
    bases = []
    if IS_FROZEN:
        meipass = getattr(sys, "_MEIPASS", "") or ""
        me = os.path.dirname(sys.executable)
        bases += [os.path.join(meipass, "guide"), os.path.join(me, "guide")]
    else:
        bases += [os.path.join(HERE, "guide")]
    out = []
    for base in bases:
        for name in GUIDE_FILES:
            p = os.path.join(base, name)
            if os.path.isfile(p) and p not in [x[0] for x in out]:
                out.append((p, name))
        if out:                       # 第一个命中的目录就够了
            break
    return out


def base_dir():
    """待安装的主程序 exe 所在目录（旧调用方兼容）。"""
    info = resolve_payload()
    if info["exe"]:
        return os.path.dirname(info["exe"])
    roots = payload_roots()
    return roots[0] if roots else HERE


def _programs_dir() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                        "Programs")


def _migrate_install_dir(base: str, dst: str) -> bool:
    """把老的（中文名）安装目录整体改名成 dst。

    同卷直接 os.replace（瞬时）；被占用就退化成逐项复制 —— 安装目录里只有 exe 和
    少量配置，复制很便宜。成功返回 True。
    """
    for old in LEGACY_INSTALL_NAMES:
        src = os.path.join(base, old)
        if not os.path.isdir(src) or os.path.normcase(src) == os.path.normcase(dst):
            continue
        if os.path.isdir(dst):
            return True                     # 新目录已存在，不覆盖
        try:
            os.replace(src, dst)            # 同卷改名
            return True
        except OSError:
            pass
        try:
            shutil.copytree(src, dst, dirs_exist_ok=True)
            return True
        except Exception:
            continue
    return False


def default_install_dir() -> str:
    """默认安装位置：`%LOCALAPPDATA%\\Programs\\Osmanthus flower`。

    老玩家的中文目录（八桂启动器 / MC官启辅助工具）会在这里被整体改名搬过去，
    避免「新旧两份并存、点开还是旧版」。

    注意区分两个「八桂启动器」：这里说的是**安装目录**（放 exe）；
    用户数据/配置目录（放 config.json、账号）在 `%APPDATA%` 下，
    第四十八轮起叫 `bgl_datat_config`（见 core/config.py 的 DATA_DIR_NAME）。
    """
    base = _programs_dir()
    dst = os.path.join(base, INSTALL_DIR_NAME)
    if os.path.isdir(dst):
        return dst
    _migrate_install_dir(base, dst)
    return dst


def remembered_install_dir() -> str:
    """第一一二轮：上一次安装时玩家选的路径（写在数据目录的 config.json 里）。

    重装 / 升级时路径框默认带上前一次的位置，而不是又回到
    `%LOCALAPPDATA%\\Programs` 的默认值。config.json 在 `%APPDATA%\\bgl_datat_config`
    下、独立于安装目录，卸载重装也不会丢。目录已经不存在（被删/搬走）就返回
    空串，回落 default_install_dir()。读原始 JSON 而不是 CONFIG 单例：
    向导是独立进程，别为读一个路径把整个配置层拉起来。
    """
    try:
        p = os.path.join(app_data_dir(), "config.json")
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        v = str(d.get("install_dir") or "").strip()
        if v and os.path.isdir(v):
            return os.path.normpath(v)
    except Exception:                                    # noqa: BLE001
        pass
    return ""


class Card(QWidget):
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
        if getattr(win_ref, "_bg", None):
            pos = self.mapTo(win_ref, self.rect().topLeft())
            x0, y0 = max(0, pos.x()), max(0, pos.y())
            x1 = min(win_ref._bg.width(), pos.x() + self.width())
            y1 = min(win_ref._bg.height(), pos.y() + self.height())
            if x1 > x0 and y1 > y0:
                from PySide6.QtCore import QRectF as RF
                p.drawImage(RF(x0 - pos.x(), y0 - pos.y(), x1 - x0, y1 - y0),
                            win_ref._bg, RF(x0, y0, x1 - x0, y1 - y0))
        p.fillRect(self.rect(), QColor(255, 255, 255, self.tint))
        p.setPen(QPen(QColor(255, 255, 255, 150), 1))
        p.drawPath(path)


class Pill(QPushButton):
    def __init__(self, text, kind="ghost", parent=None):
        super().__init__(parent)
        self.kind = kind
        self._text = text
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(36)
        self.setMinimumWidth(84)
        self.setFocusPolicy(Qt.NoFocus)

    def setText(self, t):
        self._text = t
        self.update()

    def text(self):
        return self._text

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.kind == "accent":
            p.setBrush(QColor(theme.C_ACCENT_DOWN if self.isDown() else
                              (theme.C_ACCENT_HOVER if self.underMouse() else theme.C_ACCENT)))
            p.setPen(Qt.NoPen)
        else:
            p.setBrush(QColor(255, 255, 255, 190 if self.underMouse() else 150))
            p.setPen(QPen(QColor(255, 255, 255, 150), 1))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setPen(QColor("#ffffff") if self.kind == "accent" else QColor(theme.C_TEXT))
        f = QFont(theme.FONT)
        f.setPixelSize(13)
        f.setBold(self.kind == "accent")
        p.setFont(f)
        p.drawText(r, Qt.AlignCenter, self._text)


win_ref = None


class Wizard(QWidget):
    # 工作线程 -> 主线程（Signal 经事件循环派发，避免跨线程 QTimer 失效）
    progress_sig = Signal(int, str)
    install_done_sig = Signal()
    fail_sig = Signal(object, str)          # 异常对象 + 卡在哪

    def __init__(self):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint)
        global win_ref
        win_ref = self
        self.progress_sig.connect(self._on_progress)
        self.install_done_sig.connect(self._on_install_done)
        self.fail_sig.connect(self._fail)
        self.setWindowTitle(f"{APP_NAME} 安装向导")
        self.setWindowIcon(_app_icon())
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(600, 560)
        self.setMinimumSize(600, 560)

        self._bg = None
        self._drag = None

        # 第一一二轮：优先用上一次安装的路径（config.json 的 install_dir），
        # 没装过 / 目录没了才回落到 %LOCALAPPDATA%\Programs 的默认位置
        default_dir = remembered_install_dir() or default_install_dir()
        self.install_dir = default_dir
        self._own_snapshot = os.path.join(default_dir, ".minecraft")
        self._off_snapshot = os.path.join(default_dir, paths.OFFICIAL_DIR, ".minecraft")
        # 应用路径（本启动器专用）由安装目录推导；官方路径可以在界面上改
        self.app_dir = self._own_snapshot
        self.official_dir = self._off_snapshot
        self.game_dir = ""                  # 兼容旧字段：等于应用路径
        self.game_note = ""
        self.shortcut_note = []          # 快捷方式失败原因（第五十轮：必须说出来）
        # 随包主程序：exe / 版本 / 是否按清单校验过（见 resolve_payload）
        _pl = resolve_payload()
        self.src_exe = _pl["exe"]
        self.src_files = list(_pl.get("files") or [])
        self.src_version = _pl["version"]
        self.src_verified = _pl["verified"]
        self._src_error = _pl["error"]
        self.payload_note = []           # 随包附属文件装不上时的说明

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 14)

        # 标题栏（可拖动 + 徽标：跟主程序顶部状态栏中间那个是同一张图）
        tb = QHBoxLayout()
        orb = QWidget()
        orb.setFixedSize(40, 36)
        orb.paintEvent_holder = None

        def orb_paint(ev, w=orb):
            from app.ui.branding import paint_badge
            p = QPainter(w)
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            paint_badge(p, QRectF(w.rect()))
            p.end()
        orb.paintEvent = orb_paint
        tb.addWidget(orb)
        t = QLabel(f"{APP_NAME}  v{APP_VERSION}  安装向导")
        tf = QFont(theme.FONT)
        tf.setPixelSize(15)
        tf.setBold(True)
        t.setFont(tf)
        t.setStyleSheet("color:#f2f5fa;")
        tb.addWidget(t)
        tb.addStretch(1)
        # 第 118 轮：「×」左侧加简体 / 繁体▾（字号小），默认跟随系统语言。
        # 与主程序设置窗一致：跟随时点选只临时覆盖，不写死配置。
        from app.core import i18n as _i18n
        self.btn_lang_cn = QPushButton("简体")
        self.btn_lang_tw = QPushButton("繁体▾")
        for b in (self.btn_lang_cn, self.btn_lang_tw):
            f = QFont(theme.FONT)
            f.setPixelSize(10)
            b.setFont(f)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.setStyleSheet("QPushButton{background:transparent;border:none;"
                            "color:#e8eef6;padding:2px 4px;}"
                            "QPushButton:hover{color:#7fb0ff;}")
            b.setFixedHeight(24)
            tb.addWidget(b, 0, Qt.AlignVCenter)
        self.btn_lang_cn.clicked.connect(self._on_wiz_lang_cn)
        self._lang_menu = QMenu(self)
        self._lang_menu.setFont(QFont(theme.FONT, 10))
        for lang_code in (_i18n.TRAD_TW, _i18n.TRAD_HK, _i18n.TRAD_MO):
            self._lang_menu.addAction(_i18n.LANG_LABELS[lang_code],
                                      lambda c=lang_code: self._on_wiz_lang_trad(c))
        self.btn_lang_tw.clicked.connect(self._popup_lang_menu)
        x = Pill("×", "ghost")
        x.setFixedSize(34, 34)
        x.clicked.connect(self.close)
        tb.addWidget(x)
        root.addLayout(tb)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        self.stack.addWidget(self._page_welcome())
        self.stack.addWidget(self._page_paths())
        self.stack.addWidget(self._page_options())
        self.stack.addWidget(self._page_installing())
        self.stack.addWidget(self._page_done())

        nav = QHBoxLayout()
        nav.addStretch(1)
        self.btn_back = Pill("上一步")
        self.btn_back.clicked.connect(self._go_back)
        self.btn_next = Pill("下一步", "accent")
        self.btn_next.clicked.connect(self._go_next)
        nav.addWidget(self.btn_back)
        nav.addWidget(self.btn_next)
        root.addLayout(nav)
        self._update_nav()

    # ---------- 语言切换（第 118 轮）----------
    def _wiz_pick_lang(self, lang):
        # 与主程序第 117 轮一致：跟随时点选只临时覆盖，不写死配置；
        # 已经手动锁定过时才落盘。默认 follow → 向导重启仍跟随系统。
        from app.core import i18n
        if i18n.mode() == "follow":
            i18n.temp_lang(lang)
        else:
            i18n.set_lang(lang)

    def _on_wiz_lang_cn(self):
        from app.core import i18n
        self._wiz_pick_lang(i18n.SIMPLIFIED)

    def _on_wiz_lang_trad(self, lang_code):
        self._wiz_pick_lang(lang_code)

    def _popup_lang_menu(self):
        self._lang_menu.exec(self.btn_lang_tw.mapToGlobal(
            self.btn_lang_tw.rect().bottomLeft()))

    # ---------- 通用小部件 ----------
    def _label(self, text, size=12, bold=False, color=theme.C_TEXT, wrap=True):
        l = QLabel(text)
        f = QFont(theme.FONT)
        f.setPixelSize(size)
        f.setBold(bold)
        l.setFont(f)
        l.setStyleSheet(f"color:{color};")
        l.setWordWrap(wrap)
        return l

    def _card(self):
        c = Card(self)
        lay = QVBoxLayout(c)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(12)
        self.stack.addWidget(c)
        return c, lay

    # ---------- 页面 ----------
    def _page_welcome(self):
        c, lay = self._card()
        lay.addWidget(self._label(f"欢迎使用 {APP_NAME}", 20, True))
        lay.addWidget(self._label(
            "本向导将把辅助工具安装到你的电脑：\n\n"
            "  · 自动安排整合包 / 模组 / 光影 / 材质包到微软官方启动器\n"
            "  · 头像与 3D 角色来自你上传到官方的皮肤\n"
            "  · 支持微软网页登录，全程中文界面\n\n"
            "点击「下一步」选择安装位置。", 12, color="#4c5866"))
        # 版本自证：一眼看出「这个向导自带的到底是哪一版主程序」（第四十九轮）
        if not self.src_exe or self._src_error is not None:
            self.lb_ver = self._label(
                "⚠ 安装包不完整：没有找到随包的主程序，点「下一步」会失败。"
                "请重新下载安装向导，不要只用旧的主程序 exe 顶替。",
                11, color="#c0392b")
        elif not self.src_verified:
            self.lb_ver = self._label(
                f"安装包版本 v{self.src_version}（未校验：用的是向导旁边的 exe）",
                11, color="#a2711a")
        else:
            self.lb_ver = self._label(
                f"安装包版本 v{self.src_version} · 已校验，装上去就是这个版本",
                11, color="#5a6675")
        lay.addWidget(self.lb_ver)
        lay.addStretch(1)
        return c

    def _page_paths(self):
        c, lay = self._card()
        lay.addWidget(self._label("选择安装位置", 16, True))
        lay.addWidget(self._label(
            f"程序会装进 {INSTALL_DIR_NAME} 文件夹（用纯英文名，中文路径容易让游戏启动失败）：",
            11, color="#5a6675"))

        row = QHBoxLayout()
        self.ed_dir = QLineEdit(self.install_dir)
        self.ed_dir.setFixedHeight(34)
        self.ed_dir.setCursorPosition(0)
        self.ed_dir.textChanged.connect(self._dir_changed)
        row.addWidget(self.ed_dir, 1)
        br = Pill("浏览")
        br.setFixedWidth(72)
        br.clicked.connect(self._browse_dir)
        row.addWidget(br)
        lay.addLayout(row)

        # —— 应用路径（本启动器专用）——
        lay.addSpacing(4)
        lay.addWidget(self._label("应用路径（本启动器专用）", 13, True))
        self.ed_app = QLineEdit(self.app_dir)
        self.ed_app.setReadOnly(True)
        self.ed_app.setFixedHeight(34)
        self.ed_app.setCursor(Qt.IBeamCursor)      # 只读但允许选中复制
        self.ed_app.setToolTip("只读：跟着安装位置走（= 安装目录\\.minecraft）")
        self.ed_app.setCursorPosition(0)
        lay.addWidget(self.ed_app)
        self.lb_app_hint = self._label(
            "独立启动、以及「下载游戏」里选「应用」时都落到这里；"
            "它是本启动器自己的 .minecraft，不跟官方混。", 10, color="#78848f")
        lay.addWidget(self.lb_app_hint)

        # —— 官方启动器路径 ——
        lay.addSpacing(6)
        lay.addWidget(self._label("官方启动器路径", 13, True))
        lay.addWidget(self._label(
            f"放在安装目录的 {paths.OFFICIAL_DIR} 文件夹里。官方启动器只认 .minecraft "
            f"这个目录名，以后要迁移 / 备份官方目录，都在这一个子目录里折腾：",
            10, color="#78848f"))
        row2 = QHBoxLayout()
        self.ed_off = QLineEdit(self.official_dir)
        self.ed_off.setFixedHeight(34)
        self.ed_off.setCursorPosition(0)
        self.ed_off.textChanged.connect(self._off_changed)
        row2.addWidget(self.ed_off, 1)
        br2 = Pill("浏览")
        br2.setFixedWidth(72)
        br2.clicked.connect(self._browse_off)
        row2.addWidget(br2)
        lay.addLayout(row2)
        self.game_hint = self._label("", 10, color="#2f7cf6")
        lay.addWidget(self.game_hint)
        self._update_game_hint()
        lay.addStretch(1)
        return c

    def _page_options(self):
        c, lay = self._card()
        lay.addWidget(self._label("附加选项", 16, True))
        self.cb_desktop = QCheckBox("创建桌面快捷方式")
        self.cb_startmenu = QCheckBox("创建开始菜单快捷方式")
        for cb in (self.cb_desktop, self.cb_startmenu):
            cb.setChecked(True)
            cb.setStyleSheet("QCheckBox{color:#1f2733;font-size:13px;} QCheckBox::indicator{width:16px;height:16px;}")
            lay.addWidget(cb)
        lay.addSpacing(8)
        lay.addWidget(self._label("准备就绪，点击「安装」开始。", 12, color="#5a6675"))
        lay.addStretch(1)
        return c

    def _page_installing(self):
        c, lay = self._card()
        self.lb_inst_title = self._label("正在安装…", 16, True)
        lay.addWidget(self.lb_inst_title)
        from PySide6.QtWidgets import QProgressBar
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        self.bar.setStyleSheet("QProgressBar{background:rgba(31,39,51,40);border-radius:5px;}"
                               "QProgressBar::chunk{background:#2f7cf6;border-radius:5px;}")
        lay.addWidget(self.bar)
        self.status = self._label("准备中…", 12, color="#5a6675")
        lay.addWidget(self.status)
        self.lb_err = self._label("", 11, color="#c0392b")
        self.lb_err.setVisible(False)
        lay.addWidget(self.lb_err)
        self.btn_fix = Pill("改用默认目录", "accent")
        self.btn_fix.clicked.connect(self._use_default_dir)
        self.btn_fix.setVisible(False)
        lay.addWidget(self.btn_fix, 0, Qt.AlignLeft)
        lay.addStretch(1)
        return c

    def _page_done(self):
        c, lay = self._card()
        lay.addWidget(self._label("安装完成！", 20, True))
        lay.addWidget(self._label(f"{APP_NAME} 已安装到：", 12, color="#4c5866"))
        # ⚠️ 这四行**必须在装完之后再填**：这一页在 __init__ 里就建好了，那时
        # install_dir 还是默认值，"写死"就会「提示的路径和玩家选的不符」。
        self.lb_done_install = self._label(self.install_dir, 11, color="#2f7cf6")
        lay.addWidget(self.lb_done_install)
        # 装完报一句版本：以后「是不是新装的」不用再猜（第四十九轮）
        self.lb_done_ver = self._label("", 10, color="#5a6675")
        lay.addWidget(self.lb_done_ver)
        # 快捷方式到底建没建、建在哪：失败也让玩家看见（第五十轮）
        self.lb_done_sc = self._label("", 10, color="#5a6675")
        lay.addWidget(self.lb_done_sc)
        lay.addSpacing(6)
        lay.addWidget(self._label("应用路径（本启动器专用）：", 12, color="#4c5866"))
        self.lb_done_app = self._label(self.app_dir, 11, color="#2f7cf6")
        lay.addWidget(self.lb_done_app)
        lay.addSpacing(4)
        lay.addWidget(self._label("官方启动器路径：", 12, color="#4c5866"))
        self.lb_done_off = self._label(self.official_dir, 11, color="#2f7cf6")
        lay.addWidget(self.lb_done_off)
        self.lb_done_note = self._label(self.game_note or "两条目录都已就绪",
                                        10, color="#5a6675")
        lay.addWidget(self.lb_done_note)
        lay.addSpacing(6)
        lay.addWidget(self._label("下载时选「官方 / 应用」决定装到哪一条，"
                                  "两条路径都能在应用「设置 → 游戏目录」里查看。",
                                  11, color="#4c5866"))
        lay.addStretch(1)
        self.btn_run = Pill("立即运行", "accent")
        self.btn_run.clicked.connect(self._run_app)
        lay.addWidget(self.btn_run, 0, Qt.AlignLeft)
        return c

    def _refresh_done(self):
        """按最终结果刷新「安装完成」页（路径 / 说明都以实际装到的地方为准）"""
        try:
            self.lb_done_install.setText(self.install_dir)
            try:
                self.lb_done_ver.setText(
                    f"已安装版本 v{self.src_version}"
                    + ("" if self.src_verified else "（未校验）"))
            except Exception:
                pass
            try:
                if getattr(self, "shortcut_note", None):
                    self.lb_done_sc.setText("⚠ " + "；".join(self.shortcut_note))
                    self.lb_done_sc.setStyleSheet("color:#c0392b;")
                else:
                    picks = []
                    if self.cb_desktop.isChecked():
                        picks.append("桌面")
                    if self.cb_startmenu.isChecked():
                        picks.append("开始菜单")
                    self.lb_done_sc.setText(
                        ("快捷方式已创建：" + "、".join(picks)) if picks else "")
                    self.lb_done_sc.setStyleSheet("color:#2f7cf6;")
            except Exception:
                pass
            self.lb_done_app.setText(self.app_dir)
            self.lb_done_off.setText(self.official_dir)
            self.lb_done_note.setText(self.game_note or "两条目录都已就绪")
        except Exception:
            pass

    # ---------- 逻辑 ----------
    def _own_game_dir(self):
        """应用路径：安装目录里的 `.minecraft`（本启动器专用）"""
        return os.path.join(self.install_dir, ".minecraft")

    def _default_off_dir(self):
        """官方路径默认值：`<安装目录>\\official_assets\\.minecraft`"""
        return os.path.join(self.install_dir, paths.OFFICIAL_DIR, paths.APP_DIR_NAME)

    def _detect_game_dir(self):
        """应用路径默认就放在安装目录里：`<安装目录>\\.minecraft`"""
        return self._own_game_dir()

    def _update_game_hint(self):
        off = (self.ed_off.text().strip() if hasattr(self, "ed_off")
               else self.official_dir) or self._default_off_dir()
        self.official_dir = off
        app = self.install_dir
        if gamedir.looks_like_mc(off):
            self.game_hint.setText("✓ 这个位置已经有官方启动器的数据，会直接沿用，不做改动")
            self.game_hint.setStyleSheet("color:#2f7cf6;font-size:10px;")
            return
        old_single = os.path.join(app, ".minecraft")
        if paths._has_content(old_single) and not paths._same(old_single, off):
            self.game_hint.setText(
                "✓ 检测到安装目录里原有的 .minecraft 有游戏数据，安装时会整体归到官方路径")
            self.game_hint.setStyleSheet("color:#2f7cf6;font-size:10px;")
            return
        src = paths.find_source_for(old_single, off)
        if src:
            self.game_hint.setText(f"✓ 将零拷贝共享本机现有游戏数据：{src}")
            self.game_hint.setStyleSheet("color:#2f7cf6;font-size:10px;")
            return
        self.game_hint.setText("· 本机暂无现成游戏数据，安装时只建空骨架，"
                               "第一次下载会自动填充")
        self.game_hint.setStyleSheet("color:#78848f;font-size:10px;")

    def _dir_changed(self, t):
        self.install_dir = t.strip()
        # 安装目录一变，两条子路径跟着走（玩家手动改过官方路径就别抢回来）
        old_off = getattr(self, "_off_snapshot", "")
        want_app = self._own_game_dir()
        want_off = self._default_off_dir()
        self.app_dir = want_app
        try:
            self.ed_app.setText(want_app)
            self.ed_app.setCursorPosition(0)
        except Exception:
            pass
        cur = self.ed_off.text().strip() if hasattr(self, "ed_off") else ""
        if not cur or os.path.normcase(cur) == os.path.normcase(old_off):
            if hasattr(self, "ed_off"):
                self.ed_off.setText(want_off)
                self.ed_off.setCursorPosition(0)
        self._own_snapshot = want_app
        self._off_snapshot = want_off
        self._update_game_hint()

    def _off_changed(self, t):
        self.official_dir = t.strip()
        self._update_game_hint()

    def _browse_dir(self):
        from .ui.fspicker import pick_dir
        d = pick_dir(self, "选择安装位置", self.ed_dir.text() or os.path.expanduser("~"))
        if d:
            # 自动生成 Osmanthus flower 专属文件夹（避免散落在所选盘根/目录下）
            if os.path.basename(d).lower() != INSTALL_DIR_NAME.lower():
                d = os.path.join(d, INSTALL_DIR_NAME)
                try:
                    os.makedirs(d, exist_ok=True)
                except Exception:
                    pass
            self.ed_dir.setText(d)
            self._update_game_hint()

    def _browse_off(self):
        from .ui.fspicker import pick_dir
        d = pick_dir(self, "选择官方启动器的游戏目录（.minecraft）",
                     self.ed_off.text() or os.path.expanduser("~"))
        if d:
            self.ed_off.setText(d)
            self._update_game_hint()

    def _go_back(self):
        i = self.stack.currentIndex()
        if i in (1, 2):
            self.stack.setCurrentIndex(i - 1)
        elif i == 3:
            self.stack.setCurrentIndex(1)       # 只有安装失败后才会走到这里
        self._update_nav()

    def _go_next(self):
        i = self.stack.currentIndex()
        if i == 0:
            self.stack.setCurrentIndex(1)
        elif i == 1:
            self.install_dir = self.ed_dir.text().strip() or self.install_dir
            self.app_dir = self._own_game_dir()
            self.official_dir = (self.ed_off.text().strip()
                                 or self._default_off_dir())
            self.stack.setCurrentIndex(2)
        elif i == 2:
            self.stack.setCurrentIndex(3)
            self.btn_next.setEnabled(False)
            self.btn_back.setEnabled(False)
            try:
                self.lb_err.setVisible(False)
                self.btn_fix.setVisible(False)
                self.lb_inst_title.setText("正在安装…")
                self.status.setStyleSheet("color:#5a6675;font-size:12px;")
            except Exception:
                pass
            threading.Thread(target=self._install, daemon=True).start()
        self._update_nav()

    def _update_nav(self):
        i = self.stack.currentIndex()
        self.btn_back.setVisible(i in (1, 2))
        self.btn_next.setVisible(i in (0, 1, 2))
        self.btn_next.setText("安装" if i == 2 else "下一步")

    def _progress(self, v, text):
        self.progress_sig.emit(v, text)

    def _on_progress(self, v, text):
        self.bar.setValue(v)
        self.status.setText(text)

    def _on_install_done(self):
        self._refresh_done()               # 路径以「真的装到哪」为准，不是构造时的默认值
        self.stack.setCurrentIndex(4)
        self._update_nav()
        self.btn_next.setEnabled(True)
        self.btn_back.setEnabled(True)
        try:
            self.lb_err.setVisible(False)
            self.btn_fix.setVisible(False)
            self.status.setStyleSheet("color:#5a6675;font-size:12px;")
        except Exception:
            pass

    def _shortcut(self, lnk, target):
        """创建快捷方式：COM（IShellLink）优先，失败回退 VBScript（此前 PowerShell 方案在提权环境常失效）"""
        from app.core import winlnk
        try:
            if winlnk.create_shortcut(lnk, target, workdir=os.path.dirname(target),
                                      icon=target, desc=APP_NAME):
                return True
        except Exception:
            pass
        try:
            return winlnk.create_shortcut_fallback(lnk, target)
        except Exception:
            return False

    def _clean_legacy_shortcuts(self, *dirs):
        """删掉旧名快捷方式（只动 .lnk，绝不碰目录里的别的东西）

        ★ 第四十九轮：`LEGACY_INSTALL_NAMES` 里带着 `八桂启动器`（它是**数据目录**的
          旧名），照抄着当「旧快捷方式」去 glob 会命中**刚建好的** `八桂启动器.lnk`
          ——装完桌面上一个图标都没有。当前名字永远不算旧名字。
        """
        import glob
        legacy = [n for n in LEGACY_INSTALL_NAMES
                  if os.path.normcase(n) != os.path.normcase(APP_NAME)]
        for d in dirs:
            if not d or not os.path.isdir(d):
                continue
            for old in legacy:
                for lnk in glob.glob(os.path.join(d, f"{old}*.lnk")):
                    try:
                        os.remove(lnk)
                    except OSError:
                        pass
                sub = os.path.join(d, old)
                if os.path.isdir(sub):
                    try:
                        lnks = [x for x in os.listdir(sub) if x.lower().endswith(".lnk")]
                        if len(lnks) == len(os.listdir(sub)):   # 只有快捷方式才敢删目录
                            shutil.rmtree(sub, ignore_errors=True)
                    except OSError:
                        pass

    def _prepare_dirs(self):
        """把「应用路径」「官方路径」一次整理好（见 core/paths.prepare_pair）。"""
        return paths.prepare_pair(self.app_dir, self.official_dir)

    # ---------- 失败提示 ----------
    def _err_lines(self, e, where=""):
        """把异常翻成人话：卡在哪、为什么、下一步怎么做。"""
        where = where or getattr(e, "filename", "") or self.install_dir
        eno = getattr(e, "errno", None)
        if isinstance(e, PayloadError):
            # 安装包自身的问题：换目录没用，只能重下（别给「改用默认目录」的错觉）
            return where, e.why, e.how
        if isinstance(e, PermissionError) or eno in (13, 5):
            why = ("没有写入权限。这个位置被 Windows 保护（常见于 C:\\Program Files、"
                   "C:\\Windows、别的账号的目录），或者被杀毒软件锁住了")
            how = ("换一个自己的目录就行，不需要管理员权限 —— 直接点下面的"
                   "「改用默认目录」，会换到 " + self._safe_default() + "。"
                   "如果一定要装到受保护目录，请右键安装包选「以管理员身份运行」。")
        elif eno == 28:
            why = "磁盘空间不足"
            how = "清理一下这个盘，或者点「改用默认目录」换到系统盘 / 别的盘。"
        elif eno in (2, 3):
            why = "路径不存在或不是文件夹"
            how = "检查一下路径有没有打错（比如多了引号、空格），或者点「改用默认目录」。"
        elif isinstance(e, FileExistsError) or eno == 183:
            why = "目标位置已经有一个同名文件/文件夹，并且正被占用"
            how = "先关掉正在运行的启动器（包括右下角托盘里的），再点「上一步」重试。"
        elif isinstance(e, OSError) and getattr(e, "winerror", None) == 32:
            why = "文件正被别的程序占用"
            how = "关掉正在运行的启动器 / 资源管理器里打开着这个文件夹，再重试。"
        else:
            why = f"{type(e).__name__}: {e}"
            how = "可以点「上一步」换个目录再试；如果一直失败，把这段文字发给作者。"
        return where, why, how

    def _safe_default(self):
        try:
            return default_install_dir()
        except Exception:
            return r"%LOCALAPPDATA%\Programs\\" + INSTALL_DIR_NAME

    def _fail(self, e, where="", title="安装失败"):
        where, why, how = self._err_lines(e, where)
        self.lb_inst_title.setText(title)
        self.bar.setValue(0)
        self.status.setText(f"卡在：{where}")
        self.status.setStyleSheet("color:#c0392b;font-size:12px;")
        self.lb_err.setText(f"原因：{why}\n\n下一步：{how}")
        self.lb_err.setVisible(True)
        self.btn_fix.setVisible(not isinstance(e, PayloadError))
        self.btn_back.setVisible(True)          # 上一步：回去改路径
        self.btn_back.setEnabled(True)
        self.btn_next.setVisible(False)         # 装到一半失败时「下一步」没有意义

    def _use_default_dir(self):
        """失败后一键换成默认安装目录，回到路径页让玩家确认。"""
        d = self._safe_default()
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        self.install_dir = d
        self._own_snapshot = os.path.join(d, ".minecraft")
        self._off_snapshot = os.path.join(d, paths.OFFICIAL_DIR, paths.APP_DIR_NAME)
        self.app_dir = self._own_snapshot
        self.official_dir = self._off_snapshot
        try:
            self.ed_dir.setText(d)
            self.ed_dir.setCursorPosition(0)
            self.ed_off.setText(self.official_dir)
            self.ed_off.setCursorPosition(0)
        except Exception:
            pass
        self.lb_err.setVisible(False)
        self.btn_fix.setVisible(False)
        self.status.setStyleSheet("color:#5a6675;font-size:12px;")
        self.lb_inst_title.setText("正在安装…")
        self.stack.setCurrentIndex(1)
        self._update_nav()
        self._update_game_hint()

    def _install(self):
        exe_dst = os.path.join(self.install_dir, f"{APP_NAME}.exe")
        where = self.install_dir
        try:
            self._progress(8, "创建安装目录…")
            where = self.install_dir
            os.makedirs(self.install_dir, exist_ok=True)
            # 提前探一次写入权限：别等 exe 复制完、快捷方式建好才炸出一串 WinError 5
            self._progress(16, "检查写入权限…")
            ok, msg = paths.write_probe(self.install_dir)
            if not ok:
                raise PermissionError(13, msg or "没有写入权限", self.install_dir)
            self._progress(32, "复制主程序…")
            where = exe_dst
            self._copy_payload(exe_dst)
            # 第一百一十五轮：附赠「使用指南」到安装根目录（欢迎页链接要打开它）。
            # 它是文档不是程序：装不上只记一句说明，绝不阻断安装。
            self._install_guides()
            self._progress(50, "整理游戏目录…")
            where = self.official_dir or self._default_off_dir()
            info = self._prepare_dirs()
            self.app_dir = info.get("app") or self.app_dir
            self.official_dir = info.get("official") or self.official_dir
            self.game_note = info.get("note") or ""
            if info.get("error"):
                self.game_note = (self.game_note + "；" if self.game_note else "") + \
                    f"目录准备有异常：{info['error']}（可在应用设置里重新指定）"
            self._progress(66, "写入应用配置…")
            where = self._write_config()
            # 随包附属文件 / 指南没装上的，完成页如实说出来（绝不静默）
            _notes = getattr(self, "payload_note", None) or []
            if _notes:
                self.game_note = ((self.game_note + "；") if self.game_note else "") + \
                    "；".join(_notes)
            self._progress(80, "创建快捷方式…")
            where = "桌面 / 开始菜单的快捷方式"
            self.shortcut_note = list(self._make_shortcuts(exe_dst) or [])
            self._progress(100, "安装完成")
            self.install_done_sig.emit()
        except Exception as e:                    # noqa: BLE001
            self.fail_sig.emit(e, where)

    def _copy_payload(self, exe_dst):
        """把随包的程序写进安装目录，并回读校验。

        ★ 第四十九轮：装上去的必须是**向导自带的**那个 exe。
        ★ 第五十一轮：清单里有几个 exe 就装几个（主程序 + 卸载程序）。
          ① 找不到随包主程序 → 明确报错，不复制任何旧文件；
          ② 先写 `.new` 再原子替换，旧版正开着也能装（改名让位）；
          ③ 落地后按 SHA256 回读比对。
        """
        src = getattr(self, "src_exe", "")
        if self._src_error is not None:
            raise self._src_error
        if not src or not os.path.isfile(src):
            raise PayloadError(
                f"安装包不完整：没有找到随包的 {APP_NAME}.exe",
                "重新下载完整的安装向导再装一次。主程序就在安装包里，"
                "不要只把向导 exe 单独拷过来，也不要用旧的主程序顶替。")
        files = getattr(self, "src_files", None) or [
            {"src": src, "name": os.path.basename(src), "sha256": "", "ok": True}]
        # 主程序排第一（清单就是这么生成的）；万一顺序被改，这里也认得出
        files = sorted(files, key=lambda f: 0 if os.path.abspath(f.get("src") or "")
                       == os.path.abspath(src) else 1)
        base = os.path.dirname(exe_dst)
        first = True
        notes = getattr(self, "payload_note", None)
        if notes is None:
            notes = self.payload_note = []
        for f in files:
            dst = os.path.join(base, f["name"])
            want = f.get("sha256") if self.src_verified else ""
            if self.src_verified and not want:      # 老清单/兜底路径：现算
                try:
                    want = _sha256(f["src"])
                except OSError:
                    want = ""
            if not f.get("ok", True):
                # 附属文件（卸载程序）坏了：主程序照装，但要说出来
                if not first:
                    notes.append(f"{f['name']} 校验不通过，没有安装")
                else:
                    raise PayloadError(
                        f"安装包不完整：{f['name']} 校验不通过",
                        "重新下载一次安装向导再运行。")
                first = False
                continue
            try:
                _copy_one(f["src"], dst, want)
            except Exception:
                if first:
                    raise
                notes.append(f"{f['name']} 没有装上")
            first = False

    def _copy_one(self, src, dst, want_sha=""):
        """（保留为方法，方便外部按对象调用；真正实现见模块级 `_copy_one`）"""
        return _copy_one(src, dst, want_sha)

    def _install_guides(self):
        """把随包指南（简体/繁体 × docx/pdf，共 4 份）复制到 `User Guide` 文件夹。

        第一百一十六轮：指南不再散在安装根目录，统一进 `Osmanthus flower\\
        User Guide\\`（User Guide = 「使用指南」的英文，按玩家要求用英文名）。
        旧版放在根目录的同名指南不删 —— 玩家可能改过，交给其自行处理。

        文档不是程序，失败只记 `payload_note`，不影响安装成功 —— 但绝不静默：
        完成页会把没装上说出来。用 `_copy_one` 的原子替换。
        """
        notes = getattr(self, "payload_note", None)
        if notes is None:
            notes = self.payload_note = []
        srcs = guide_sources()
        if not srcs:
            notes.append("安装包未附带使用指南")
            return
        dest_dir = os.path.join(self.install_dir, "User Guide")
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except Exception as e:
            notes.append(f"使用指南文件夹创建失败: {e}")
            return
        for src, name in srcs:
            dst = os.path.join(dest_dir, name)
            try:
                _copy_one(src, dst)
            except Exception:
                notes.append(f"使用指南 {name} 没有装上")

    def _write_config(self):
        cfg_dir = app_data_dir()
        os.makedirs(cfg_dir, exist_ok=True)
        cfg_path = os.path.join(cfg_dir, "config.json")
        data = {}
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        # 第六十七轮补丁：**先记住上一份应用路径**再覆盖。换安装目录是玩家自由，
        # 但不能因此把他以前的游戏「弄丢」—— 新目录是空的、旧目录里塞满版本时，
        # 继续用旧的那份（只写配置，一个字节都不搬）。
        old_game = str(data.get("game_dir") or "").strip()
        old_ov = str(data.get(paths.APP_OVERRIDE_KEY) or "").strip()
        new_app = self.app_dir or self._own_game_dir()
        keep = ""
        if not paths.has_games(new_app):
            for cand in (old_ov, old_game):
                if not cand:
                    continue
                if os.path.normcase(os.path.abspath(cand)) == os.path.normcase(new_app):
                    continue
                if paths.has_games(cand):
                    keep = os.path.abspath(cand)
                    break
        data["install_dir"] = self.install_dir
        if keep:
            data[paths.APP_OVERRIDE_KEY] = keep
            data["game_dir"] = keep
            self.game_note = ((self.game_note + "；") if self.game_note else "") + \
                f"沿用你之前的游戏目录：{keep}（新安装目录里还没游戏，旧数据没动）"
        else:
            data[paths.APP_OVERRIDE_KEY] = ""
            data["game_dir"] = new_app                     # 应用路径（独立启动读它）
            if old_game and os.path.normcase(os.path.abspath(old_game)) != \
                    os.path.normcase(new_app):
                # 留个面包屑：万一以后又指错了，启动时能凭它认回来
                data[paths.PREV_APP_KEY] = os.path.abspath(old_game)
        data["official_game_dir"] = self.official_dir
        # 第四十六轮：目标不写死。空串 = 跟随「独立启动」开关
        # （关着=官方，开着=应用），装完第一次下载就是玩家真正会用的那条。
        data[paths.TARGET_KEY] = ""
        data[paths.TARGET_AUTO_KEY] = True
        data[paths.SPLIT_KEY] = True             # 向导已经把两条路径分好了，别再迁移一次
        try:
            kind, val = find_launcher_exe()
            if kind == "exe":
                data["launcher_path"] = val
        except Exception:
            pass
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return cfg_path

    # ---- 快捷方式落在哪：一律问 Shell，别自己拼 %USERPROFILE%\Desktop ----
    def _desktop_dir(self):
        from app.core import winlnk
        return winlnk.desktop_dir()

    def _startmenu_dir(self):
        from app.core import winlnk
        return winlnk.startmenu_dir()

    def _shortcut_into(self, folder, target, label, notes, name=None):
        """在 folder 里建快捷方式；失败把原因写进 notes（不再静默）。

        `name` 默认 `八桂启动器.lnk`；传了就按传的来（开始菜单里还要放一个
        「卸载 八桂启动器.lnk」，第五十一轮）。
        """
        if not folder:
            notes.append(f"{label}快捷方式：找不到{label}目录")
            return False
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            notes.append(f"{label}快捷方式：无法创建目录 {folder}（{e.strerror or e}）")
            return False
        lnk = os.path.join(folder, name or f"{APP_NAME}.lnk")
        if not self._shortcut(lnk, target):
            notes.append(f"{label}快捷方式：写入失败（{lnk}）")
            return False
        if not os.path.isfile(lnk):
            notes.append(f"{label}快捷方式：没有落盘（{lnk}）")
            return False
        return True

    def _make_shortcuts(self, exe_dst):
        """桌面 / 开始菜单快捷方式。

        ★ 第五十轮：玩家把桌面挪到别的盘（或 OneDrive 接管）时，
          `%USERPROFILE%\\Desktop` 只是个空壳目录 —— 建进去等于石沉大海。
          现在一律问 Shell 要「真·桌面 / 真·开始菜单」，失败还要**说出来**。
        ★ 第五十一轮：开始菜单里再放一个「卸载 八桂启动器」，否则玩家得自己
          进安装目录翻那个 exe。
        """
        notes = []
        desktop = self._desktop_dir()
        sm = self._startmenu_dir()
        if self.cb_desktop.isChecked():
            self._shortcut_into(desktop, exe_dst, "桌面", notes)
        if self.cb_startmenu.isChecked():
            self._shortcut_into(sm, exe_dst, "开始菜单", notes)
            uni = os.path.join(os.path.dirname(exe_dst), f"{UNINSTALL_EXE_NAME}.exe")
            if os.path.isfile(uni):
                self._shortcut_into(sm, uni, "开始菜单", notes,
                                    name=f"卸载{APP_NAME}.lnk")
        # 改名后升级：老名字的快捷方式留着会变成「两个图标，点错进旧版」
        self._clean_legacy_shortcuts(desktop, sm)
        return notes

    def _run_app(self):
        exe_dst = os.path.join(self.install_dir, f"{APP_NAME}.exe")
        if os.path.isfile(exe_dst):
            subprocess.Popen([exe_dst], creationflags=subprocess.DETACHED_PROCESS)
        self.close()
        QApplication.quit()

    # ---------- 交互 ----------
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


def _app_icon():
    """应用图标（樱花树叶方块，与主程序共用内嵌资源）"""
    from app.ui.branding import app_icon
    return app_icon(128)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(f"{APP_NAME} 安装向导")
    app.setFont(QFont(theme.FONT, 10))
    app.setStyleSheet(theme.qss())
    # 第 118 轮：安装向导也要能简繁切换（默认跟随系统）。必须在建任何控件
    # 之前装拦截；向导与主程序共用同一份 config.json，语言设置自然延续。
    try:
        from app.ui import i18n_ui as _i18n_ui
        _i18n_ui.install(app)
    except Exception:
        pass
    try:
        theme.apply_light_tooltip(app)
        from app.ui import tipbox
        tipbox.install(app)
    except Exception:
        pass
    w = Wizard()
    w.show()
    winacrylic.set_round_corners(w, True)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
