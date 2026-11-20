# -*- coding: utf-8 -*-
"""对话框：登录 / 设置 / 关闭确认 / 安装进度 / 项目详情"""
import os

from PySide6.QtCore import (QPoint, QRectF, QSize, Qt, QTimer, QUrl,
                            Signal)
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QGuiApplication,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QDialog, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QRadioButton, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

from ..core import cnnames as cn_names
from ..core import deps, installer, modrinth
from ..core.asyncio_qt import run_async
from ..core.config import APP_VERSION, CONFIG
from ..core.modrinth import cn_category
from . import theme
from .widgets import (AsyncImageView, ChoiceExpander, FrostCard, IconButton,
                      MemoryField, PillButton, PillSwitch, Toast)


class _DialogBase(QDialog):
    """磨砂玻璃对话框基类"""

    def __init__(self, parent, width=420, title=""):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setFixedWidth(width)
        self.root = FrostCard(radius=18, tint=242, parent=self)
        self.root.setGeometry(0, 0, 0, 0)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(14)
        self.head = None
        if title:
            # 标题行改成一条水平布局（第五十五轮）：右侧要能挂图标按钮，
            # 且必须与标题文字「同一行、垂直居中」才算「与设置字样平齐」。
            self.head = QHBoxLayout()
            self.head.setContentsMargins(0, 0, 0, 0)
            self.head.setSpacing(6)
            t = QLabel(title)
            f = QFont(theme.FONT)
            f.setPixelSize(17)
            f.setBold(True)
            t.setFont(f)
            self.head.addWidget(t, 0, Qt.AlignVCenter)
            self.head.addStretch(1)
            lay.addLayout(self.head)
        self.body = lay
        self._drag = None

    def showEvent(self, e):
        super().showEvent(e)
        self.root.setGeometry(self.rect())
        # 首次出现也夹一次：父窗口移动过 / 多屏时别让弹窗落在窗口外
        self.move(self.clamp_pos(self.pos()))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.root.setGeometry(self.rect())

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() < 64:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def parent_area(self):
        """可拖动范围：父窗口的矩形（没有父窗口时退回屏幕可用区）"""
        from PySide6.QtCore import QPoint, QRect
        from PySide6.QtGui import QGuiApplication
        p = self.parentWidget()
        while p is not None:
            if p.isWindow() and p.isVisible():
                return QRect(p.mapToGlobal(QPoint(0, 0)), p.size())
            p = p.parentWidget()
        scr = self.screen() or QGuiApplication.primaryScreen()
        return scr.availableGeometry()

    def clamp_pos(self, pos):
        """把弹窗左上角夹在父窗口内 —— 拖不出窗口（弹窗比父窗口大时居中）"""
        from PySide6.QtCore import QPoint
        area = self.parent_area()
        if area.width() < self.width() or area.height() < self.height():
            return area.center() - QPoint(self.width() // 2, self.height() // 2)
        x = max(area.left(), min(pos.x(), area.right() - self.width() + 1))
        y = max(area.top(), min(pos.y(), area.bottom() - self.height() + 1))
        return QPoint(x, y)

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(self.clamp_pos(e.globalPosition().toPoint() - self._drag))

    def mouseReleaseEvent(self, e):
        self._drag = None

    def add_buttons(self, specs):
        """specs: [(text, kind, callback)]"""
        row = QHBoxLayout()
        row.addStretch(1)
        row.setSpacing(10)
        for text, kind, cb in specs:
            b = PillButton(text, kind)
            b.setFixedHeight(36)
            b.setMinimumWidth(92)
            if cb:
                b.clicked.connect(cb)
            row.addWidget(b)
        self.body.addLayout(row)
        return row


# ================================================================ 启动项（设置页）
class _ProfileRow(QWidget):
    """官启档案 = 一个启动项。点击展开：运行版本 / 目录 / 内存 + 版本隔离开关。"""

    def __init__(self, prof, page):
        super().__init__()
        self.prof = prof
        self.page = page
        self.setStyleSheet("background:transparent;")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.head = QWidget()
        self.head.setCursor(Qt.PointingHandCursor)
        self.head.setFixedHeight(42)
        self.head.setAttribute(Qt.WA_StyledBackground, True)
        self.head.setStyleSheet(
            "QWidget{background:rgba(255,255,255,170);border-radius:10px;}")
        h = QHBoxLayout(self.head)
        h.setContentsMargins(12, 6, 10, 6)
        h.setSpacing(6)

        nm = QLabel(prof.get("label") or prof["name"])
        f = QFont(theme.FONT)
        f.setPixelSize(12)
        f.setBold(True)
        nm.setFont(f)
        nm.setStyleSheet("background:transparent;")
        nm.setToolTip(prof["name"])
        h.addWidget(nm, 1)

        def chip(text, bg, fg):
            c = QLabel(text)
            c.setStyleSheet(f"QLabel{{background:{bg};color:{fg};font-size:10px;"
                            f"border-radius:7px;padding:2px 6px;}}")
            return c

        h.addWidget(chip(prof["type_cn"], "rgba(47,124,246,30)", "#2f7cf6"))
        if prof.get("instance"):
            h.addWidget(chip("实例", "rgba(46,158,91,30)", "#2e9e5b"))
        elif prof.get("isolated"):
            h.addWidget(chip("已隔离", "rgba(46,158,91,30)", "#2e9e5b"))
        if prof.get("default"):
            h.addWidget(chip("默认", "rgba(31,39,51,24)", "#4c5866"))
        self.arrow = QLabel("▸")
        self.arrow.setStyleSheet("color:#8a95a3;font-size:12px;background:transparent;")
        h.addWidget(self.arrow)
        lay.addWidget(self.head)

        # ---- 展开区（默认收起）----
        self.detail = QWidget()
        self.detail.setAttribute(Qt.WA_StyledBackground, True)
        self.detail.setStyleSheet(
            "QWidget{background:rgba(255,255,255,120);border-radius:10px;}")
        dl = QVBoxLayout(self.detail)
        dl.setContentsMargins(12, 8, 12, 10)
        dl.setSpacing(4)

        def line(text, dim=False):
            lb = QLabel(text)
            lb.setWordWrap(True)
            lb.setStyleSheet(
                "color:%s;font-size:11px;background:transparent;" %
                ("#5a6675" if dim else "#33404f"))
            dl.addWidget(lb)
            return lb

        vid = prof["version_id"] or ""
        if not vid:
            ver_txt = "未绑定（先在官启里启动一次）"
        elif vid.startswith("latest-"):
            ver_txt = f"{vid}（由官方启动器自动选择版本）"
        elif prof.get("exists"):
            ver_txt = vid
        else:
            ver_txt = f"{vid}（该版本目录不存在）"
        line(f"运行版本：{ver_txt}", dim=not vid or vid.startswith("latest-")
             or not prof.get("exists"))
        line(f"游戏目录：{prof['game_dir'] or '游戏根目录（默认）'}", dim=True)
        line(f"内存参数：{prof['java_args'] or '启动器默认'}", dim=True)

        self.cb_iso = QCheckBox("版本隔离（存档 / 模组只用这一份，互不影响）")
        self.cb_iso.setStyleSheet("color:#4c5866;font-size:11px;background:transparent;")
        if prof.get("instance"):
            self.cb_iso.setChecked(True)
            self.cb_iso.setEnabled(False)
            self.cb_iso.setText("已是独立实例目录（整合包自带隔离）")
        else:
            self.cb_iso.setChecked(bool(prof.get("isolated")))
            can = bool(prof.get("version_id")) and prof.get("exists")
            self.cb_iso.setEnabled(can)
            if not can:
                self.cb_iso.setToolTip("该启动项还没有具体的运行版本，先在官方启动器里启动一次")
            self.cb_iso.toggled.connect(self._toggle_iso)
        dl.addWidget(self.cb_iso)

        row = QHBoxLayout()
        row.setSpacing(8)
        b_dir = PillButton("打开目录", "ghost")
        b_dir.setFixedHeight(28)
        b_dir.clicked.connect(self._open_dir)
        row.addWidget(b_dir)
        b_def = PillButton("设为启动项", "ghost")
        b_def.setFixedHeight(28)
        b_def.setEnabled(not prof.get("default"))
        b_def.clicked.connect(self._make_default)
        row.addWidget(b_def)
        row.addStretch(1)
        dl.addLayout(row)

        self.detail.setVisible(False)
        lay.addWidget(self.detail)
        lay.addSpacing(4)
        self.head.mousePressEvent = self._toggle

    def _toggle(self, e=None):
        vis = not self.detail.isVisible()
        self.detail.setVisible(vis)
        self.arrow.setText("▾" if vis else "▸")

    def _open_dir(self):
        gd = installer.detect_game_dir()
        path = self.prof.get("game_dir") or gd
        try:
            # 第六十轮：设置里「文件管理器」开着就走内置窗口，关着才叫系统资源管理器
            from .fspicker import pick_view
            pick_view(self, "游戏目录", path)
        except Exception:
            Toast.show_toast(self.page.dlg, "无法打开该目录")

    def _toggle_iso(self, on):
        ok, msg = installer.set_profile_isolation(self.prof["id"], bool(on))
        Toast.show_toast(self.page.dlg, msg, ms=4200, kind="success" if ok else "error")
        if not ok:
            self.cb_iso.blockSignals(True)
            self.cb_iso.setChecked(not on)
            self.cb_iso.blockSignals(False)
        self.page.reload()

    def _make_default(self):
        ok, msg = installer.set_default_profile(self.prof["id"])
        Toast.show_toast(self.page.dlg, msg, ms=3600, kind="success" if ok else "error")
        if ok:
            self.page.reload()


class _StartupPage(QWidget):
    """设置 → 启动项：**全局启动设置**（始终显示）+ 官启档案列表。

    第四十四轮：上面三块（版本隔离 / 优先进程 / Java）是全局设置，**不需要装任何
    整合包也一直显示**；下面才是官方启动器里的启动项（档案），没有就只留一句提示。
    这两部分以前混在一起，玩家没装包时整页空空，会以为功能没做。
    """

    _reload_pending = False          # 档案列表是否还排在下一帧里没铺

    def __init__(self, dlg):
        super().__init__()
        self.dlg = dlg
        from ..core import launchprefs as lp
        self._lp = lp

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        title = QLabel("启动项")
        tf = QFont(theme.FONT)
        tf.setPixelSize(15)
        tf.setBold(True)
        title.setFont(tf)
        outer.addWidget(title)

        tip = QLabel("全局启动设置对「独立启动」的所有游戏生效；单个游戏可以在"
                     "「游戏管理 → 设置」里单独覆盖。下面的列表是官方启动器里的启动项（档案）。")
        tip.setStyleSheet("color:#5a6675;font-size:11px;")
        tip.setWordWrap(True)
        outer.addWidget(tip)

        # ---------------- 全局启动设置（始终显示） ----------------
        self.ex_iso = ChoiceExpander(
            "版本隔离", [(k, n, d) for k, n, d in lp.ISO_MODES], lp.iso_mode())
        self.ex_iso.setToolTip("决定每个版本用不用独立的游戏目录（存档 / 模组是否共享）")
        outer.addWidget(self.ex_iso)

        self.ex_prio = ChoiceExpander(
            "优先进程", [(k, n, d) for k, n, d in lp.PRIORITIES], lp.priority())
        self.ex_prio.setToolTip("启动游戏时给 java 进程的 CPU 优先级")
        outer.addWidget(self.ex_prio)

        mode, path = lp.java_choice()
        # 第九十八轮：这里**先不扫盘**（扫描结果由 _load_javas_async 回填）
        self._javas = []
        self.ex_java = ChoiceExpander("Java 运行环境", self._java_options(path), path or "auto")
        self.ex_java.changed.connect(self._on_java_changed)
        outer.addWidget(self.ex_java)

        self.lb_java = QLabel("")
        self.lb_java.setStyleSheet("color:#8a95a3;font-size:10.5px;")
        self.lb_java.setWordWrap(True)
        outer.addWidget(self.lb_java)
        self._sync_java_hint()
        self._load_javas_async()

        # ---------------- 第一百零四轮：高级启动参数（默认全关，不改老行为） ----
        # 第一百零五轮：整块改成收放式（玩家要求「弄成收放式选项」）。
        # 默认收起：这些是高级项，摊开会把下面的官启档案挤出可视区。
        # 只换外层容器，里面每个控件的属性名一个都没动 —— 保存逻辑
        # （_build_rest / _save 里读 self.startup.ed_*）照旧能拿到。
        self.grp_adv = _SourceGroup("高级启动参数（全局）", "默认收起",
                                    expanded=False)
        adv = QWidget()
        av = QVBoxLayout(adv)
        av.setContentsMargins(0, 4, 0, 0)
        av.setSpacing(5)
        adv_title = QLabel("高级启动参数（全局）")
        adv_title.setStyleSheet("color:#2a3442;font-size:12px;font-weight:600;")
        av.addWidget(adv_title)

        self.cb_jvm_opt = QCheckBox("启用默认优化 JVM 参数（G1GC / 防 log4j2 漏洞等，可关）")
        self.cb_jvm_opt.setChecked(bool(self._lp.default_jvm_opt_enabled()))
        self.cb_jvm_opt.setStyleSheet("color:#4c5866;font-size:11px;")
        av.addWidget(self.cb_jvm_opt)

        def _line(placeholder, text=""):
            e = QLineEdit()
            e.setPlaceholderText(placeholder)
            e.setText(str(text or ""))
            e.setFixedHeight(28)
            e.setStyleSheet(
                "QLineEdit{background:rgba(255,255,255,190);"
                "border:1px solid rgba(31,39,51,45);border-radius:7px;"
                "padding:0 8px;font-size:11px;color:#2a3442;}")
            av.addWidget(e)
            return e

        self.ed_jvm = _line("全局自定义 JVM 参数（实例级填了会完全覆盖这一行）",
                            CONFIG.get("jvm_args_global", ""))
        self.ed_game = _line("全局自定义游戏参数（--gameDir 之类交给启动器，别重复填）",
                             CONFIG.get("game_args_global", ""))
        self.ed_pre = _line("预启动命令（可留空）",
                            CONFIG.get("pre_launch_global", ""))
        self.cb_pre_wait = QCheckBox("等预启动命令跑完再启动游戏（最多 120 秒）")
        self.cb_pre_wait.setChecked(bool(CONFIG.get("pre_launch_wait", True)))
        self.cb_pre_wait.setStyleSheet("color:#4c5866;font-size:11px;")
        av.addWidget(self.cb_pre_wait)
        self.ed_window = _line("窗口：宽x高|模式|标题，如 1280x720|windowed|我的世界",
                               "%sx%s|%s|%s" % (
                                   CONFIG.get("window_width", 854),
                                   CONFIG.get("window_height", 480),
                                   CONFIG.get("window_mode", "windowed"),
                                   CONFIG.get("window_title", "")))
        self.ed_server = _line("启动后自动进入服务器：地址[:端口]（可留空）",
                               CONFIG.get("server_to_enter", ""))
        self.cb_javaw = QCheckBox("用 javaw.exe 启动（不弹控制台窗口）")
        self.cb_javaw.setChecked(bool(CONFIG.get("javaw", False)))
        self.cb_javaw.setStyleSheet("color:#4c5866;font-size:11px;")
        av.addWidget(self.cb_javaw)
        self.ed_ipstack = _line("网络优先：auto / ipv4 / ipv6",
                                CONFIG.get("ip_stack", "auto"))
        self.grp_adv.add(adv)
        outer.addWidget(self.grp_adv)
        self.grp_adv.head.clicked.connect(
            lambda _c: QTimer.singleShot(0, self._refit_startup))

        # ---------------- 官启档案（第 105 轮：整块收放式） ----------------
        self.grp_profiles = _SourceGroup("官方启动器里的启动项", "官启档案",
                                          expanded=True)
        barw = QWidget()
        barw.setStyleSheet("background:transparent;")
        bar = QHBoxLayout(barw)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        btn = PillButton("刷新", "ghost")
        btn.setFixedHeight(26)
        # 第一百轮：以前直连 reload()，快速连点就是反复整页重建（实测 276ms/次）。
        # 现在走下一帧去抖 + 内容签名比对，连点只重扫一次、内容没变一行都不重建。
        btn.clicked.connect(self._request_reload)
        bar.addStretch(1)
        bar.addWidget(btn)
        self.grp_profiles.add(barw)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self.host = QWidget()
        self.host.setStyleSheet("background:transparent;")
        self.body = QVBoxLayout(self.host)
        self.body.setContentsMargins(0, 0, 4, 0)
        self.body.setSpacing(2)
        self.scroll.setWidget(self.host)
        self.grp_profiles.add(self.scroll, 1)
        outer.addWidget(self.grp_profiles, 1)
        self.grp_profiles.head.clicked.connect(
            lambda _c: QTimer.singleShot(0, self._refit_startup))
        # 第九十八轮：**不要在建页时同步铺档案列表**。9 条档案铺进
        # `widgetResizable` 的滚动区实测 246ms（每 addWidget 一次都要重排前面
        # 所有行，是 O(n²)），玩家点左侧「启动项」卡的就是这一下。
        # 先让页面本身出现（44ms），列表推到下一帧再填。
        self._reload_pending = True
        QTimer.singleShot(0, self._reload_soon)

    def _refit_startup(self):
        """第 105 轮：收放式分组展开 / 收起后让外面重算尺寸。

        第 93 轮踩过的坑：这一页在 `_ScrollPage`（显式最小高、无横滚）里，
        卡片自己长高没人叫它，页面还按旧尺寸摆，末行会被裁掉。沿父链找到
        滚动页 fit() 一次，再让设置窗按新高度调整。用 QTimer.singleShot(0)
        延后一拍，等 `_SourceGroup._toggle()` 先把 visible 状态落定。
        """
        w = self.parentWidget()
        sa = None
        for _ in range(10):
            if w is None:
                break
            if type(w).__name__ == "_ScrollPage":
                sa = w
                break
            w = w.parentWidget()
        if sa is not None:
            try:
                sa.fit()
            except Exception:                       # noqa: BLE001
                pass
        top = self.window()
        box = getattr(top, "_fit_box", None)
        if callable(box):
            try:
                box()
            except Exception:                       # noqa: BLE001
                pass

    def _reload_soon(self):
        if not self._reload_pending:
            return
        self._reload_pending = False
        try:
            self.reload()
        except RuntimeError:
            pass
        except Exception:                                 # noqa: BLE001
            import traceback
            traceback.print_exc()

    def flush(self):
        """把排队的「铺档案列表」立刻做完（保存取值之前必须调）"""
        if self._reload_pending:
            self._reload_soon()

    # ---------- Java ----------
    def _java_options(self, current_exe="", lst=None):
        """Java 下拉的选项。

        第九十八轮：**不要在这里顺手扫磁盘**。原来这一行直接
        `javamanager.scan_all()`，冷启动实测 345ms，而「启动项」页一建就要它
        —— 玩家点左侧「启动项」看到的就是那一下卡顿。现在只拿已经扫到的那份
        （`self._javas`，还没扫就是空），真正的扫描由 `_load_javas_async`
        在工作线程里做完再回填（它本来就有）。
        """
        from ..core import javamanager
        opts = [("auto", "自动选择（按游戏需要挑最合适的 Java）",
                 "推荐：1.20.5 及以上用 21，1.17~1.20.4 用 17，1.16 及更早用 8")]
        pool = lst if lst is not None else (getattr(self, "_javas", None) or [])
        for j in pool:
            desc = f"{j['home']} · 来源：{j.get('source') or '本机'}"
            opts.append((j["exe"], f"Java {j['major']}（{j['ver']}）", desc))
        if current_exe and all(str(k) != current_exe for k, _n, _d in opts):
            # 手动添加过、但扫描没找到（比如在移动硬盘上）→ 仍然要能选中
            opts.append((current_exe, "手动添加的 java.exe", current_exe))
        opts.append(("__add__", "添加 Java.exe…", "手动指定一个 java.exe（绿色版 JDK 解压在别处时用）"))
        return opts

    def _load_javas_async(self):
        from ..core import javamanager
        from ..core.asyncio_qt import run_async

        def done(lst):
            try:
                self._javas = list(lst or [])
                if not self.ex_java.isVisible() and not self.dlg.isVisible():
                    return
                cur = self.ex_java.value()
                self.ex_java.set_options(self._java_options(
                    "" if cur in ("auto", "__add__") else cur), cur)
                self.lb_java.setText(
                    f"本机找到 {len(lst)} 个 Java" if lst else
                    "本机没找到 Java：可以点上面「添加 Java.exe…」，或到「设置 → 运行内存」旁边的 Java 面板下载一个")
            except Exception:
                pass

        run_async(javamanager.scan_all, on_done=done, on_err=lambda e: None)

    def _on_java_changed(self, key):
        if key != "__add__":
            self._sync_java_hint()
            return
        from .fspicker import pick_file
        p = pick_file(self, "选择 java.exe", "")
        if p:
            import os as _os
            self.ex_java.set_options(
                self._java_options(p), p if _os.path.isfile(p) else "auto")
        else:
            self.ex_java.set_value("auto")
        self._sync_java_hint()

    def _sync_java_hint(self):
        from ..core import javamanager
        v = self.ex_java.value()
        if v and v != "auto":
            home = javamanager.java_home_of(v)
            major = javamanager._release_major(home)[0] or javamanager._guess_major_from_path(home)
            self.lb_java.setText(f"当前：{v}" + (f"（Java {major}）" if major else ""))
        else:
            self.lb_java.setText("当前：自动选择 —— 按每个版本 json 里的 javaVersion 挑最合适的 JDK")

    # ---------- 官启档案 ----------
    def _request_reload(self):
        """「刷新」按钮：把连点合并到下一帧，并强制重读一次磁盘。"""
        self._force_reload = True
        if not self._reload_pending:
            self._reload_pending = True
            QTimer.singleShot(0, self._reload_soon)

    @staticmethod
    def _profile_sig(profs):
        """档案列表的「长相」。不依赖具体键名，任何字段变了都算变了。"""
        try:
            return tuple(repr(sorted((str(k), str(v))
                                    for k, v in (p or {}).items()))
                         for p in (profs or []))
        except Exception:                                 # noqa: BLE001
            return None

    def reload(self):
        self._reload_pending = False
        force = bool(getattr(self, "_force_reload", False))
        self._force_reload = False
        profs = installer.list_profiles()
        sig = self._profile_sig(profs)
        # 内容没变就别重建：9 条档案铺进 widgetResizable 滚动区实测 246ms，
        # 快速连点「刷新」会把这一下放大成「点一下卡一下」（第一百轮）。
        if (not force and sig is not None
                and sig == getattr(self, "_reload_sig", None)
                and self.body.count()):
            return
        self._reload_sig = sig
        while self.body.count():
            it = self.body.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        if not profs:
            lb = QLabel("还没有启动项。在官方启动器里安装一个版本，或从本软件装一个整合包。"
                        "（上面的全局启动设置不受影响，随时可用）")
            lb.setStyleSheet("color:#8a95a3;font-size:11px;")
            lb.setWordWrap(True)
            self.body.addWidget(lb)
        for p in profs:
            self.body.addWidget(_ProfileRow(p, self))
        self.body.addStretch(1)


# ================================================================ 删除确认
class ConfirmDeleteDialog(_DialogBase):
    """删除游戏版本 / 整合包：让用户选删除方式（彻底删除 / 移入回收站）。

    第 122 轮：不再只有「确定/取消」——先问「请选择游戏删除方式」。
    ``self.choice`` = "permanent"（彻底删除）/ "recycle"（移入回收站）/ ""（取消）。
    """

    def __init__(self, parent, name: str, kind: str = "instance"):
        what = "游戏版本" if kind == "version" else "整合包"
        super().__init__(parent, width=460, title=f"删除{what}")
        self.choice = ""                              # 取消时保持空串
        tip = QLabel(f"请选择游戏删除方式：\n「{name}」（{what}）")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#1f2733;font-size:13px;font-weight:bold;")
        self.body.addWidget(tip)

        if kind == "version":
            note = ("移入回收站：整个版本目录进 Windows 回收站，可随时还原。\n"
                    "彻底删除：整个版本目录直接永久删除，无法恢复。\n"
                    "引用它的启动项需重新绑定运行版本；还有整合包在用请先删整合包。")
        else:
            note = ("移入回收站：实例目录、官启启动项与安装记录一起进回收站，可还原。\n"
                    "彻底删除：连同实例目录、启动项与安装记录一起永久删除，无法恢复。\n"
                    "存档若在实例目录里也会一起删除，请先确认不需要存档。")
        lb = QLabel(note)
        lb.setWordWrap(True)
        lb.setStyleSheet("color:#8a6a2a;font-size:11px;background:rgba(232,164,66,26);"
                         "border-radius:10px;padding:10px;")
        self.body.addWidget(lb)
        # 彻底删除不可逆，用 accent 高亮 + 文案里写明后果；回收站是较温和的 ghost。
        self.add_buttons([("取消", "ghost", self.reject),
                          ("移入回收站", "ghost", self._pick_recycle),
                          ("彻底删除", "accent", self._pick_permanent)])

    def _pick_recycle(self):
        self.choice = "recycle"
        self.accept()

    def _pick_permanent(self):
        self.choice = "permanent"
        self.accept()


# ================================================================ 创建游戏分类
class CategoryDialog(_DialogBase):
    """创建分类（第 105 轮）：命名 + 选种类（游戏版本 / 整合包）。

    两处共用：游戏管理头部「+」、启动选择「已导入」右侧的「创建分类」。
    约束：整合包只能进「整合包」分类，游戏版本只能进「游戏版本」分类；
    这里的种类只是给分类定性，拖拽落点会再校验一次类型是否匹配。
    """

    def __init__(self, parent, default_kind="pack", default_name=""):
        super().__init__(parent, width=430, title="创建分类")
        from ..core import categories as cats
        self.name = ""
        self.kind = default_kind if default_kind in cats.KIND_KEYS else "pack"

        tip = QLabel("给分类起个名字，并选它装哪一类游戏。刚建好是空的，"
                     "到「游戏管理」里长按游戏拖进去即可；删分类不会动硬盘上的游戏。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#3a4654;font-size:12px;")
        self.body.addWidget(tip)

        self.ed = QLineEdit(str(default_name or ""))
        self.ed.setPlaceholderText("分类名，例如「1.20.1 整合包」")
        self.ed.setFixedHeight(32)
        self.ed.setStyleSheet(
            "QLineEdit{background:rgba(255,255,255,190);"
            "border:1px solid rgba(31,39,51,45);border-radius:8px;"
            "padding:0 9px;font-size:12px;color:#2a3442;}")
        self.ed.returnPressed.connect(self._ok)
        self.body.addWidget(self.ed)

        row = QHBoxLayout()
        row.setSpacing(14)
        grp = QButtonGroup(self)
        for k, n, d in cats.KINDS:
            rb = QRadioButton(n)
            rb.setToolTip(d)
            rb.setStyleSheet("color:#3a4654;font-size:12px;")
            if k == self.kind:
                rb.setChecked(True)
            rb.toggled.connect(lambda on, kk=k: self._pick(kk, on))
            grp.addButton(rb)
            row.addWidget(rb)
        row.addStretch(1)
        self.body.addLayout(row)

        note = QLabel("整合包只能放进「整合包」分类，游戏版本只能放进"
                      "「游戏版本」分类；拖错地方会提示，不会塞进去。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#8a95a3;font-size:11px;")
        self.body.addWidget(note)

        self.add_buttons([("取消", "ghost", self.reject),
                          ("创建", "accent", self._ok)])

    def _pick(self, k, on):
        if on:
            self.kind = k

    def _ok(self):
        nm = self.ed.text().strip()
        if not nm:
            Toast.show_toast(self, "先给分类起个名字", kind="error")
            return
        self.name = nm
        self.accept()


# ================================================================ 整合包内存分配
class PackMemoryDialog(_DialogBase):
    """整合包自己没声明内存时，让玩家手动分配（与设置页的运行内存控件一致）。

    结果：self.mb > 0 表示玩家分配了内存，0 表示「跳过，用官方启动器默认」。

    第一百零五轮：同一个弹窗里顺带问归类（玩家明确要求「在运行内存分配
    弹窗内提示」）。`category_ctx` 为 None 时这块完全不出现，老行为一字不变；
    `show_memory=False` 表示内存已由整合包声明、只用这个框问分类。
    无论玩家点「跳过」还是「应用分配」，分类选择都会被读走并落盘；
    选「否」= mode none，**绝不影响下载 / 导入已经成功**。
    """

    def __init__(self, parent, pack_name: str, default_mb: int = 0,
                 show_memory=True, category_ctx=None):
        super().__init__(parent, width=470, title="为整合包分配运行内存")
        self.mb = 0
        self.category_choice = {"mode": "none"}
        self._cat_ctx = category_ctx or None
        self._cat_combo = None
        self._cat_yes = None
        self._cat_name = None

        if show_memory:
            tip = QLabel(f"「{pack_name}」没有写明需要多少运行内存，请手动分配。\n"
                         f"这里写的就是官方启动器里的 JVM 参数（-Xmx），"
                         f"与「设置 → 运行内存」用同一套分配方式。")
            self.field = MemoryField(
                self, mb=default_mb or int(CONFIG.get("memory_mb") or 0),
                auto_min=bool(CONFIG.get("memory_auto_min", True)))
        else:
            tip = QLabel(f"「{pack_name}」的运行内存已经由整合包声明，不用再分配。\n"
                         f"顺带确认一下要不要给它归个类。")
            self.field = None
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#3a4654;font-size:12px;")
        self.body.addWidget(tip)
        if show_memory:
            self.body.addWidget(self.field)
            warn = QLabel("提示：分配过大反而拖慢启动。整合包一般 4~8 GB 足够。")
            warn.setStyleSheet("color:#8a95a3;font-size:11px;")
            warn.setWordWrap(True)
            self.body.addWidget(warn)

        if self._cat_ctx:
            self.body.addWidget(self._build_category_box())
        labels = (("跳过（用启动器默认）", "应用分配") if show_memory
                  else ("不分类", "确定"))
        self.add_buttons([
            (labels[0], "ghost", self._skip),
            (labels[1], "accent", self._ok),
        ])

    # ---------------- 归类询问 ----------------
    def _build_category_box(self):
        from ..core import categories as cats
        box = QWidget()
        box.setObjectName("catBox")
        box.setStyleSheet("#catBox{background:rgba(47,124,246,14);"
                          "border-radius:10px;}")
        v = QVBoxLayout(box)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(7)
        kind = str(self._cat_ctx.get("kind") or "pack")
        have = cats.categories(kind)
        if have:
            v.addWidget(QLabel("请问要把它放进哪个分类？"))
            self._cat_combo = QComboBox()
            self._cat_combo.setFixedHeight(30)
            self._cat_combo.addItem("不分类", "")
            for c in have:
                self._cat_combo.addItem(c["name"], c["id"])
            self._cat_combo.addItem("＋ 新建分类…", "__new__")
            self._cat_combo.currentIndexChanged.connect(self._on_cat_combo)
            v.addWidget(self._cat_combo)
            self._cat_name = QLineEdit(str(self._cat_ctx.get("name") or "新分类"))
            self._cat_name.setPlaceholderText("新分类的名字")
            self._cat_name.setVisible(False)
            v.addWidget(self._cat_name)
        else:
            lb = QLabel("请问是否创建分类？")
            f = QFont(theme.FONT)
            f.setPixelSize(12)
            f.setBold(True)
            lb.setFont(f)
            v.addWidget(lb)
            self._cat_yes = QRadioButton("是，创建并命名")
            self._cat_no = QRadioButton("否，不用分类")
            self._cat_no.setChecked(True)
            grp = QButtonGroup(box)
            for rb in (self._cat_yes, self._cat_no):
                rb.setStyleSheet("color:#3a4654;font-size:12px;")
                grp.addButton(rb)
                v.addWidget(rb)
            self._cat_yes.toggled.connect(self._on_cat_yes)
            self._cat_name = QLineEdit(str(self._cat_ctx.get("name") or "新分类"))
            self._cat_name.setPlaceholderText("给分类起个名字")
            self._cat_name.setVisible(False)
            v.addWidget(self._cat_name)
        return box

    def _on_cat_combo(self, _i):
        if self._cat_name is not None and self._cat_combo is not None:
            self._cat_name.setVisible(
                self._cat_combo.currentData() == "__new__")

    def _on_cat_yes(self, on):
        if self._cat_name is not None:
            self._cat_name.setVisible(bool(on))

    def _collect_choice(self):
        if not self._cat_ctx:
            return
        if self._cat_combo is not None:
            data = self._cat_combo.currentData()
            if not data:
                return
            if data == "__new__":
                nm = (self._cat_name.text().strip()
                      if self._cat_name is not None else "")
                if nm:
                    self.category_choice = {"mode": "new", "name": nm}
            else:
                self.category_choice = {"mode": "existing", "cid": str(data)}
            return
        if self._cat_yes is not None and self._cat_yes.isChecked():
            nm = (self._cat_name.text().strip()
                  if self._cat_name is not None else "")
            self.category_choice = {"mode": "new", "name": nm or "新分类"}

    def _skip(self):
        self.mb = 0
        self._collect_choice()
        self.accept()

    def _ok(self):
        self.mb = int(self.field.value()) if self.field is not None else 0
        self._collect_choice()
        self.accept()


# ================================================================ 两选一确认
class AskDialog(_DialogBase):
    """一句话 + 是 / 否（第九十八轮：总路径改完问「现在重启吗」）。

    第九十九轮修一个致命接线错误：以前两个按钮都 `accept()`，而
    `QDialog.exec()` 返回的是 **Accepted(1)/Rejected(0)**，`AskDialog.YES`
    刚好也是 1 —— 于是 `exec() == AskDialog.YES` 在玩家点「否」时同样成立，
    启动器照样当场重启（玩家感受就是「我点了否，它还是退出了」）。
    现在改用 `done(自己的一串码)`，并把选择记在 `choice` 上（原来叫 `result`，
    正好把 `QDialog.result()` 这个方法盖掉了，谁调谁炸）。
    """

    YES, NO = 1, 2

    def __init__(self, parent, title, text, yes_text="是", no_text="否",
                 width=470):
        super().__init__(parent, width=width, title=title)
        lb = QLabel(text)
        lb.setWordWrap(True)
        lb.setStyleSheet("color:#2a3442;font-size:12.5px;line-height:150%;")
        self.body.addWidget(lb)
        self.choice = self.NO
        self.add_buttons([(no_text, "ghost", lambda: self._pick(self.NO)),
                          (yes_text, "accent", lambda: self._pick(self.YES))])

    def _pick(self, v):
        self.choice = v
        self.done(v)


# ================================================================ 关闭确认
class CloseConfirmDialog(_DialogBase):
    YES, NO, CANCEL = 1, 2, 3

    def __init__(self, parent):
        super().__init__(parent, width=430)
        msg = QLabel("请问您是否将窗口最小化至托盘？")
        msg.setWordWrap(True)
        f = QFont(theme.FONT)
        f.setPixelSize(14)
        msg.setFont(f)
        self.body.addWidget(msg)
        tip = QLabel("「是」最小化到系统托盘 · 「否」退出程序 · 「取消」返回应用")
        tip.setStyleSheet("color:#5a6675;font-size:12px;")
        self.body.addWidget(tip)

        def done(v):
            self.done(v)
        self.add_buttons([
            ("是", "accent", lambda: done(self.YES)),
            ("否", "ghost", lambda: done(self.NO)),
            ("取消", "ghost", lambda: done(self.CANCEL)),
        ])


# ================================================================ 登录
class _CaretPill(PillButton):
    """文字右侧带小三角的胶囊按钮（第六十九轮）。

    用户要求：小三角要在「离线模式」**字样右边、按钮里面**（不是按钮外面另起一个），
    点小三角展开/收起第三方登录，点文字仍然是「离线模式」登录。所以做成子类：
      · sizeHint 多留 18px 给箭头（不留的话文字贴着边框，箭头没地方画）；
      · 箭头画在文字块右边缘外 2px，展开后朝上（收放式的直觉）；
      · mousePressEvent 先判 x：落在箭头区只发 `caret_clicked`，不进按钮的点击，
        否则点箭头会顺手把「离线模式」也登了。
    """

    caret_clicked = Signal()
    CARET_W = 18.0

    def __init__(self, text, kind="ghost", parent=None):
        super().__init__(text, kind, parent=parent)
        self._open = False

    def set_open(self, v):
        v = bool(v)
        if v != self._open:
            self._open = v
            self.update()

    def sizeHint(self):
        s = super().sizeHint()
        return QSize(int(s.width() + self.CARET_W), s.height())

    def _text_span(self):
        """文字块在按钮里的 (x, 宽) —— 算法与 PillButton.paintEvent 完全一致。"""
        fm = self.fontMetrics()
        tw = fm.horizontalAdvance(self.text())
        iw = 16 if self.icon_name else 0
        total = tw + (iw + 6 if iw else 0)
        x = (self.width() - total) / 2.0
        if iw:
            x += iw + 6
        return x, tw

    def _caret_rect(self):
        tx, tw = self._text_span()
        right = min(self.width() - 2.0, tx + tw + 2.0 + self.CARET_W)
        return QRectF(right - 8.0, self.height() / 2.0 - 4.0, 8.0, 8.0)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and \
                e.position().x() >= self._caret_rect().left() - 1.0:
            self.caret_clicked.emit()
            e.accept()
            return
        super().mousePressEvent(e)

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = self._caret_rect().center()
        w, h = 6.4, 3.9
        path = QPainterPath()
        if self._open:                      # 展开：朝上
            path.moveTo(c.x() - w / 2, c.y() + h / 2)
            path.lineTo(c.x() + w / 2, c.y() + h / 2)
            path.lineTo(c.x(), c.y() - h / 2)
        else:                               # 收起：朝下
            path.moveTo(c.x() - w / 2, c.y() - h / 2)
            path.lineTo(c.x() + w / 2, c.y() - h / 2)
            path.lineTo(c.x(), c.y() + h / 2)
        path.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff" if self.kind == "accent" else theme.C_TEXT))
        p.drawPath(path)
        p.end()


class LoginDialog(_DialogBase):
    logged_in = Signal(dict)

    def __init__(self, parent):
        super().__init__(parent, width=460, title="登录微软账户")
        self._busy = False
        self._cancel_flag = [False]

        self.tip = QLabel("选择登录方式：微软网页登录可在浏览器中完成账户验证；\n"
                          "离线模式仅用于展示皮肤与本地标识。")
        self.tip.setStyleSheet("color:#5a6675;font-size:12px;")
        self.tip.setWordWrap(True)
        self.body.addWidget(self.tip)

        self.ms_tip = QLabel("若浏览器登录页提示出错，可在“设置 → 微软登录 Client ID”\n"
                             "中填入自己的 ID 后重试。")
        self.ms_tip.setStyleSheet("color:#8a93a0;font-size:11px;")
        self.ms_tip.setWordWrap(True)
        self.ms_tip.hide()
        self.body.addWidget(self.ms_tip)

        # ---- 离线模式输入区 ----
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("离线模式：输入玩家名")
        self.name_edit.setFixedHeight(38)
        self.body.addWidget(self.name_edit)

        # ---- 微软设备码区（默认隐藏）----
        self.ms_box = QWidget()
        mb = QVBoxLayout(self.ms_box)
        mb.setContentsMargins(0, 0, 0, 0)
        mb.setSpacing(8)
        self.ms_code_label = QLabel("")
        cf = QFont(theme.FONT)
        cf.setPixelSize(26)
        cf.setBold(True)
        self.ms_code_label.setFont(cf)
        self.ms_code_label.setAlignment(Qt.AlignCenter)
        self.ms_code_label.setStyleSheet(
            "QLabel{background:rgba(47,124,246,30);color:#2f7cf6;"
            "border-radius:10px;padding:10px;}")
        self.ms_code_label.setCursor(Qt.PointingHandCursor)
        self.ms_code_label.setToolTip("点击复制设备码")
        self.ms_code_label.hide()

        def copy_code(e, lab=self.ms_code_label):
            QApplication.clipboard().setText(lab.text())
            self.ms_status.setText("已复制到剪贴板！粘贴到 microsoft.com/link 即可")

        self.ms_code_label.mousePressEvent = copy_code
        mb.addWidget(self.ms_code_label)
        self.ms_status = QLabel("")
        self.ms_status.setStyleSheet("color:#5a6675;font-size:12px;")
        self.ms_status.setWordWrap(True)
        mb.addWidget(self.ms_status)
        self.ms_box.hide()
        self.body.addWidget(self.ms_box)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#e84a42;font-size:12px;")
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)

        # ---- 按钮行：离线模式那颗自带小三角（收放第三方登录）----
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        b = PillButton("取消", "ghost")
        b.setFixedHeight(36)
        b.setMinimumWidth(92)
        b.clicked.connect(self.reject)
        self.btn_cancel = b
        row.addWidget(b)
        self.btn_offline = _CaretPill("离线模式", "ghost")
        self.btn_offline.setFixedHeight(36)
        self.btn_offline.setMinimumWidth(112)
        self.btn_offline.setToolTip("点文字=用离线模式登录；点文字右边的小三角=展开第三方登录")
        self.btn_offline.clicked.connect(self._offline)
        self.btn_offline.caret_clicked.connect(self._toggle_tp)
        row.addWidget(self.btn_offline)
        b = PillButton("微软网页登录", "accent")
        b.setFixedHeight(36)
        b.setMinimumWidth(92)
        b.clicked.connect(self._ms_login)
        row.addWidget(b)
        self.body.addLayout(row)

        # ---- 选项二 · 第三方登录（折叠在「离线模式」正下方）----
        self._tp_open = False
        self.tp_box = self._build_tp()
        self.body.addWidget(self.tp_box)
        self.tp_box.hide()      # 必须在 addWidget 之后：加进可见布局会被自动 show

    # ---- 选项二 · 第三方登录 ----
    def _build_tp(self):
        box = QWidget()
        box.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        cap = QLabel("选项二 · 第三方登录（皮肤站 / 面板服账号）")
        cap.setStyleSheet("color:#2f7cf6;font-size:11.5px;font-weight:600;"
                          "background:transparent;")
        lay.addWidget(cap)

        self.tp_server = QLineEdit()
        self.tp_server.setPlaceholderText("站点地址（皮肤站 / 面板服给的 API 地址）")
        self.tp_user = QLineEdit()
        self.tp_user.setPlaceholderText("邮箱（或站点要求的账号）")
        self.tp_pass = QLineEdit()
        self.tp_pass.setPlaceholderText("密码")
        self.tp_pass.setEchoMode(QLineEdit.Password)
        for w in (self.tp_server, self.tp_user, self.tp_pass):
            w.setFixedHeight(36)
            lay.addWidget(w)
        self.tp_pass.returnPressed.connect(self._tp_login)
        self.tp_user.returnPressed.connect(lambda: self.tp_pass.setFocus())

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.tp_btn = PillButton("登录第三方账号", "accent")
        self.tp_btn.setFixedHeight(34)
        self.tp_btn.clicked.connect(self._tp_login)
        row.addWidget(self.tp_btn)
        row.addStretch(1)
        self.tp_status = QLabel("")
        self.tp_status.setStyleSheet("color:#5a6675;font-size:11px;"
                                     "background:transparent;")
        self.tp_status.setWordWrap(True)
        row.addWidget(self.tp_status, 1)
        lay.addLayout(row)

        hint = QLabel("站点地址示例：https://littleskin.cn/api/yggdrasil"
                      "（只填域名也能认）。\n"
                      "启动时会自动注入 authlib-injector，皮肤与登录验证都走这个站；"
                      "密码只用来换一次登录凭证，不会写进配置文件。")
        hint.setStyleSheet("color:#8a93a0;font-size:11px;background:transparent;")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        box.hide()
        return box

    def _toggle_tp(self):
        if self._busy:
            return
        open_ = not getattr(self, "_tp_open", False)
        self._tp_open = open_
        if open_:
            # 两个选项互斥：展开选项二就把离线输入 / 设备码区收起来，
            # 免得玩家不知道该填哪一栏。
            self.ms_box.hide()
            self.ms_tip.hide()
            self.name_edit.hide()
        else:
            self.name_edit.show()
        self.tp_box.setVisible(open_)
        self.btn_offline.set_open(open_)
        self.status.setText("")
        self.tp_status.setText("")
        (self.tp_server if open_ else self.name_edit).setFocus()
        self.adjustSize()

    def _tp_login(self):
        if self._busy:
            return
        server = self.tp_server.text().strip()
        acct = self.tp_user.text().strip()
        pwd = self.tp_pass.text()
        if not server:
            self.tp_status.setText("请填写站点地址")
            return
        if not acct:
            self.tp_status.setText("请填写邮箱（或站点账号）")
            return
        if not pwd:
            self.tp_status.setText("请填写密码")
            return
        from ..core import authlib
        self._busy = True
        self.status.setText("")
        self.tp_status.setText("正在连接站点…")
        self.tp_btn.setEnabled(False)

        def done(sess):
            from ..core.session import save_session
            save_session(sess)
            self._busy = False
            self.tp_btn.setEnabled(True)
            self.logged_in.emit(sess)
            self.accept()

        def err(e):
            self._busy = False
            self.tp_btn.setEnabled(True)
            self.tp_status.setText("")
            self.status.setText(str(e)[:160])

        run_async(authlib.authenticate, server, acct, pwd,
                  on_done=done, on_err=err)

    # ---- 离线 ----
    def _offline(self):
        name = self.name_edit.text().strip()
        if not name:
            self.status.setText("离线模式请先输入玩家名")
            return
        from ..core.session import offline_session, save_session
        s = offline_session(name)
        save_session(s)
        self.logged_in.emit(s)
        self.accept()

    # ---- 微软网页登录 ----
    def _ms_login(self):
        if self._busy:
            return
        from ..core import msa
        self._busy = True
        self.name_edit.hide()
        # 选项二还开着就一并收起，别让「正在申请设备码」和三格表单挤在一起
        self._tp_open = False
        self.tp_box.hide()
        self.btn_offline.set_open(False)
        self.ms_box.show()
        self.ms_tip.show()
        self.ms_code_label.hide()
        self.ms_status.setText("正在向微软申请设备码…")
        self.status.setText("")

        def code_done(d):
            self._busy = False
            code = d.get("user_code", "")
            if not code:
                self.ms_status.setText(f"申请失败：{str(d)[:80]}")
                return
            self.ms_code_label.setText(code)
            self.ms_code_label.show()
            # 自动复制到剪贴板，点击代码可再次复制
            QApplication.clipboard().setText(code)
            self.ms_status.setText(
                "已自动复制设备码！在浏览器打开 microsoft.com/link 粘贴即可登录\n"
                "（点击上方代码可再次复制）")
            try:
                msa.open_login_page(d.get("verification_uri",
                                          "https://www.microsoft.com/link"))
            except Exception:
                pass

            def poll_progress(text):
                self.ms_status.setText(text)

            def poll_done(session):
                from ..core.session import save_session
                save_session(session)
                self._busy = False
                self.logged_in.emit(session)
                self.accept()

            def poll_err(e):
                self._busy = False
                self.status.setText(str(e)[:100])

            run_async(msa.login_full, d["device_code"], int(d.get("interval", 5)),
                      int(d.get("expires_in", 900)), progress=poll_progress,
                      cancel_flag=self._cancel_flag, on_done=poll_done,
                      on_err=poll_err)

        def code_err(e):
            self._busy = False
            self.status.setText(str(e)[:100])

        # 设备码申请放工作线程（避免网络阻塞 UI）
        run_async(msa.device_code_start, on_done=code_done, on_err=code_err)

    def reject(self):
        self._cancel_flag[0] = True
        super().reject()


# ================================================================ 设置
_NAV_QSS = """
QPushButton{border:none;background:transparent;color:#3a4757;text-align:left;
padding:9px 14px;border-radius:9px;font-size:13px;}
QPushButton:hover{background:rgba(31,39,51,16);}
QPushButton:checked{background:rgba(47,124,246,26);color:#2f7cf6;font-weight:600;}
"""

_PAGE_TIP = "color:#5a6675;font-size:11px;"

_SEG_QSS = """
QPushButton{border:none;border-radius:8px;font-size:11px;padding:0px;
background:rgba(255,255,255,140);color:#4c5866;}
QPushButton:hover{background:rgba(255,255,255,220);}
QPushButton:checked{background:#2f7cf6;color:#ffffff;font-weight:600;}
"""

# 没有镜像的源：只留一个灰标签，看着就不像能点（第 52 轮）
_SEG_OFF_QSS = """
QPushButton{border:none;border-radius:8px;font-size:11px;padding:0px;
background:rgba(120,132,146,40);color:#8c97a3;font-weight:600;}
"""


class _SourceRow(QWidget):
    """一行下载源：名称 + 说明 + 官方/自动/镜像 三选一。"""

    def __init__(self, key, name, desc, value, official_host="", mirror_host=""):
        super().__init__()
        self.key = key
        self.setAttribute(Qt.WA_StyledBackground, True)
        # 第九十八轮：选择器用 **#objectName** 而不是裸 `QWidget{…}`。
        # 裸选择器会把这一段 QSS 套到**每一个子控件**上（行里还有 3 个按钮 +
        # 3 条 label），11 个源 × 每个 6 个子控件 = 上百次多余 polish，
        # 「下载镜像源」页实测 316ms，其中一大截花在这儿。
        self.setObjectName("srcRow")
        self.setStyleSheet(
            "#srcRow{background:rgba(255,255,255,150);border-radius:10px;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 10, 8)
        lay.setSpacing(5)

        top = QHBoxLayout()
        top.setSpacing(8)
        t = QLabel(name)
        t.setStyleSheet("color:#1f2733;font-size:12px;font-weight:600;"
                        "background:transparent;")
        top.addWidget(t)
        top.addStretch(1)

        from PySide6.QtWidgets import QButtonGroup, QPushButton
        from ..core import mirror
        # 没有国内镜像的源（Modrinth）：只摆一个「官方」，
        # 不给点了没反应的开关，并在下面写清楚为什么。
        self._no_mirror = not mirror_host
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._btns = {}
        if self._no_mirror:
            b = QPushButton("官方")
            b.setCheckable(False)        # 纯标签：不给「能点」的错觉
            b.setEnabled(False)
            b.setCursor(Qt.ArrowCursor)
            b.setFixedSize(48, 24)
            b.setStyleSheet(_SEG_OFF_QSS)
            b.setToolTip("这个源没有国内镜像，只能走官方")
            top.addWidget(b)
            self._btns["official"] = b
        else:
            for i, m in enumerate(mirror.MODES):
                b = QPushButton(mirror.MODE_CN[m])
                b.setCheckable(True)
                b.setCursor(Qt.PointingHandCursor)
                b.setFixedSize(48, 24)
                b.setStyleSheet(_SEG_QSS)
                b.setChecked(m == value)
                self._group.addButton(b, i)
                top.addWidget(b)
                self._btns[m] = b
        lay.addLayout(top)

        d = QLabel(desc)
        d.setStyleSheet("color:#6b7784;font-size:11px;background:transparent;")
        d.setWordWrap(True)
        lay.addWidget(d)

        if self._no_mirror:
            txt = f"官方 {official_host}　（无国内镜像）"
        else:
            txt = f"官方 {official_host}　→　镜像 {mirror_host}"
        hosts = QLabel(txt)
        hosts.setStyleSheet("color:#9aa4b0;font-size:10px;background:transparent;")
        hosts.setWordWrap(True)
        lay.addWidget(hosts)

    def value(self):
        """当前选中的源模式。

        **必须容忍缺按钮**：没有国内镜像的源（Modrinth）只摆了一个
        灰的「官方」标签，`_btns` 里根本没有 "auto"/"always"。第五十六轮之前这里
        直接 `self._btns[m]` → KeyError('auto') → save() 在 accept() 之前就炸了，
        表现就是「点保存窗口纹丝不动」（而且要等点了下载镜像源那页才踩得到）。
        """
        from ..core import mirror
        for m in mirror.MODES:
            b = self._btns.get(m)
            if b is not None and b.isChecked():
                return m
        return "official" if self._no_mirror else "auto"


_GROUP_QSS = """
QPushButton{text-align:left;padding-left:10px;border:none;border-radius:9px;
background:rgba(255,255,255,120);color:#33404f;font-size:12px;font-weight:600;}
QPushButton:hover{background:rgba(255,255,255,195);}
QPushButton:checked{background:rgba(255,255,255,165);}
"""


class _SourceGroup(QWidget):
    """下载源的可折叠分组（第 52 轮：镜像源页从平铺改成展开式分类）。

    标题行点一下折叠 / 展开；行控件仍是 `_SourceRow`，保存逻辑一行没动 ——
    `_page_mirror` 只负责把行按 group 分好塞进来。
    """

    def __init__(self, title, note="", expanded=True):
        super().__init__()
        from PySide6.QtWidgets import QPushButton
        self._open = bool(expanded)
        self._title = title
        self._note = note
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self.head = QPushButton()
        self.head.setCursor(Qt.PointingHandCursor)
        self.head.setCheckable(True)
        self.head.setChecked(self._open)
        self.head.setFixedHeight(30)
        self.head.setStyleSheet(_GROUP_QSS)
        self.head.clicked.connect(self._toggle)
        lay.addWidget(self.head)

        self.body = QWidget()
        self.body.setStyleSheet("background:transparent;")
        self._bl = QVBoxLayout(self.body)
        self._bl.setContentsMargins(2, 0, 2, 2)
        self._bl.setSpacing(5)
        lay.addWidget(self.body)
        # 第 105 轮：以前只在 _toggle() 里同步可见性，构造时 expanded=False
        # 的分组 body 其实是**可见**的（镜像源页全用 True，一直没暴露）。
        # 现在「高级启动参数」要默认收起，这里必须按初始值同步一次。
        self.body.setVisible(self._open)
        self._render()

    def add(self, w, stretch=0):
        self._bl.addWidget(w, stretch)

    def _toggle(self):
        self._open = self.head.isChecked()
        self.body.setVisible(self._open)
        self._render()

    def _render(self):
        mark = "▾" if self._open else "▸"
        extra = f"　·　{self._note}" if self._note else ""
        self.head.setText(f"{mark}　{self._title}{extra}")


class _BgCell(QWidget):
    """九宫格里的一张背景缩略图（点击选中，当前那张带高亮圈）"""
    clicked = Signal(int)

    W, H = 112, 70

    def __init__(self, name, index, selected=False, parent=None):
        super().__init__(parent)
        self.name = name
        self.index = index
        self.selected = selected
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"第 {index + 1} 张")
        self._hover = False

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.index)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        path = QPainterPath()
        path.addRoundedRect(QRectF(1, 1, self.W - 2, self.H - 2), 9, 9)
        p.setClipPath(path)
        from . import wallpaper as _wp
        im = _wp.thumb(self.name, self.W - 4, self.H - 4)
        if im is not None:
            p.drawImage(2, 2, im)
        else:
            p.fillPath(path, QColor(226, 231, 238))
        if self._hover and not self.selected:
            p.fillPath(path, QColor(255, 255, 255, 26))
        p.setClipping(False)
        if self.selected:
            # 选中：外面套一圈绿环（别用绿底盖图，会看不清是哪张）
            p.setPen(QPen(QColor(92, 186, 132), 2.4))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(1.6, 1.6, self.W - 3.2, self.H - 3.2), 9, 9)


class _ScrollPage(QScrollArea):
    """设置里的单页容器：内容按「首选高度」撑开，超出视口才出现细滚动条。

    裸 QScrollArea(widgetResizable=True) 只把内容拉到视口高，行数一变
    （幻灯片开关会在「切换间隔 / 背景图」之间换行）末尾就被裁掉半行；
    这里显式按布局首选高度对齐，并把纵向滚动条归零到顶部。
    """

    def __init__(self, inner):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.viewport().setAutoFillBackground(False)
        self.setWidget(inner)
        self._inner = inner

    def fit(self):
        w = self._inner
        lw = w.layout()
        if lw is None:
            return
        # 第九十三轮：内容自己长高/长宽（收放式卡片展开）时没人调 fit()，
        # 而 fit() 又会拿缓存的 sizeHint 去算 —— 先 invalidate 才不会少算一截
        lw.invalidate()
        vw = max(120, self.viewport().width())
        h = w.heightForWidth(vw)
        if h <= 0:
            h = lw.sizeHint().height()
        h = max(h, self.viewport().height())
        # 加守卫：滚动条出现/消失会再次触发 resizeEvent，别来回抖
        if w.minimumHeight() != h:
            w.setMinimumHeight(h)
        if w.width() != vw or w.height() != h:
            w.resize(vw, h)

    def showEvent(self, e):
        super().showEvent(e)
        self.fit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.fit()


class _BgPicker(QWidget):
    """选背景图的浮层（Qt.Popup：点外面自动关）"""

    def __init__(self, parent, cur=0, on_pick=None):
        super().__init__(parent, Qt.Popup)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._on_pick = on_pick
        from . import wallpaper as _wp
        self._names = _wp.available()
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(9)
        t = QLabel("选择背景图")
        f = QFont(theme.FONT)
        f.setPixelSize(12)
        f.setBold(True)
        t.setFont(f)
        t.setStyleSheet("background:transparent;")
        v.addWidget(t)
        g = QGridLayout()
        g.setSpacing(8)
        self._cells = []
        for i, name in enumerate(self._names):
            c = _BgCell(name, i, i == int(cur) % max(len(self._names), 1))
            c.clicked.connect(self._pick)
            g.addWidget(c, i // 3, i % 3)
            self._cells.append(c)
        v.addLayout(g)
        hint = QLabel("幻灯片关闭时使用这张 · 天空仍按现实时间变化")
        hint.setStyleSheet("color:#6b7684;font-size:10.5px;background:transparent;")
        v.addWidget(hint)
        self.adjustSize()

    def _pick(self, i):
        try:
            if self._on_pick:
                self._on_pick(int(i))
        finally:
            self.close()

    def show_at(self, anchor):
        self.adjustSize()
        gp = anchor.mapToGlobal(QPoint(anchor.width() - self.width(), anchor.height() + 6))
        scr = QGuiApplication.screenAt(gp) or QGuiApplication.primaryScreen()
        r = scr.availableGeometry()
        x = min(max(gp.x(), r.left() + 8), r.right() - self.width() - 8)
        y = gp.y()
        if y + self.height() > r.bottom() - 8:
            y = anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 6
        y = min(max(y, r.top() + 8), r.bottom() - self.height() - 8)
        self.move(x, y)
        self.show()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, 15, 15)
        p.fillPath(path, QColor(252, 253, 255, 249))
        p.setPen(QPen(QColor(31, 39, 51, 34), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)


class SettingsDialog(_DialogBase):
    """设置：左右分栏（左分类 / 右设置区）"""

    def __init__(self, parent):
        # 宽度先按主窗口收一次：主窗口本身窄的时候（窄屏 / 小窗），780 会顶出窗口
        pw = parent.width() if parent is not None else 780
        dw = max(660, min(780, pw - 32))
        super().__init__(parent, width=dw, title=f"设置    v{APP_VERSION}")
        from PySide6.QtWidgets import QButtonGroup, QPushButton, QStackedWidget

        from ..core import relink
        self._relink = relink
        self._mk_src = os.path.expandvars(r"%APPDATA%\.minecraft")

        # ---- 「?」注解开关（第五十五轮）----
        # 与「设置 vX」同一行、右端对齐；默认隐藏注解（玩家只想看开关，不想看说明）。
        # 状态存配置键 show_tips，下次开设置还是上次的样子。
        self._tips = []
        self._tips_on = bool(CONFIG.get("show_tips", False))
        # 第一百一十六轮：标题栏「?」左侧的界面语言切换（简体 / 繁体▾）。
        # 字号刻意小（10px），不与「设置 vX」标题抢视觉；点「繁体▾」向下
        # 展开台式 / 港式 / 澳式三选一。切换即时生效并刷新全界面。
        self._build_lang_switcher()
        self.btn_help = IconButton("help", 30, 21)
        self.btn_help.setToolTip("显示 / 隐藏各页面的说明文字（默认隐藏）")
        self.btn_help.clicked.connect(self._toggle_tips)
        if self.head is not None:
            self.head.addWidget(self.btn_help, 0, Qt.AlignVCenter)
        self._refresh_help_btn()

        box = QWidget()
        self._box = box
        # 高度不再写死 580：按「最高的一页」实测高度给，并且不超过主窗口高度。
        # 见 _fit_box() —— 矮窗口里就收窄高度、交给 _ScrollPage 滚，
        # 免得设置窗比主窗口还高、上下都探出窗口外。
        box.setFixedHeight(560)
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(16)

        # ---- 左侧分类 ----
        navw = QWidget()
        navw.setFixedWidth(146)
        nl = QVBoxLayout(navw)
        nl.setContentsMargins(0, 2, 0, 0)
        nl.setSpacing(3)
        self.stack = QStackedWidget()
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        pages = [
            ("常规", self._page_general),
            ("游戏目录", self._page_paths),
            ("启动项", self._page_startup),
            ("运行内存", self._page_memory),
            ("下载镜像源", self._page_mirror),
            ("微软登录", self._page_msa),
            ("目录迁移", self._page_migrate),
        ]
        for i, (label, builder) in enumerate(pages):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(36)
            b.setStyleSheet(_NAV_QSS)
            b.setChecked(i == 0)
            self._nav_group.addButton(b, i)
            nl.addWidget(b)
            # 第五十三轮：**懒建**。7 页一次性建完实测 620ms（205 次 setStyleSheet、
            # 7 个滚动区），玩家看到的就是「点设置卡一下才出弹窗」。现在只先建第一页，
            # 其余在显示之后逐帧补（每帧一页），点开就是瞬间出来、翻页时早就好了。
            self.stack.addWidget(QWidget())          # 占位，_build_page 时替换
        self._page_defs = pages
        self._built_pages = set()
        self._build_page(0)
        nl.addStretch(1)
        self._nav_group.idClicked.connect(self._goto_page)
        h.addWidget(navw)
        h.addWidget(self.stack, 1)
        self.body.addWidget(box)
        self._fit_box()          # 先按 560 定下弹窗尺寸（_fit_box 里有懒建保护）

        def save():
            """保存并关闭（第五十六轮：**保证一定会关**）。

            改之前 `_save_mirror()` 里 `_SourceRow.value()` 抛 KeyError('auto')，
            异常一路冒到 Qt 事件循环，`self.accept()` 永远走不到 —— 玩家点了「保存」
            窗口纹丝不动，看起来就是「保存没反应、不自动返回」。
            现在：先尽量把每一项都存下去（单项失败只记下来，不打断后面的），
            最后无论如何 accept()，出错的用一条 toast 说清楚。

            第九十八轮：改了**总路径**就在关窗之前问一句「现在重启吗」——
            真正的搬迁交给 core/relocate.py（重启时 / 关启动器时），这里只负责问，
            绝不在窗口开着的时候动玩家的目录。
            """
            bad = []
            restart = False
            try:
                self._save_all(bad)
            except Exception as e:                      # noqa: BLE001
                bad.append(f"{type(e).__name__}: {str(e)[:60]}")
            mv = getattr(self, "_root_move", None)
            if mv:
                d = AskDialog(
                    self.parent(), "总路径已改，要现在重启吗？",
                    f"会把整份 {os.path.basename(mv[0])} 从\n{mv[0]}\n"
                    f"搬到\n{mv[1]}\n\n"
                    "· 点「是」：现在重启，重启过程中搬过去；\n"
                    "· 点「否」：什么都不动，路径还是原来那个，关窗口也不搬；"
                    "只有你下次重开启动器时才搬过去。",
                    yes_text="是，现在重启", no_text="否，下次重启再搬")
                d.exec()
                # 判据是**玩家点了哪个按钮**，不是 exec() 的返回值（见 AskDialog 注释）
                restart = (d.choice == AskDialog.YES)
            self.accept()
            if bad:
                Toast.show_toast(self.parent(),
                                 f"有 {len(bad)} 项没存上：{bad[0]}", ms=6000,
                                 kind="error")
            if mv and not restart:
                Toast.show_toast(
                    self.parent(),
                    "总路径没动，还是原来那个；关窗口也不搬，等下次重开启动器时才搬过去",
                    ms=5600, kind="info")
            if restart:
                win = self.parent()
                QTimer.singleShot(120, lambda: getattr(
                    win, "restart_app", lambda: None)())

        self.add_buttons([("取消", "ghost", self.reject), ("保存", "accent", save)])

    def _save_all(self, bad=None):
        """把所有页面的取值写进配置。bad 用于收集单项失败（见 save()）。

        第九十八轮：整段写盘套进 `CONFIG.batch()`。里面三十来个 `CONFIG.set`
        以前每一条都把整份 config.json 重写一次（实测 6.1ms/次），光写盘就
        180ms —— 和「保存后返回页面卡一下」是同一件事。现在只落一次。
        """
        bad = bad if bad is not None else []
        # 懒建：还没翻到过的页这里必须补齐 —— 下面要读每一页的控件取值
        self._build_rest()
        with CONFIG.batch():
            self._save_all_inner(bad)

    def _save_all_inner(self, bad):
        CONFIG.set("launcher_path", self.ed_launcher.text().strip())
        # 第四十五轮：游戏目录改成两条只读路径（应用 / 官方），
        # 这里只保存「下载目标」—— game_dir 由迁移与安装向导维护。
        from ..core import paths as _paths
        # 关窗弹窗：默认关（点 × 直接退出），开了才弹「最小化 / 退出」确认
        CONFIG.set("close_confirm", bool(self.sw_close_confirm.isChecked()))
        # 第一百一十六轮：跟随系统语言开关（切换时 i18n 已即时写盘，这里兜底同步，
        # 防止只动开关、回调因懒建页未触发的情况）
        from ..core import i18n as _i18n
        _sw_follow = getattr(self, "sw_follow_lang", None)
        if _sw_follow is not None and bool(_sw_follow.isChecked()) != (_i18n.mode() == "follow"):
            if _sw_follow.isChecked():
                _i18n.follow_system()
            else:
                _i18n.set_mode("manual", _i18n.current_lang())
        CONFIG.set("msa_client_id", self.ed_msa.text().strip())
        # ---- 启动项（第四十四轮）：版本隔离 / 优先进程 / Java 走 launchprefs ----
        from ..core import launchprefs as _lp
        _lp.set_iso_mode(self.startup.ex_iso.value())
        _lp.set_priority(self.startup.ex_prio.value())
        _java = self.startup.ex_java.value()
        if _java in ("", "auto", "__add__"):
            _lp.set_java_choice("auto")
        else:
            _lp.set_java_choice("path", _java)
        # 第一百零四轮：全局自定义参数 / 预启动命令 / 窗口 / javaw / IP 栈。
        # 全用 getattr 兜底：这一页懒建，某些测试路径可能根本没构造过它。
        _st = getattr(self, "startup", None)
        if _st is not None:
            def _cb(name, key, default=False):
                w = getattr(_st, name, None)
                CONFIG.set(key, bool(w.isChecked()) if w is not None else default)

            def _ed(name, key, default=""):
                w = getattr(_st, name, None)
                CONFIG.set(key, w.text().strip() if w is not None else default)

            _cb("cb_jvm_opt", "jvm_opt_default")
            _ed("ed_jvm", "jvm_args_global")
            _ed("ed_game", "game_args_global")
            _ed("ed_pre", "pre_launch_global")
            _cb("cb_pre_wait", "pre_launch_wait", True)
            _cb("cb_javaw", "javaw")
            _ed("ed_server", "server_to_enter")
            _ip = getattr(_st, "ed_ipstack", None)
            _ips = (_ip.text().strip().lower() if _ip is not None else "auto")
            CONFIG.set("ip_stack", _ips if _ips in ("auto", "ipv4", "ipv6") else "auto")
            _wd = getattr(_st, "ed_window", None)
            if _wd is not None:
                parts = _wd.text().strip().split("|")
                try:
                    w, h = parts[0].lower().split("x", 1)
                    CONFIG.set("window_width", max(320, int(w)))
                    CONFIG.set("window_height", max(240, int(h)))
                except (ValueError, IndexError):
                    pass
                mode = parts[1].strip() if len(parts) > 1 else "windowed"
                CONFIG.set("window_mode",
                           mode if mode in _lp.WINDOW_MODE_KEYS else "windowed")
                CONFIG.set("window_title", parts[2].strip() if len(parts) > 2 else "")
        # ---- 运行内存：分配方式 + 值（自动时存的是算出来的建议值） ----
        CONFIG.set("memory_mb", int(self.mem_spin.value()))
        _lp.set_memory_mode(self.ex_mem.value())
        CONFIG.set("memory_auto_min", bool(self.cb_xms.isChecked()))
        # C2：LockMemory / 独立初始堆（懒建未建时用 getattr 兜底）
        _lock = getattr(self, "cb_lock", None)
        if _lock is not None:
            CONFIG.set("ram_lock_memory", bool(_lock.isChecked()))
        _init = getattr(self, "sp_init", None)
        if _init is not None:
            CONFIG.set("ram_init_mb", int(_init.value()))
        _rt = getattr(self, "_ram_timer", None)
        if _rt is not None:
            _rt.stop()
        # 独立启动开关：**打开就直接切到独立启动**。
        # 第九十七轮（玩家反馈）：以前只写了「关掉时拉回官方」这一半，打开时
        # `launch_mode` 还停在 official，主页那颗按钮仍旧写「启动官方启动器」，
        # 得自己点右侧小三角才发现能切 —— 玩家以为「开了跟没开一样」。
        # 只在**刚翻转**这一刻动模式：已经开着的不去覆盖玩家用小三角做的临时选择。
        from ..core import gamelaunch
        on = bool(self.sw_standalone.isChecked())
        was = gamelaunch.standalone_enabled()
        CONFIG.set("standalone_launch", on)
        if not on:
            gamelaunch.set_launch_mode("official")
        elif not was:
            gamelaunch.set_launch_mode("standalone")
            # 第 114 轮：只在「刚打开独立启动」这一刻把粘住的官方目标清掉，
            # 否则 download_target="official" 会盖住开关、下载仍走官启目录。
            # 下拉框也同步回「自动」，不然下面的保存逻辑又把它写回去。
            _paths.reset_target()
            if hasattr(self, "ex_target"):
                self.ex_target.set_value("auto")
        # 第四十六轮：目标「自动」= 跟随独立启动（关=官方，开=应用）。
        # 设置页里选的就是明确值；选「自动」则把明确值清掉、重新跟随开关。
        # 第 114 轮：独立启动开着时无论下拉写什么，target() 都强制走应用，
        # 这里不再重复写死一个会被忽略的显式值。
        _tv = str(self.ex_target.value() or "")
        if on:
            _paths.reset_target()
        elif _tv in _paths.TARGETS:
            _paths.set_target(_tv)
        else:
            _paths.reset_target()
        _paths.reset_official_cache()   # 官启路径可能刚改过，下次 target() 重测
        # 自动导入：只影响「添加已有文件夹」的行为，随时可改
        CONFIG.set("auto_import", bool(self.sw_auto_import.isChecked()))
        # 自动补全（第五十四轮）：装完照 manifest.json 核对并补回缺的文件
        CONFIG.set("auto_complete", bool(self.sw_auto_complete.isChecked()))
        # 自动中文（第五十五轮）：启动前把 options.txt 的 lang 写成 zh_cn
        from ..core import langfix as _lf
        CONFIG.set(_lf.ENABLED_KEY, bool(self.sw_auto_chinese.isChecked()))
        # 启动切输入法：下次点启动就按这个来（core/ime.py 读同一个键）
        from ..core import ime as _ime
        CONFIG.set(_ime.ENABLED_KEY, bool(self.sw_ime.isChecked()))
        # 内置文件管理器（第六十轮）：ui/fspicker.py 每次调用现读这个键，
        # 不用重启，关掉后下一次选文件就走系统对话框
        CONFIG.set("builtin_fs", bool(self.sw_builtin_fs.isChecked()))
        # 引导教程（第八十轮）：开着且还没走完 = 下次进主界面自动走；
        # 从关到开 = 立刻再来一遍（玩家点它就是想再看一次）。
        _tour_on = bool(self.sw_tour.isChecked())
        _tour_was = bool(CONFIG.get("tour_enabled", True))
        CONFIG.set("tour_enabled", _tour_on)
        _win = self.parent()
        if _tour_on and not _tour_was:
            CONFIG.set("tour_done", False)
            # 延迟一拍再起：这会儿设置窗还没关，遮罩会盖在设置窗下面
            QTimer.singleShot(
                420, lambda w=_win: getattr(w, "start_tour", lambda *a: None)(True)
                if w is not None else None)
        elif not _tour_on:
            getattr(_win, "stop_tour", lambda: None)()
        # 卡片隐藏：写完立刻让主窗按新设置重排计时器（现在管所有页面）
        CONFIG.set("card_auto_hide", bool(self.sw_card_hide.isChecked()))
        CONFIG.set("card_hide_secs", int(self._card_secs))
        win = self.parent()
        hook = getattr(win, "apply_auto_hide", None)
        if hook:
            try:
                hook()
            except Exception:
                pass
        else:
            page = getattr(win, "page_home", None)
            hook2 = getattr(page, "apply_card_hide", None)
            if hook2:
                try:
                    hook2()
                except Exception:
                    pass
        # 背景幻灯片：关掉时改用 bg_pick 指定的那张（天空两种情况下都跟现实时间走）
        CONFIG.set("slideshow", bool(self.sw_slide.isChecked()))
        CONFIG.set("slide_secs", int(self._slide_secs))
        CONFIG.set("bg_pick", int(self._bg_pick))
        hook_wp = getattr(win, "apply_wallpaper", None)
        if hook_wp:
            try:
                hook_wp()
            except Exception:
                pass
        # 镜像源（逐项）—— 单项失败不影响别的项，但要记下来
        try:
            self._save_mirror()
        except Exception as e:                      # noqa: BLE001
            bad.append(f"下载源：{str(e)[:40]}")
        # 数据目录搬家放最后：前面那些 CONFIG.set 已经写进旧目录那份
        # config.json，这里整份带过去，新位置一落地就是最新状态。
        try:
            self._apply_data_dir()
        except Exception as e:                      # noqa: BLE001
            bad.append(f"数据目录：{str(e)[:40]}")
        # 总路径（Osmanthus flower）：**只记待办，不当场搬**（见 core/relocate.py）。
        # 待办落在数据目录里，它不在 Osmanthus flower 里面，搬完也不会跟着丢。
        self._root_move = None
        new_root = str(getattr(self, "_root_new", "") or "")
        if new_root:
            from ..core import relocate as _rl
            ok2, msg2 = _rl.set_pending(self._root_now, new_root)
            if ok2:
                self._root_move = (self._root_now, new_root)
            else:
                bad.append(f"总路径：{msg2}")
                self.lb_root_state.setText(f"没记下：{msg2}")
        ok, msg = False, ""
        try:
            ok, msg = installer.apply_memory_to_profiles()
        except Exception as e:                      # noqa: BLE001
            bad.append(f"内存档案：{str(e)[:40]}")
        if ok:
            Toast.show_toast(self.parent(), f"设置已保存，{msg}", kind="success")
        elif "正在运行" in msg:
            Toast.show_toast(self.parent(), msg, ms=5000, kind="error")
        elif not bad:
            Toast.show_toast(self.parent(), "设置已保存", kind="success")

    # ---------- 页面构建 ----------
    def _build_page(self, i):
        """建第 i 页（幂等）。懒建的理由见 __init__ 里的注释。"""
        if i in self._built_pages:
            return
        self._built_pages.add(i)
        ph = self.stack.widget(i)
        try:
            w = self._scrolled(self._page_defs[i][1]())
        except Exception:                                # noqa: BLE001
            w = self._scrolled(QWidget())                # 单页炸了也别让设置打不开
        try:
            # 把这一页的「小字注解」收进登记表：之后点「?」统一显隐。
            # 懒建的页在建的这一刻就按当前开关状态摆好，不会出现「翻过去注解还在」
            self._reg_tips(getattr(w, "_inner", w))
        except Exception:                                # noqa: BLE001
            pass
        cur = self.stack.currentIndex()
        self.stack.insertWidget(i, w)
        # QStackedWidget.insertWidget 会把「当前页」抢成刚插进来的那一页。
        # 懒建发生在翻页/保存时，不还原就会出现「点的是下载镜像源，看到的是目录迁移」。
        if cur >= 0:
            self.stack.setCurrentIndex(cur)
        if ph is not None and ph is not w:
            self.stack.removeWidget(ph)
            ph.deleteLater()

    # ---------- 「?」说明开关 ----------
    # 哪些标签算「注解」：字号 10~11.5px 且是一句话（≥14 字）。
    # 为什么不给每个标签打标记：设置页有 7 个页面、几十处小字，逐个改代码太容易漏；
    # 反过来为什么不能只看字号：`mem_gb`（"16 GB"）、迁移状态（"已完成"）也是小字，
    # 但它们是**值**，藏掉就变成"设置项没有值"。长文本判定把这两类分得很干净。
    _TIP_SIZES = ("font-size:10px", "font-size:10.5px",
                  "font-size:11px", "font-size:11.5px")

    def _tip_labels(self, root):
        out = []
        try:
            labels = root.findChildren(QLabel)
        except Exception:                                # noqa: BLE001
            return out
        for lab in labels:
            try:
                if lab.property("noTip"):
                    continue
                qss = (lab.styleSheet() or "").replace(" ", "").lower()
                if not qss or not any(s in qss for s in self._TIP_SIZES):
                    continue
                if len(str(lab.text() or "")) < 14:
                    continue
                out.append(lab)
            except RuntimeError:
                continue
        return out

    def _reg_tips(self, root):
        for lab in self._tip_labels(root):
            if lab not in self._tips:
                self._tips.append(lab)
            lab.setVisible(self._tips_on)

    def _toggle_tips(self, *_):
        self._tips_on = not self._tips_on
        CONFIG.set("show_tips", self._tips_on)           # 纯显示偏好，不必等「保存」
        self._refresh_help_btn()
        self._apply_tips()

    def _refresh_help_btn(self):
        btn = getattr(self, "btn_help", None)
        if btn is None:
            return
        btn.icon_color = theme.C_ACCENT if self._tips_on else "#7b8794"
        btn.setToolTip("正在显示各页说明，点一下隐藏" if self._tips_on
                       else "各页说明已隐藏，点一下显示")
        btn.update()

    # ---------- 第一百一十六轮：标题栏界面语言切换 ----------
    def _build_lang_switcher(self):
        """「?」左侧放两个小按钮：简体 / 繁体▾（繁体点开下拉台式/港式/澳式）。"""
        from PySide6.QtWidgets import QMenu, QPushButton
        from ..core import i18n as _i18n

        self._lang_btns = {}
        qss = ("QPushButton{background:transparent;border:1px solid rgba(120,135,150,70);"
               "border-radius:8px;padding:2px 7px;color:#4a5b6e;font-size:10px;}"
               "QPushButton:hover{background:rgba(120,150,190,26);}"
               "QPushButton:checked{background:rgba(47,124,246,30);color:#2f7cf6;"
               "border-color:rgba(47,124,246,120);}")

        def mk(label, key):
            b = QPushButton(label)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(qss)
            b.setCheckable(True)
            b.setObjectName("langBtn_" + key)
            if self.head is not None:
                self.head.addWidget(b, 0, Qt.AlignVCenter)
            self._lang_btns[key] = b
            return b

        self.btn_lang_cn = mk("简体", "cn")
        self.btn_lang_tw = mk("繁体 ▾", "trad")

        menu = QMenu(self.btn_lang_tw)
        self._lang_menu = menu
        for lang in _i18n.TRADITIONAL:
            act = menu.addAction(_i18n.LANG_LABELS[lang])
            act.setObjectName("langAct_" + lang)
            act.triggered.connect(lambda _c=False, l=lang: self._on_pick_lang(l))

        def show_menu():
            self.btn_lang_tw.setChecked(True)
            self.btn_lang_cn.setChecked(False)
            pos = self.btn_lang_tw.mapToGlobal(self.btn_lang_tw.rect().bottomLeft())
            menu.exec(pos)

        self.btn_lang_tw.clicked.connect(show_menu)
        self.btn_lang_cn.clicked.connect(lambda _c=False: self._on_pick_lang(_i18n.SIMPLIFIED))
        self._refresh_lang_btns()

    def _refresh_lang_btns(self):
        """按当前语言刷新两个按钮的选中态（跟随模式下按系统判定结果点亮）。"""
        btns = getattr(self, "_lang_btns", None)
        if not btns:
            return
        try:
            from ..core import i18n as _i18n
            lang = _i18n.current_lang()
            btns["cn"].setChecked(lang == _i18n.SIMPLIFIED)
            btns["trad"].setChecked(lang in _i18n.TRADITIONAL)
        except Exception:
            pass

    def _on_pick_lang(self, lang):
        """点简体 / 繁体下拉项：即时刷新全界面。

        第117轮：跟随开启时**不关掉跟随**、不写死手动模式 —— 用临时覆盖
        （temp_lang）让本次会话按所选语言显示，开关保持勾选，下次启动仍
        跟随系统。只有跟随关掉时才走 set_lang 落盘为手动。
        """
        from ..core import i18n as _i18n
        if _i18n.mode() == "follow":
            _i18n.temp_lang(lang)            # 跟随：临时看，不改配置/开关
        else:
            _i18n.set_lang(lang)             # 手动：锁定并落盘
        self._refresh_lang_btns()

    def _on_follow_lang_toggled(self, on):
        """「跟随系统语言」开关：开→跟随系统并即时刷新；关→锁定当前语言。"""
        from ..core import i18n as _i18n
        if on:
            _i18n.follow_system()
        else:
            # 关掉时保持当前正在显示的语言，转手动模式（不跳回简体）
            _i18n.set_mode("manual", _i18n.current_lang())
        self._refresh_lang_btns()

    def _apply_tips(self):
        for lab in list(self._tips):
            try:
                lab.setVisible(self._tips_on)
            except RuntimeError:
                self._tips.remove(lab)                   # 被删掉的旧页标签
        # 行数变了必须重算一次高度：藏了注解后页面变矮，弹窗也跟着收，
        # 否则底下会空一大块（和不藏的时候一样高）。
        self._fit_box()
        self._fit_pages()

    def _build_rest(self):
        """把还没建的页一次建完（保存前必须调：save() 要读每一页的控件）

        第九十八轮：**不只要建页，还要把「摊到后面几帧再填」的内容强制填完** ——
        镜像源的行控件、官启档案列表都是懒填的，取值之前不落地就会漏项
        （`_save_mirror` 拿不到没建出来的行，等于把那些源重置成默认）。
        """
        for i in range(len(self._page_defs)):
            self._build_page(i)
        try:
            self._fill_mirror_all()
        except Exception:                                # noqa: BLE001
            import traceback
            traceback.print_exc()
        try:
            self.startup.flush()
        except Exception:                                # noqa: BLE001
            pass
        self._fit_box()

    def _later(self, fn):
        """把一段重活推到下一帧（页面先出现，内容随后补）。

        为什么不用 `QTimer.singleShot(0, fn)` 直接传：Qt 回调里抛异常等于
        静默失效（打包后没有 traceback），这里自己兜一层。
        """
        def _run():
            try:
                fn()
            except RuntimeError:
                pass                                     # 窗口已经关了
            except Exception:                            # noqa: BLE001
                import traceback
                traceback.print_exc()
        QTimer.singleShot(0, _run)

    def _goto_page(self, i):
        """点左边分类：翻到哪页建哪页（400ms 的页只有玩家真的点开时才会付出）。

        第一百轮：页已建好、点的又是当前页 → 直接返回。以前照样
        setCurrentIndex，触发 showEvent → _ScrollPage.fit() 全页重算，
        快速连点页签就是白白重复算布局（实测 57ms/轮）。
        """
        if i in self._built_pages and self.stack.currentIndex() == i:
            return
        self._build_page(i)
        self.stack.setCurrentIndex(i)

    def _scrolled(self, inner):
        """把单页内容塞进透明滚动区（无边框、6px 细滚动条）"""
        sa = _ScrollPage(inner)
        sa.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollBar:vertical{background:transparent;width:6px;margin:2px 0 2px 2px;}"
            "QScrollBar::handle:vertical{background:rgba(31,39,51,64);"
            "border-radius:3px;min-height:26px;}"
            "QScrollBar::handle:vertical:hover{background:rgba(31,39,51,104);}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
            "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{"
            "background:transparent;}")
        return sa

    def _fit_box(self):
        """按「最高的一页」+ 主窗口高度定设置窗高度。

        原来写死 580：在 740 高的主窗口里设置窗总高 705（上下都快顶到窗口边），
        窗口一缩小（最小 660）就直接比窗口还高、上下探出窗外；而内容其实只要
        547（幻灯片开着）—— 底下白白空一块。现在两头都收敛：
        高度取「实测内容高」和「主窗口能给的」里的小的那个，超了改由内容滚动。
        """
        if not hasattr(self, "_box"):
            return
        need = self._needed_height()
        if len(self._built_pages) < len(self._page_defs):
            # 懒建：还没建完的那几页没参与测量。这里先按原来的固定高度摆着，
            # 否则只量「常规」页会把窗定矮，后面每建一页就长一截、看起来在抽动。
            need = max(need, 560)
        # 上限 700：常规页从第五十四轮起多了「自动补全」一行，620 装不下（实测 685），
        # 固定 620 会让最后两行（幻灯片 / 背景图）永远要靠滚动才看得到。
        # 700 只在「窗口真的有这么高」时才生效（下面还会再按父窗高度收一次），
        # 屏幕小的机器照样是滚动，不会出现「弹窗比窗口还高」。
        cap = 700
        par = self.parentWidget()
        if par is not None and par.isVisible():
            # 125 = 标题 + body 上下边距 + 间距；再留 12 给窗口边
            cap = min(cap, max(320, par.height() - 137))
        hgt = int(min(max(320, need), cap))
        if self._box.height() != hgt:
            self._box.setFixedHeight(hgt)
        self._fit_pages()
        self.adjustSize()

    def _content_width(self):
        """右侧内容区可用宽度：总宽 - body 左右边距 - 左侧导航 - 间距 - 滚动条"""
        return max(240, self.width() - 52 - 146 - 16 - 8)

    def _needed_height(self):
        """最高那一页需要的高度（按当前内容区宽度算）"""
        cw = self._content_width()
        best = 0
        for i in range(self.stack.count()):
            wd = self.stack.widget(i)
            inner = getattr(wd, "_inner", wd)
            lay = inner.layout()
            if lay is None:
                continue
            hh = inner.heightForWidth(cw)
            if hh <= 0:
                hh = lay.sizeHint().height()
            best = max(best, int(hh))
        return best or 320

    def _fit_pages(self):
        """内容行数变了（幻灯片开关）就重新对齐一次高度，否则末尾那行会被裁掉"""
        for i in range(self.stack.count()):
            w = self.stack.widget(i)
            if isinstance(w, _ScrollPage):
                w.fit()

    def _page(self, title, desc=""):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 2, 8, 2)
        lay.setSpacing(8)
        t = QLabel(title)
        tf = QFont(theme.FONT)
        tf.setPixelSize(15)
        tf.setBold(True)
        t.setFont(tf)
        lay.addWidget(t)
        if desc:
            d = QLabel(desc)
            d.setStyleSheet(_PAGE_TIP)
            d.setWordWrap(True)
            lay.addWidget(d)
        return w, lay

    def _field(self, lay, label, placeholder, value="", picker=None, tip=None):
        lab = QLabel(label)
        lab.setStyleSheet("color:#1f2733;font-size:12px;font-weight:600;")
        lay.addWidget(lab)
        row = QHBoxLayout()
        row.setSpacing(8)
        ed = QLineEdit()
        ed.setPlaceholderText(placeholder)
        ed.setFixedHeight(34)
        ed.setText(value)
        row.addWidget(ed, 1)
        if picker:
            br = IconButton("folder", 34, 16)
            br.clicked.connect(picker)
            row.addWidget(br)
        lay.addLayout(row)
        if tip:
            tl = QLabel(tip)
            tl.setStyleSheet(_PAGE_TIP)
            tl.setWordWrap(True)
            lay.addWidget(tl)
        return ed

    def _page_general(self):
        w, lay = self._page("常规", "基础配置项。下载与导入的内容会自动安排到官方启动器。")
        info = QLabel("内容安装去向：\n"
                      "模组 → mods · 光影 → shaderpacks · 材质包 → resourcepacks\n"
                      "整合包 → 独立实例目录 + 自动注册到官方启动器档案")
        info.setStyleSheet("color:#3a4757;font-size:12px;background:rgba(31,39,51,10);"
                           "border-radius:10px;padding:12px;")
        info.setWordWrap(True)
        lay.addWidget(info)

        # 第一百一十六轮：界面语言。开关开着 = 跟随 Windows 系统语言
        # （简体系统→简体，繁体系统→对应繁体，其它→简体）；关掉 = 用标题栏
        # 手动选的简体 / 台式 / 港式 / 澳式。切换即时生效，不等「保存」。
        from ..core import i18n as _i18n
        self.sw_follow_lang = self._switch_row(
            lay, "跟随系统语言",
            "默认开启：按 Windows 系统语言自动切换界面（现支持简体 / 繁体，"
            "繁体再分台式、港式、澳式）。开启时点右上角「简体 / 繁体▾」只临时切换本次显示，"
            "不影响跟随；关闭后才会把所选语言固定下来。",
            _i18n.mode() == "follow")
        self.sw_follow_lang.toggled.connect(self._on_follow_lang_toggled)

        # 独立启动：关掉时主页/侧栏的启动按钮连小三角都不显示（见 launchbtn.py）
        from ..core import gamelaunch
        self.sw_standalone = self._switch_row(
            lay, "独立启动",
            "开启后启动按钮直接就是「独立启动」：点一下就拉起游戏，不经过官方启动器"
            "（想临时回官方启动器，点按钮右侧小三角切）。\n"
            "第一次用先去「游戏管理」点 ▶ 选一个要启动的游戏。",
            gamelaunch.standalone_enabled())

        # 自动导入：默认关 —— 关着时「添加已有文件夹」只登记不复制，可直接运行
        from ..core import importer
        self.sw_auto_import = self._switch_row(
            lay, "自动导入",
            "开启后，在「导入游戏」里添加已有文件夹时会立刻把它搬进官方启动器（要复制文件）。\n"
            "关闭时只登记：在「游戏管理 → 已导入」点「运行」，用原目录启动，不占额外空间。",
            importer.auto_import_enabled())

        # 自动补全（第五十四轮）：默认开 —— 装完照着实例里的 manifest.json 核一遍，
        # 清单上写着却没下下来的模组按记录重新拉，别等进了游戏才发现少东西
        from ..core import packfill as _pf
        self.sw_auto_complete = self._switch_row(
            lay, "自动补全",
            "装完整合包自动解析 manifest.json，照清单核对大小与 sha1，"
            "把缺的文件重新下回来。\n"
            "关掉只是不再自动跑，「游戏目录 → 补全整合包文件」仍可手动补。",
            _pf.enabled())

        # 自动中文（第五十五轮）：默认开 —— 启动前把 options.txt 的 lang 改成
        # zh_cn，整合包 / 别人拷来的实例不用再进游戏点一遍语言。
        from ..core import langfix as _lf
        self.sw_auto_chinese = self._switch_row(
            lay, "自动中文",
            "每次启动游戏前，把游戏目录里 options.txt 的语言设成简体中文。\n"
            "只动 lang 这一行，键位 / 音量 / 难度这些一个都不改。",
            _lf.enabled())

        # 启动切输入法：默认开 —— 开局前把输入语言切成英文，
        # 中文输入法不会吃掉 WASD / 在聊天框里蹦候选词。
        # 游戏退出后自动切回（不写注册表，也不永久改玩家的默认语言）
        from ..core import ime as _ime
        self.sw_ime = self._switch_row(
            lay, "启动切输入法",
            "每次启动游戏前把输入语言切成英文（en-US），"
            "免得中文输入法吃掉 WASD、或者在游戏聊天框里蹦候选词。\n"
            "游戏退出后自动切回你启动前用的输入法；不写注册表、不永久改默认语言。",
            bool(CONFIG.get(_ime.ENABLED_KEY, True)))

        # 关窗弹窗：默认关 —— 点 × 直接全部退出（第四十五轮）。
        # 开着的时候 x 会问「最小化到托盘 / 退出」，关掉就一步到底。
        self.sw_close_confirm = self._switch_row(
            lay, "关闭弹窗",
            "开启后点 × 先问「最小化到托盘 / 直接退出」；关闭（默认）时点 × 直接退出。",
            bool(CONFIG.get("close_confirm", False)))

        # 内置文件管理器（第六十轮）：默认开。开着时「选目录 / 选 java.exe /
        # 选保存位置 / 装完查看文件夹」全走软件自己画的窗口；关掉就全交给
        # Windows 的系统对话框（有人习惯直接在地址栏粘 %APPDATA%）。
        self.sw_builtin_fs = self._switch_row(
            lay, "文件管理器",
            "开启后用启动器自带的文件管理器选文件夹、选 java.exe、查看下载去向 —— "
            "样式跟软件一致，也拖不出主窗口。\n"
            "关闭后这些都改用 Windows 的系统文件对话框。",
            bool(CONFIG.get("builtin_fs", True)))

        # 引导教程（第八十轮）：默认开。新装第一次进主界面自动走一遍，
        # 逐项点亮「浏览整合包 / 顶部页签 / 游戏管理」这些入口。
        # 关掉之后不再自动出现；重新打开 = 立刻再走一遍（tour.reset() 把「已看过」清掉）。
        self.sw_tour = self._switch_row(
            lay, "引导教程",
            "开启后第一次进入主界面会自动走一遍引导：主页 5 项、顶部页签 5 项、"
            "游戏管理 4 项，一步步高亮并说明作用。\n"
            "走完自动记住；关掉不再出现，重新打开会再走一遍。",
            bool(CONFIG.get("tour_enabled", True)))

        # 卡片隐藏：整个内容区（含右侧启动选择抽屉）停手 N 秒后自动隐去，顶部状态栏不动
        self._card_secs = int(CONFIG.get("card_hide_secs") or 3)
        self.sw_card_hide = self._switch_row(
            lay, "卡片隐藏",
            "开启后，鼠标在内容区不动、或者停在顶部状态栏 / 移出窗口，到所选秒数整个内容区"
            "自动隐去（顶部状态栏不受影响）。\n"
            "鼠标动一动不会把它叫回来，点一下窗口才会重新出现。",
            bool(CONFIG.get("card_auto_hide")))
        self.w_card_secs = self._secs_row(lay, self._card_secs)
        self.w_card_secs.setVisible(bool(CONFIG.get("card_auto_hide")))
        self.sw_card_hide.toggled.connect(self.w_card_secs.setVisible)

        # 背景幻灯片：默认开（每 N 秒淡入淡出换一张）；关掉就从九张里挑一张固定用。
        # 两种情况下天空都照现实时间变化 —— 调色在 wallpaper.py 里做。
        from . import wallpaper as _wp
        names = _wp.available()
        self._bg_names_cache = names
        self._bg_pick = int(CONFIG.get("bg_pick") or 0)
        if names:
            self._bg_pick = max(0, min(self._bg_pick, len(names) - 1))
        self._slide_secs = int(CONFIG.get("slide_secs") or 15)
        slide_on = bool(CONFIG.get("slideshow", True))
        self.sw_slide = self._switch_row(
            lay, "幻灯片",
            "开启后每隔一段时间自动换一张背景图（淡入淡出）。"
            "关掉就从下面挑一张固定用 —— 天空照旧跟着现实时间变。",
            slide_on)
        self.w_slide_secs = self._slide_secs_row(lay, self._slide_secs)
        self.w_bg_pick = self._bg_pick_row(lay, self._bg_pick)
        self.w_slide_secs.setVisible(slide_on and bool(names))
        self.w_bg_pick.setVisible((not slide_on) and bool(names))
        self.sw_slide.toggled.connect(self._on_slide_toggle)

        lay.addStretch(1)
        return w

    # ------------------------------------------------------------ 幻灯片设置
    def _on_slide_toggle(self, on):
        has = bool(self._bg_names())
        self.w_slide_secs.setVisible(bool(on) and has)
        self.w_bg_pick.setVisible((not on) and has)
        # 换行会把常规页撑高 39px：先重算一次高度，别让「背景图」那行落在窗底外
        self._fit_box()
        self._fit_pages()

    def _bg_names(self):
        from . import wallpaper as _wp
        names = getattr(self, "_bg_names_cache", None)
        if names is None:
            names = _wp.available()
            self._bg_names_cache = names
        return names

    def _slide_secs_row(self, lay, cur):
        """「切换间隔」：10s / 15s / 30s / 60s 四个胶囊，选中实心"""
        box = QWidget()
        box.setAttribute(Qt.WA_StyledBackground, True)
        box.setObjectName("slideBox")
        box.setStyleSheet("#slideBox{background:rgba(31,39,51,8);border-radius:9px;}")
        h = QHBoxLayout(box)
        h.setContentsMargins(14, 9, 14, 9)
        h.setSpacing(8)
        lb = QLabel("切换间隔")
        lb.setStyleSheet("color:#4c5866;font-size:11.5px;font-weight:600;"
                         "background:transparent;")
        h.addWidget(lb)
        self._slide_btns = {}
        for s in (10, 15, 30, 60):
            b = PillButton(f"{s}s", "accent" if s == cur else "ghost")
            b.setFixedHeight(26)
            b.setFixedWidth(50)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, v=s: self._pick_slide_secs(v))
            h.addWidget(b)
            self._slide_btns[s] = b
        h.addStretch(1)
        lay.addWidget(box)
        return box

    def _pick_slide_secs(self, secs):
        self._slide_secs = int(secs)
        for s, b in self._slide_btns.items():
            b.kind = "accent" if s == self._slide_secs else "ghost"
            b.update()

    def _bg_pick_row(self, lay, cur):
        """「背景图」：小预览 + 名字 + 更换（弹出九宫格）"""
        box = QWidget()
        box.setAttribute(Qt.WA_StyledBackground, True)
        box.setObjectName("bgPickBox")
        box.setStyleSheet("#bgPickBox{background:rgba(31,39,51,8);border-radius:9px;}")
        h = QHBoxLayout(box)
        h.setContentsMargins(14, 9, 14, 9)
        h.setSpacing(12)
        self._bg_thumb = QLabel()
        self._bg_thumb.setFixedSize(92, 56)
        self._bg_thumb.setCursor(Qt.PointingHandCursor)
        self._bg_thumb.setStyleSheet("background:transparent;")
        h.addWidget(self._bg_thumb)
        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel("背景图")
        t.setStyleSheet("color:#4c5866;font-size:11.5px;font-weight:600;"
                        "background:transparent;")
        self._bg_label = QLabel("")
        self._bg_label.setStyleSheet("color:#6b7684;font-size:11px;background:transparent;")
        col.addWidget(t)
        col.addWidget(self._bg_label)
        col.addStretch(1)
        h.addLayout(col, 1)
        btn = PillButton("更换", "ghost")
        btn.setFixedHeight(26)
        btn.setFixedWidth(64)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda: self._open_bg_picker(btn))
        h.addWidget(btn, 0, Qt.AlignVCenter)
        lay.addWidget(box)
        self._bg_box = box
        # 第一百轮：首次解码这张 JPG 实测 107ms。先让设置窗口出来，
        # 缩略图下一帧再补 —— 别把「点开设置」卡成一下。
        QTimer.singleShot(0, self._render_bg_thumb)
        return box

    def _render_bg_thumb(self):
        try:
            self._bg_thumb.width()      # 对话框可能已经在下一帧之前关掉了
        except (RuntimeError, AttributeError):
            return
        from . import wallpaper as _wp
        names = self._bg_names()
        if not names:
            self._bg_label.setText("没找到背景图（assets/bg）")
            return
        i = int(self._bg_pick) % len(names)
        self._bg_label.setText(f"第 {i + 1} 张 / 共 {len(names)} 张")
        im = _wp.thumb(names[i], 92, 56)
        pm = QPixmap(92, 56)
        pm.fill(Qt.transparent)
        if im is not None:
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 0, 92, 56), 8, 8)
            p.setClipPath(path)
            p.drawImage(0, 0, im)
            p.end()
        self._bg_thumb.setPixmap(pm)

    def _open_bg_picker(self, anchor):
        if not self._bg_names():
            Toast.show_toast(self.parent() or self, "没有可用的背景图", kind="error")
            return
        dlg = _BgPicker(self, self._bg_pick, self._set_bg_pick)
        dlg.show_at(anchor)

    def _set_bg_pick(self, i):
        self._bg_pick = int(i)
        self._render_bg_thumb()

    def _secs_row(self, lay, cur):
        """「自动隐藏时间」：3s / 5s / 10s / 15s 四个胶囊，选中实心"""
        box = QWidget()
        box.setAttribute(Qt.WA_StyledBackground, True)
        box.setObjectName("secsBox")
        # 必须 #id 限定：写成 QWidget{...} 会级联到里面所有子控件
        box.setStyleSheet("#secsBox{background:rgba(31,39,51,8);border-radius:9px;}")
        h = QHBoxLayout(box)
        h.setContentsMargins(14, 9, 14, 9)
        h.setSpacing(8)
        lb = QLabel("自动隐藏时间")
        lb.setStyleSheet("color:#4c5866;font-size:11.5px;font-weight:600;"
                         "background:transparent;")
        h.addWidget(lb)
        self._secs_btns = {}
        for s in (3, 5, 10, 15):
            b = PillButton(f"{s}s", "accent" if s == cur else "ghost")
            b.setFixedHeight(26)
            b.setFixedWidth(50)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, v=s: self._pick_card_secs(v))
            h.addWidget(b)
            self._secs_btns[s] = b
        h.addStretch(1)
        lay.addWidget(box)
        return box

    def _pick_card_secs(self, secs):
        self._card_secs = int(secs)
        for s, b in self._secs_btns.items():
            b.kind = "accent" if s == self._card_secs else "ghost"
            b.update()

    def _switch_row(self, lay, title, desc, on):
        """一行「标题 + 说明 + 右侧胶囊开关」的设置项"""
        box = QWidget()
        box.setAttribute(Qt.WA_StyledBackground, True)
        box.setStyleSheet("QWidget{background:rgba(31,39,51,12);border-radius:11px;}")
        h = QHBoxLayout(box)
        h.setContentsMargins(14, 11, 14, 11)
        h.setSpacing(12)
        txt = QVBoxLayout()
        txt.setSpacing(3)
        t = QLabel(title)
        tf = QFont(theme.FONT)
        tf.setPixelSize(12)
        tf.setBold(True)
        t.setFont(tf)
        t.setStyleSheet("background:transparent;")
        txt.addWidget(t)
        d = QLabel(desc)
        d.setStyleSheet("color:#5a6675;font-size:11px;background:transparent;")
        d.setWordWrap(True)
        txt.addWidget(d)
        h.addLayout(txt, 1)
        sw = PillSwitch(on)
        h.addWidget(sw, 0, Qt.AlignVCenter)
        lay.addWidget(box)
        return sw

    def _page_paths(self):
        """游戏目录（第四十五轮）：应用路径 / 官方路径 两条并存。

        以前这里只有一条可编辑的「游戏目录」，既当本启动器的地盘又替官方启动器存数据。
        现在分成两条只读路径（安装时定好，动手改目录请用「目录迁移」）：
          · 应用路径 = <安装目录>\\.minecraft —— 纯属于本启动器
          · 官方路径 = <安装目录>\\official_assets\\.minecraft —— 官方启动器用
        """
        from ..core import paths as _paths
        w, lay = self._page(
            "游戏目录",
            "本启动器使用两条独立的游戏目录，互不干扰："
            "「应用」只属于这个启动器（独立启动 / 下载到应用时用），"
            "「官方」交给官方启动器（下载到官方时用，也就是 official_assets 里那一份）。")

        st = _paths.status()
        self._path_dirs = {"app": st["app"], "official": st["official"]}

        # 第九十八轮：总路径（Osmanthus flower 本身）= 应用路径 + 官方路径 +
        # 游戏缓存 + 整合包。第九十九轮玩家明确纠正：**它不含配置 / 数据目录**
        # （账号、设置、日志独立在 %APPDATA%\bgl_datat_config），搬它不动账号。
        # 不在这里直接搬：只记下要搬到哪，真正的搬运交给 core/relocate.py，
        # 只在重启 / 下次重开启动器时落地（点「否」连关窗口都不搬）。
        self._root_now = st["root"]
        self._root_new = ""
        self._root_move = None
        lay.addWidget(self._sub_title(f"总路径（{_paths.INSTALL_DIR_NAME}）"))
        tip_root = QLabel(
            f"应用路径、官方路径、游戏缓存、整合包放在这一个 {_paths.INSTALL_DIR_NAME} "
            "文件夹里；账号、设置和配置 / 数据目录不在这里（独立放在 %APPDATA%），"
            "改总路径不会动它们，账号也不会丢。改它就是这份游戏家当整份搬家（连同文件夹本身）。"
            "保存时会问要不要现在重启：点「是」立刻重启并在重启过程中搬过去；"
            "点「否」什么都不动，路径还是原来那个，关窗口也不搬，只有下次重开启动器时才搬"
            "—— 跨盘的大搬家会在重开时用系统通知告诉你进度。")
        tip_root.setStyleSheet("color:#5a6675;font-size:11px;")
        tip_root.setWordWrap(True)
        lay.addWidget(tip_root)
        rrow = QHBoxLayout()
        rrow.setSpacing(8)
        self.ed_root = QLineEdit(st["root"])
        self.ed_root.setReadOnly(True)
        self.ed_root.setFixedHeight(32)
        self.ed_root.setCursor(Qt.IBeamCursor)
        self.ed_root.setToolTip("只读：点右边「更改」选新的位置")
        rrow.addWidget(self.ed_root, 1)
        btn_root = PillButton("更改", "ghost")
        btn_root.setFixedHeight(32)
        btn_root.setMinimumWidth(64)
        btn_root.setToolTip("选新位置（选父文件夹就行，会自动接上 %s）"
                            % _paths.INSTALL_DIR_NAME)
        btn_root.clicked.connect(self._pick_root)
        rrow.addWidget(btn_root)
        lay.addLayout(rrow)
        self.lb_root_state = QLabel("")
        self.lb_root_state.setStyleSheet("color:#8a95a3;font-size:10.5px;")
        self.lb_root_state.setWordWrap(True)
        lay.addWidget(self.lb_root_state)

        row = self._path_row(lay, "应用路径",
                             "独立启动、下载到「应用」都落在这里。",
                             st["app"])
        row2 = self._path_row(
            lay, "官方路径",
            f"官方启动器用；放在 {_paths.OFFICIAL_DIR} 里，以后迁移只动它。",
            st["official"])

        # 下载目标：跟下载面板 / 游戏管理 / 启动选择里那排胶囊同一份配置，改哪边都一样。
        # 第四十六轮多一个「自动」= 跟随独立启动开关（关=官方，开=应用），也是默认状态。
        from ..core import paths as _p2
        self.ex_target = ChoiceExpander(
            "默认目录",
            [("auto", "自动", "跟随「独立启动」开关：关着用官方，开着用应用"),
             ("official", "官方", f"始终看官方路径：{st['official']}"),
             ("app", "应用", f"始终看应用路径：{st['app']}")],
            _p2.explicit_target() or "auto")
        self.ex_target.setToolTip(
            "下载装到哪、游戏管理和启动选择里列哪条目录，都由它决定；"
            "选「自动」就跟着上面的独立启动开关走")
        lay.addWidget(self.ex_target)

        self._data_dir_row(lay)

        lay.addSpacing(4)
        lay.addWidget(self._sub_title("官方启动器程序"))
        self.ed_launcher = self._field(
            lay, "启动器程序路径",
            r"C:\Program Files (x86)\Minecraft Launcher\MinecraftLauncher.exe",
            CONFIG.get("launcher_path") or "", picker=self._pick_launcher)

        # 整合包档案自检：修 lastVersionId 为空 / 实例目录错位 / 缺运行版本
        tip = QLabel("官启报「REQUEST_FAILED」时点「修复整合包档案」：重绑运行版本、"
                     "归位实例目录、复用已装版本。\n"
                     "整合包里少了模组时点「补全整合包文件」：照 manifest.json 核对并重下。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#5a6675;font-size:11px;margin-top:6px;")
        lay.addWidget(tip)
        row3 = QHBoxLayout()
        row3.setSpacing(8)
        self.btn_repair = PillButton("修复整合包档案", "ghost")
        self.btn_repair.setFixedHeight(32)
        self.btn_repair.setMinimumWidth(120)
        self.btn_repair.clicked.connect(self._do_repair_profiles)
        row3.addWidget(self.btn_repair)
        # 第五十四轮：照实例里的 manifest.json 核对一遍，缺的文件重新下回来
        self.btn_fill = PillButton("补全整合包文件", "ghost")
        self.btn_fill.setFixedHeight(32)
        self.btn_fill.setMinimumWidth(120)
        self.btn_fill.setToolTip(
            "装完发现少了模组、或者从别处拷来的整合包不完整时点这里：\n"
            "照实例目录里的 manifest.json 核对大小 / sha1，缺的按记录重新下载。")
        self.btn_fill.clicked.connect(self._do_fill_packs)
        row3.addWidget(self.btn_fill)
        row3.addStretch(1)
        lay.addLayout(row3)
        lay.addStretch(1)
        return w

    def _pick_root(self):
        """选新的总路径位置（第九十八轮）

        选父文件夹就行 —— `relocate.new_root_for` 会自动接上 Osmanthus flower；
        玩家真的一路点进 `Osmanthus flower` 里面选，也照样认。
        """
        from .fspicker import pick_dir
        from ..core import paths as _p
        from ..core import relocate as _rl
        start = os.path.dirname(self._root_now) or os.path.expanduser("~")
        d = pick_dir(self, "选择 %s 放到哪里" % _p.INSTALL_DIR_NAME, start)
        if not d:
            return
        new = _rl.new_root_for(d)
        if os.path.normcase(new) == os.path.normcase(self._root_now):
            self._root_new = ""
            self.ed_root.setText(self._root_now)
            self.lb_root_state.setText("还是现在这个位置，没变")
            return
        self._root_new = new
        self.ed_root.setText(new)
        self.lb_root_state.setText(
            f"保存后会把整份 {_p.INSTALL_DIR_NAME} 搬到 {new}（会弹窗问你要不要重启）")
        try:
            self._fit_pages()
        except Exception:                                # noqa: BLE001
            pass

    def _sub_title(self, text):
        lb = QLabel(text)
        f = QFont(theme.FONT)
        f.setPixelSize(13)
        f.setBold(True)
        lb.setFont(f)
        lb.setStyleSheet("color:#2a3442;margin-top:2px;")
        return lb

    def _path_row(self, lay, title, desc, path):
        """一行只读路径：标题 + 说明 + 可选中复制的路径框 + 打开按钮"""
        lay.addWidget(self._sub_title(title))
        d = QLabel(desc)
        d.setStyleSheet("color:#5a6675;font-size:11px;")
        d.setWordWrap(True)
        lay.addWidget(d)
        row = QHBoxLayout()
        row.setSpacing(8)
        ed = QLineEdit(path)
        ed.setReadOnly(True)
        ed.setFixedHeight(32)
        ed.setCursor(Qt.IBeamCursor)          # 只读但允许选中复制
        ed.setToolTip("只读：安装时确定。要换位置请用「目录迁移」。")
        ed.setCursorPosition(0)               # 长路径从盘符开始显示，别一上来就是尾巴
        ed.deselect()
        row.addWidget(ed, 1)
        b = PillButton("打开", "ghost")
        b.setFixedHeight(32)
        b.setMinimumWidth(64)
        b.clicked.connect(lambda: self._open_path(path))
        row.addWidget(b)
        lay.addLayout(row)
        return ed

    def _open_path(self, path):
        try:
            os.makedirs(path, exist_ok=True)
            from .fspicker import pick_view          # 第六十轮：内置优先
            pick_view(self, "文件夹内容", path)
        except Exception as e:                       # noqa: BLE001
            Toast.show_toast(self, f"打不开：{str(e)[:60]}", kind="error")

    # ---------------- 数据 / 配置路径（第五十五轮） ----------------
    def _data_dir_row(self, lay):
        """玩家自己的账号 / 设置 / 缓存 / 日志放哪 —— **可查也可改**。

        和上面两条游戏目录不同：那两条只读（要换位置走「目录迁移」），
        这条是启动器自己的数据，换位置不影响游戏，所以直接给输入框 + 选目录。
        真搬家发生在点「保存」的时候（见 _apply_data_dir），旧目录一个字节都不删。
        """
        from ..core import config as _cfg
        lay.addSpacing(4)
        lay.addWidget(self._sub_title("数据 / 配置路径"))
        d = QLabel("本启动器的账号、设置、缓存与日志都放这里 —— 不是游戏目录。"
                   "C 盘紧张时可以指到别的盘，保存时会把现有数据整体复制过去。")
        d.setWordWrap(True)
        d.setStyleSheet("color:#5a6675;font-size:11px;")
        lay.addWidget(d)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.ed_data = QLineEdit(_cfg.app_data_dir())
        self.ed_data.setFixedHeight(32)
        self.ed_data.setCursorPosition(0)     # 长路径从盘符开始显示
        self.ed_data.setToolTip("直接改也行：填一个目录，点「保存」后生效")
        row.addWidget(self.ed_data, 1)
        b = IconButton("folder", 32, 16)
        b.setToolTip("选一个目录")
        b.clicked.connect(self._pick_data_dir)
        row.addWidget(b)
        self.btn_data_default = PillButton("恢复默认", "ghost")
        self.btn_data_default.setFixedHeight(32)
        self.btn_data_default.setMinimumWidth(80)
        self.btn_data_default.setToolTip(f"改回默认位置：{_cfg.default_data_dir()}")
        self.btn_data_default.clicked.connect(self._reset_data_dir)
        row.addWidget(self.btn_data_default)
        lay.addLayout(row)

    def _pick_data_dir(self):
        from .fspicker import pick_dir
        cur = self.ed_data.text().strip() or os.path.expanduser("~")
        d = pick_dir(self, "选择数据 / 配置目录", cur)
        if d:
            # 玩家选的通常是「D:\某目录」：再套一层数据目录名，别把别人的盘根占满
            from ..core.config import DATA_DIR_NAME
            if os.path.basename(d.rstrip("\\/")).lower() != DATA_DIR_NAME:
                d = os.path.join(d, DATA_DIR_NAME)
            self.ed_data.setText(d)

    def _reset_data_dir(self):
        from ..core import config as _cfg
        self.ed_data.setText(_cfg.default_data_dir())

    def _apply_data_dir(self):
        """保存设置时把数据目录搬过去（成功会顺带更新 CONFIG.path）"""
        from ..core import config as _cfg
        want = (self.ed_data.text() or "").strip()
        cur = _cfg.app_data_dir()
        try:
            same = (not want) or (os.path.normcase(os.path.abspath(
                os.path.expandvars(want))) == os.path.normcase(os.path.abspath(cur)))
        except Exception:
            same = False
        if same:
            return
        ok, msg = _cfg.set_data_root(want)
        if ok:
            Toast.show_toast(self.parent(),
                             "数据目录已搬好（旧目录原样保留，确认没问题后可以自己删）",
                             ms=5000, kind="success")
            self.ed_data.setText(_cfg.app_data_dir())
        else:
            Toast.show_toast(self.parent(), f"数据目录没换成：{msg}", ms=6000,
                             kind="error")
            self.ed_data.setText(cur)

    def _page_startup(self):
        # 标题与说明由 _StartupPage 自己画（它上面还有「全局启动设置」三块）
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 2, 8, 2)
        lay.setSpacing(8)
        self.startup = _StartupPage(self)
        lay.addWidget(self.startup, 1)
        return w

    def _do_repair_profiles(self):
        self.btn_repair.setEnabled(False)
        self.btn_repair.setText("修复中…")

        def reset():
            self.btn_repair.setEnabled(True)
            self.btn_repair.setText("修复整合包档案")

        def done(res):
            reset()
            msgs = [m for m in (res or [])]
            if not msgs:
                Toast.show_toast(self, "没有需要修复的整合包档案", kind="success")
                return
            bad = [m for m in msgs if "仍缺" in m]
            text = msgs[0] if len(msgs) == 1 else f"{msgs[0]}（共 {len(msgs)} 项）"
            Toast.show_toast(self, text, ms=6000,
                             kind="error" if bad else "success")

        def err(e):
            reset()
            Toast.show_toast(self, f"修复失败：{str(e)[:90]}", ms=5000, kind="error")

        run_async(installer.repair_installed_profiles, on_done=done, on_err=err)

    def _do_fill_packs(self):
        """先弹「选哪个整合包」→ 再带进度条补（第一百零九轮）。

        以前点一下就在按钮上转「补全中…」，所有整合包一起跑、没有任何进度：
        玩家既不知道补的是哪个、也不知道补到哪一步。现在拆成两步：
          · `FillPacksDialog` 列出本机能补的包（默认全选，可只挑几个）；
          · 补的过程有进度条 + 阶段文本，结束后停在结果页不自动消失。
        """
        from ..core import packfill
        try:
            packs = packfill.list_packs()
        except Exception as e:                            # noqa: BLE001
            Toast.show_toast(self, f"读不到整合包列表：{str(e)[:80]}",
                             ms=5000, kind="error")
            return
        d = FillPacksDialog(self, packs)
        d.exec()

    # ---------- 下载镜像源 ----------
    def _page_mirror(self):
        from ..core import mirror
        w, lay = self._page(
            "下载镜像源",
            "官方源连不上、或者连得上但响应很慢时自动换国内镜像。默认「自动」即可："
            "先连官方，5 秒没动静就换镜像；某个域名连续慢两次，本次运行就一直用镜像。"
            "点分类标题可以折叠。")
        self.mirror_rows = []

        # 第一百零四轮（D1/D2）：两个**可选非默认**策略。MCIMirror 的 API 代理
        # 已只读实测可用（CDN 代理实测 302 回官方，不接入）；错峰竞速用
        # ThreadPoolExecutor + as_completed，官方先发、约 3 秒后镜像跟上。
        self.cb_mcim = QCheckBox(
            "MCIMirror 模组接口代理（可选，仅 api.modrinth.com）")
        self.cb_mcim.setChecked(bool(mirror.mcim_enabled()))
        self.cb_mcim.setStyleSheet("color:#4c5866;font-size:11px;")
        self.cb_mcim.setToolTip(mirror.MCIM_CDN_NOTE)
        lay.addWidget(self.cb_mcim)
        self.cb_race = QCheckBox("错峰竞速：官方先发、约 3 秒后镜像跟上，谁先好用谁")
        self.cb_race.setChecked(bool(mirror.race_enabled()))
        self.cb_race.setStyleSheet("color:#4c5866;font-size:11px;")
        lay.addWidget(self.cb_race)

        host = QWidget()
        host.setStyleSheet("background:transparent;")
        hl = QVBoxLayout(host)
        hl.setContentsMargins(0, 0, 4, 0)
        hl.setSpacing(6)
        seg = _SourceRow("__default__", "默认策略",
                         "下面没有单独设置过的类别，都跟随这里。",
                         mirror.default_mode(), "各官方源", "国内镜像")
        hl.addWidget(seg)
        self.mirror_default_row = seg
        self.mirror_rows.append(seg)

        # 按 group 分组成展开式（第 52 轮）：游戏本体 / 加载器 / 模组平台。
        # 模组平台那几项国内没有镜像，行内只会显示一个「官方」。
        buckets = {}
        for s in mirror.SOURCES:
            buckets.setdefault(s.get("group") or "其它", []).append(s)
        order = [g for g in getattr(mirror, "GROUPS", ()) if g in buckets]
        order += [g for g in buckets if g not in order]
        # 第九十八轮：11 个源每个行控件 ~19ms（一行里 3 个按钮 + 3 条 label，
        # 各自 setStyleSheet 都要重新 polished 一遍），整页实测 303ms，
        # 玩家点「下载镜像源」卡的就是这一下。现在分组壳先摆出来（79ms），
        # 行控件下一帧起一组一组补 —— 页面立刻出现，内容随后填上。
        # 取值逻辑没动：保存前 `_build_rest()` 会把没补的强制补完。
        self._mirror_jobs = []
        for gname in order:
            box = _SourceGroup(gname, f"{len(buckets[gname])} 项")
            hl.addWidget(box)
            self._mirror_jobs.append((box, list(buckets[gname])))
        hl.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        scroll.setWidget(host)
        lay.addWidget(scroll, 1)

        # 两个「调完就想试」的旋钮放滚动区外面固定显示（第 52 轮）：
        # 跟着镜像列表一起滚很容易被忽略，而它们直接影响下一分钟的下载速度。
        self._add_dl_limits(lay)

        self.lb_mirror_state = QLabel(mirror.speed_hint() or
                                      "镜像：BMCLAPI（bangbang93）")
        self.lb_mirror_state.setStyleSheet(_PAGE_TIP)
        self.lb_mirror_state.setWordWrap(True)
        lay.addWidget(self.lb_mirror_state)
        # 第一组当场就建（这一组才 3~5 行，几十毫秒），页面一出来就有内容，
        # 不会闪一下空白；后面几组再摊到后续帧。
        try:
            if self._mirror_jobs:
                _box, _items = self._mirror_jobs.pop(0)
                self._fill_mirror_group(_box, _items)
        except Exception:                                # noqa: BLE001
            pass
        self._later(self._fill_mirror_step)     # 剩下的行控件摊到后面的帧里建
        return w

    def _fill_mirror_step(self):
        """补一组的行控件，然后把自己排到下一帧（见 `_page_mirror`）"""
        jobs = getattr(self, "_mirror_jobs", None)
        if not jobs:
            return
        box, items = jobs.pop(0)
        self._fill_mirror_group(box, items)
        if jobs:
            self._later(self._fill_mirror_step)
        else:
            self._fit_box()

    def _fill_mirror_group(self, box, items):
        from ..core import mirror
        for s in items:
            try:
                row = _SourceRow(s["key"], s["name"], s["desc"],
                                 mirror.mode_for(s["key"]),
                                 mirror.official_host(s["key"]),
                                 mirror.mirror_host(s["key"]))
            except Exception:                              # noqa: BLE001
                continue
            box.add(row)
            self.mirror_rows.append(row)

    def _fill_mirror_all(self):
        """把还没建的行一次建完（保存前必须调，否则 `_save_mirror` 会漏项）"""
        jobs = getattr(self, "_mirror_jobs", None) or []
        self._mirror_jobs = []
        for box, items in jobs:
            self._fill_mirror_group(box, items)

    def _add_dl_limits(self, lay):
        """最大线程数 + 速度限制两条拖条（第 52 轮）。

        线程数指的是**总连接预算**（和 PCL / PCLCE 的语义一致），不是
        「同时下几个文件」：文件多时铺满，文件少时把预算让给单个大文件多切
        几段。所以拉到 64 不会变成 64×16=1024 条连接去打 CDN。
        """
        from PySide6.QtWidgets import QSlider
        ss = (
            "QSlider::groove:horizontal{height:6px;background:rgba(31,39,51,36);border-radius:3px;}"
            "QSlider::sub-page:horizontal{background:#2f7cf6;border-radius:3px;}"
            "QSlider::handle:horizontal{width:16px;height:16px;margin:-5px 0;"
            "border-radius:8px;background:#ffffff;border:2px solid #2f7cf6;}")

        box = QWidget()
        box.setObjectName("dlLim")
        box.setAttribute(Qt.WA_StyledBackground, True)
        # 铁律：只写 #objectName，绝不写裸 QWidget{background} —— 否则级联
        # 命中行里的 QLabel 并撑爆高度。
        box.setStyleSheet(
            "#dlLim{background:rgba(255,255,255,0.72);"
            "border:1px solid rgba(120,150,190,0.22);border-radius:10px;}")
        v = QVBoxLayout(box)
        v.setContentsMargins(14, 10, 14, 9)
        v.setSpacing(5)

        def make_row(title, lo, hi, cur, fmt):
            row = QWidget()
            row.setStyleSheet("background:transparent;")
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(10)
            t = QLabel(title)
            t.setFixedWidth(58)
            t.setStyleSheet("color:#3b4756;font-size:12px;background:transparent;")
            s = QSlider(Qt.Horizontal)
            s.setRange(lo, hi)
            s.setValue(max(lo, min(hi, cur)))
            s.setStyleSheet(ss)
            val = QLabel("")
            val.setFixedWidth(78)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val.setStyleSheet("color:#2f7cf6;font-size:12px;background:transparent;")
            h.addWidget(t)
            h.addWidget(s, 1)
            h.addWidget(val)
            v.addWidget(row)

            def upd(_x=None):
                val.setText(fmt(s.value()))
            s.valueChanged.connect(upd)
            upd()
            return s

        self.sl_dlthreads = make_row(
            "最大线程", 1, 128,
            int(CONFIG.get("dl_threads") or 64),
            lambda n: f"{n} 条")
        # 限速拖条走「档位」：0~2048 档 × 25KB/s = 0 ~ 50MB/s。
        # 低端 25KB/s 一档足够细（调试时想压到 100KB/s 也顺手），
        # 高端 50MB/s 已经远超家用带宽。
        self.sl_dlspeed = make_row(
            "速度限制", 0, 2048,
            int(CONFIG.get("dl_speed_kb") or 0) // 25,
            lambda n: "不限" if n <= 0 else
                      (f"{n * 25 / 1024:.1f} MB/s" if n * 25 >= 1024 else f"{n * 25} KB/s"))

        tip = QLabel("线程数是总连接预算：文件多时铺满，文件少时给单个大文件多切几段；"
                     "速度限制管全局总带宽，0 为不限。保存后下一次下载生效。")
        tip.setStyleSheet(_PAGE_TIP)
        tip.setWordWrap(True)
        v.addWidget(tip)
        lay.addWidget(box)
        self._dl_lim_box = box

    def _save_mirror(self):
        from ..core import loader, mirror
        if not getattr(self, "mirror_rows", None):
            return
        items = {r.key: r.value() for r in self.mirror_rows if r.key != "__default__"}
        mirror.save_settings(default=self.mirror_default_row.value(), items=items)
        if getattr(self, "cb_mcim", None) is not None:
            mirror.set_mcim_enabled(self.cb_mcim.isChecked())
        if getattr(self, "cb_race", None) is not None:
            mirror.set_race_enabled(self.cb_race.isChecked())
        loader.clear_cache()
        # 下载旋钮一起落盘（第 52 轮）
        if getattr(self, "sl_dlthreads", None) is not None:
            CONFIG.set("dl_threads", int(self.sl_dlthreads.value()))
        if getattr(self, "sl_dlspeed", None) is not None:
            CONFIG.set("dl_speed_kb", int(self.sl_dlspeed.value()) * 25)

    # ---------- 运行内存 ----------
    def _page_memory(self):
        from PySide6.QtWidgets import QCheckBox, QSlider, QSpinBox
        from ..core import launchprefs as lp
        w, lay = self._page(
            "运行内存",
            "分配给 Minecraft 的最大内存。保存后写入本软件创建的官启档案（JVM 参数 "
            "-Xms / -Xmx），并从「独立启动」直接开局时生效。")

        # ---- 分配方式（第四十四轮）：默认「自动配置」 ----
        self.ex_mem = ChoiceExpander(
            "内存分配方式", [(k, n, d) for k, n, d in lp.MEMORY_MODES], lp.memory_mode())
        self.ex_mem.changed.connect(lambda _k: self._sync_mem_mode())
        lay.addWidget(self.ex_mem)

        total = installer.total_ram_gb()
        max_mb = int(min(65536, max(2048, (total - 2) * 1024)))
        cur = int(CONFIG.get("memory_mb") or 0)
        if cur <= 0:
            # 第六十七轮：自定义的默认值也跟着 4G 起步（跟「自动配置」的下限对齐）
            cur = int(min(8192, max(4096, total * 1024 // 2)))
        cur = max(1024, min(cur, max_mb))

        val_row = QHBoxLayout()
        val_row.setSpacing(10)
        self.mem_big = QLabel("")
        mf = QFont(theme.FONT)
        mf.setPixelSize(26)
        mf.setBold(True)
        self.mem_big.setFont(mf)
        self.mem_big.setStyleSheet("color:#2f7cf6;")
        val_row.addWidget(self.mem_big)
        self._mem_hint_base = (
            f"本机物理内存 {total} GB · 自动配置最少 4 GB，"
            f"建议不超过 {max(2048, total * 1024 // 2) // 1024} GB"
            f"（留下空间给系统与其它程序）")
        self.mem_hint = QLabel(self._mem_hint_base)
        self.mem_hint.setStyleSheet("color:#8a95a3;font-size:11px;")
        self.mem_hint.setWordWrap(True)
        val_row.addWidget(self.mem_hint, 1)
        lay.addLayout(val_row)

        self.mem_slider = QSlider(Qt.Horizontal)
        self.mem_slider.setRange(1024, max_mb)
        self.mem_slider.setSingleStep(256)
        self.mem_slider.setPageStep(1024)
        self.mem_slider.setValue(cur)
        self._sl_style = (
            "QSlider::groove:horizontal{height:6px;background:rgba(31,39,51,36);border-radius:3px;}"
            "QSlider::sub-page:horizontal{background:#2f7cf6;border-radius:3px;}"
            "QSlider::handle:horizontal{width:16px;height:16px;margin:-5px 0;"
            "border-radius:8px;background:#ffffff;border:2px solid #2f7cf6;}")
        # 自动配置时滑块要「看得出不能用」：禁用态系统会压暗轨道，但 handle 仍需换灰
        self._sl_style_off = (
            "QSlider::groove:horizontal{height:6px;background:rgba(31,39,51,20);border-radius:3px;}"
            "QSlider::sub-page:horizontal{background:rgba(31,39,51,40);border-radius:3px;}"
            "QSlider::handle:horizontal{width:16px;height:16px;margin:-5px 0;"
            "border-radius:8px;background:#eef1f5;border:2px solid #c2cad4;}")
        self.mem_slider.setStyleSheet(self._sl_style)
        lay.addWidget(self.mem_slider)

        inrow = QHBoxLayout()
        inrow.setSpacing(8)
        lb = QLabel("手动输入")
        lb.setStyleSheet("color:#1f2733;font-size:12px;font-weight:600;")
        inrow.addWidget(lb)
        self.mem_spin = QSpinBox()
        self.mem_spin.setRange(1024, max_mb)
        self.mem_spin.setSingleStep(256)
        self.mem_spin.setSuffix(" MB")
        self.mem_spin.setValue(cur)
        self.mem_spin.setFixedHeight(32)
        self.mem_spin.setFixedWidth(140)
        self._spin_style = (
            "QSpinBox{background:rgba(255,255,255,190);border:1px solid rgba(31,39,51,45);"
            "border-radius:8px;padding:0 8px;font-size:12px;color:#2a3442;}"
            "QSpinBox:focus{border:1px solid rgba(47,124,246,160);}"
            "QSpinBox::up-button,QSpinBox::down-button{width:0;border:none;}")
        self._spin_style_off = (
            "QSpinBox{background:rgba(31,39,51,12);border:1px solid rgba(31,39,51,22);"
            "border-radius:8px;padding:0 8px;font-size:12px;color:#9aa4b0;}"
            "QSpinBox::up-button,QSpinBox::down-button{width:0;border:none;}")
        self.mem_spin.setStyleSheet(self._spin_style)
        inrow.addWidget(self.mem_spin)
        self.mem_gb = QLabel("")
        self.mem_gb.setStyleSheet("color:#5a6675;font-size:11px;")
        inrow.addWidget(self.mem_gb)
        inrow.addStretch(1)
        lay.addLayout(inrow)

        self.cb_xms = QCheckBox("同时把最小内存固定为一半（-Xms），减少启动卡顿")
        self.cb_xms.setChecked(bool(CONFIG.get("memory_auto_min", True)))
        self.cb_xms.setStyleSheet("color:#4c5866;font-size:12px;")
        lay.addWidget(self.cb_xms)

        # 第一百零四轮（C2）：LockMemory / 独立初始堆 / 1 秒实时内存条。
        # 定时器只改两行文本，不重绘、不重建控件（第 100 轮设置页性能规则）。
        self.cb_lock = QCheckBox("锁定内存（Xms=Xmx，利 ZGC；开启后不再单独加 -Xms / -Xmn）")
        self.cb_lock.setChecked(bool(CONFIG.get("ram_lock_memory", False)))
        self.cb_lock.setStyleSheet("color:#4c5866;font-size:11px;")
        lay.addWidget(self.cb_lock)
        initrow = QHBoxLayout()
        initrow.setSpacing(8)
        lb_init = QLabel("独立初始堆")
        lb_init.setStyleSheet("color:#3b4756;font-size:11px;")
        initrow.addWidget(lb_init)
        self.sp_init = QSpinBox()
        self.sp_init.setRange(0, 65536)
        self.sp_init.setSingleStep(256)
        self.sp_init.setSuffix(" MB（0 = Xmx 的一半）")
        self.sp_init.setValue(int(CONFIG.get("ram_init_mb", 0) or 0))
        self.sp_init.setFixedHeight(28)
        self.sp_init.setFixedWidth(190)
        initrow.addWidget(self.sp_init)
        initrow.addStretch(1)
        lay.addLayout(initrow)
        self.lb_ram_live = QLabel("")
        self.lb_ram_live.setStyleSheet("color:#8a95a3;font-size:10.5px;")
        lay.addWidget(self.lb_ram_live)
        from ..core import ram as _ram

        def _ram_tick(check=True):
            if check and not w.isVisible():
                self._ram_timer.stop()
                return
            st = _ram.stats()
            cur = int(self.mem_spin.value())
            over = cur > max(2048, int((st.get("avail_mb") or 0) * 0.8))
            self.lb_ram_live.setText(
                ("⚠ 分配过高，可能拖慢系统 · " if over else "")
                + f"本机剩余 {(st.get('avail_mb') or 0) / 1024:.1f} GB · 每秒刷新")
        self._ram_timer = QTimer(w)
        self._ram_timer.setInterval(1000)
        self._ram_timer.timeout.connect(lambda: _ram_tick(True))
        _ram_tick(False)
        self._ram_timer.start()

        warn = QLabel("提示：分配过大反而会拖慢启动（垃圾回收压力大）。整合包一般 4~8 GB 足够。")
        warn.setStyleSheet("color:#8a95a3;font-size:11px;")
        warn.setWordWrap(True)
        lay.addWidget(warn)
        lay.addStretch(1)

        def sync_from_slider(v):
            self.mem_spin.blockSignals(True)
            self.mem_spin.setValue(int(v))
            self.mem_spin.blockSignals(False)
            self._mem_label(v)

        def sync_from_spin(v):
            self.mem_slider.blockSignals(True)
            self.mem_slider.setValue(int(v))
            self.mem_slider.blockSignals(False)
            self._mem_label(v)

        self.mem_slider.valueChanged.connect(sync_from_slider)
        self.mem_spin.valueChanged.connect(sync_from_spin)
        self._mem_label(cur)
        self._sync_mem_mode()       # 按当前方式决定滑块亮/灰
        return w

    def _auto_mem(self):
        """按「模组数量 + 剩余内存」算一个推荐值（与启动时用的同一套算法）"""
        from ..core import ram
        gd = installer.detect_game_dir()
        mods = ram.count_mods(os.path.join(gd, "mods"))
        return ram.auto_alloc(mods)

    def _sync_mem_mode(self):
        """自动配置：滑块与输入框置灰、显示算出来的值；自定义：恢复可编辑"""
        auto = self.ex_mem.value() == "auto"
        if auto:
            mb, why = self._auto_mem()
            for wd in (self.mem_slider, self.mem_spin):
                wd.blockSignals(True)
                wd.setValue(int(mb))
                wd.blockSignals(False)
            self._mem_label(mb)
            self.mem_hint.setText(why + "（自动配置）")
        else:
            self.mem_hint.setText(self._mem_hint_base)
        self.mem_slider.setEnabled(not auto)
        self.mem_spin.setEnabled(not auto)
        self.mem_slider.setStyleSheet(self._sl_style_off if auto else self._sl_style)
        self.mem_spin.setStyleSheet(self._spin_style_off if auto else self._spin_style)

    def _mem_label(self, mb):
        mb = int(mb)
        self.mem_big.setText(f"{mb / 1024:.1f} GB".replace(".0 GB", " GB"))
        self.mem_gb.setText(f"→ 官启 -Xmx{mb}M")

    def _page_msa(self):
        w, lay = self._page("微软登录", "内置公共 Client ID 可直接登录，一般无需修改。")
        self.ed_msa = self._field(
            lay, "微软登录 Client ID（可选，留空使用内置公共 ID）",
            "00000000-0000-0000-0000-000000000000",
            CONFIG.get("msa_client_id") or "")
        self.ed_msa.setToolTip(
            "内置 ID 已可直接登录。若想用自己的：\n"
            "1. 打开 portal.azure.com 登录微软账户 → 搜索“应用注册” → 新注册\n"
            "2. 受支持的账户类型选“个人 Microsoft 账户”，重定向 URI 留空\n"
            "3. 进入应用 → 身份验证 → 底部“高级设置”里把\n"
            "    “允许公共客户端流”设为“是” → 保存\n"
            "4. 复制“应用程序(客户端) ID”填到这里")
        lay.addStretch(1)
        return w

    def _page_migrate(self):
        w, lay = self._page(
            "目录迁移（mklink）",
            "把 .minecraft 数据搬到其他盘，原位置留目录联接：官方启动器照常可用，C 盘不再占用游戏空间。")
        self.mk_state = QLabel("")
        self.mk_state.setStyleSheet("color:#5a6675;font-size:11px;")
        self.mk_state.setWordWrap(True)
        lay.addWidget(self.mk_state)
        self._refresh_mk_state()

        mkrow = QHBoxLayout()
        mkrow.setSpacing(8)
        self.ed_mk_dst = QLineEdit()
        self.ed_mk_dst.setPlaceholderText(r"目标盘目录，例如 D:\MinecraftData")
        self.ed_mk_dst.setFixedHeight(34)
        self.ed_mk_dst.textChanged.connect(self._update_mk_cmds)
        mkrow.addWidget(self.ed_mk_dst, 1)
        mkbr = IconButton("folder", 34, 16)
        mkbr.clicked.connect(self._pick_mk_dst)
        mkrow.addWidget(mkbr)
        lay.addLayout(mkrow)

        cmdrow = QHBoxLayout()
        cmdrow.setSpacing(8)
        self.mk_cmds = QLabel("")
        f = QFont(theme.FONT)
        f.setFamily("Consolas")
        f.setPixelSize(9)
        self.mk_cmds.setFont(f)
        self.mk_cmds.setStyleSheet(
            "QLabel{background:rgba(31,39,51,14);color:#33415c;border-radius:8px;padding:8px;}")
        self.mk_cmds.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.mk_cmds.setWordWrap(True)
        cmdrow.addWidget(self.mk_cmds, 1)
        self._update_mk_cmds()
        cmdcol = QVBoxLayout()
        cmdcol.setSpacing(6)
        cp = PillButton("复制命令", "ghost")
        cp.setFixedHeight(30)
        cp.setMinimumWidth(86)
        cp.clicked.connect(self._copy_mk_cmds)
        cmdcol.addWidget(cp)
        go = PillButton("一键迁移", "accent")
        go.setFixedHeight(30)
        go.setMinimumWidth(86)
        go.clicked.connect(self._do_migrate)
        self._btn_migrate = go
        cmdcol.addWidget(go)
        cmdcol.addStretch(1)
        cmdrow.addLayout(cmdcol)
        lay.addLayout(cmdrow)
        lay.addStretch(1)
        return w

    def _pick_launcher(self):
        from .fspicker import pick_file
        p = pick_file(self, "选择官方启动器",
                      os.path.dirname(self.ed_launcher.text().strip() or "C:\\"),
                      ["*.exe"])
        if p:
            self.ed_launcher.setText(p)

    # ---- 游戏目录迁移（mklink）----
    def _refresh_mk_state(self):
        src = self._mk_src
        if self._relink.is_junction(src):
            t = self._relink.link_target(src) or "（外部目录）"
            self.mk_state.setText(f"✓ 已迁移：{src} 是目录联接，数据实际存放在 {t}")
            self.mk_state.setStyleSheet("color:#2e9e5b;font-size:11px;")
        elif os.path.isdir(src):
            self.mk_state.setText(f"○ 未迁移：{src}（真实数据在 C 盘）")
            self.mk_state.setStyleSheet("color:#5a6675;font-size:11px;")
        else:
            self.mk_state.setText(f"○ {src} 尚不存在（官方启动器还没下载过游戏）")
            self.mk_state.setStyleSheet("color:#5a6675;font-size:11px;")

    def _pick_mk_dst(self):
        from .fspicker import pick_dir
        d = pick_dir(self, "选择数据要搬到的目录（其他盘）",
                     self.ed_mk_dst.text().strip() or "D:\\")
        if d:
            self.ed_mk_dst.setText(d)

    def _update_mk_cmds(self):
        dst = (self.ed_mk_dst.text() or r"D:\MinecraftData").strip() or r"D:\MinecraftData"
        dst = os.path.join(dst, ".minecraft") if os.path.basename(dst).lower() != ".minecraft" else dst
        self.mk_cmds.setText(self._relink.commands(self._mk_src, dst))

    def _copy_mk_cmds(self):
        QApplication.clipboard().setText(self.mk_cmds.text())
        Toast.show_toast(self.parent(),
                         "命令已复制，粘贴到命令提示符运行即可"
                         "（robocopy 与 mklink /J 都不需要管理员）")

    def _do_migrate(self):
        if getattr(self, "_migrating", False):
            return
        dst = self.ed_mk_dst.text().strip()
        if not dst:
            Toast.show_toast(self.parent(), "请先选择目标盘目录")
            return
        if os.path.basename(dst).lower() != ".minecraft":
            dst = os.path.join(dst, ".minecraft")
        src = self._mk_src

        dlg = _DialogBase(self, width=430, title="确认迁移游戏目录？")
        l = QLabel(
            f"数据将从：{src}\n数据搬到：{dst}\n\n"
            "· 迁移前会自动检查官方启动器是否已退出\n"
            "· 大型整合包可能需要几分钟，期间请勿关闭窗口\n"
            "· 官方启动器无需任何改动，照常从原位置读取")
        l.setStyleSheet("color:#4c5866;font-size:12px;")
        l.setWordWrap(True)
        dlg.body.addWidget(l)
        dlg.add_buttons([
            ("取消", "ghost", dlg.reject),
            ("开始迁移", "accent",
             lambda: (dlg.accept(), self._migrate_start(dst))),
        ])
        dlg.exec()

    def _migrate_start(self, dst):
        src = self._mk_src
        self._migrating = True
        self._btn_migrate.setEnabled(False)
        self.mk_state.setText("正在迁移（可能需要几分钟，请勿关闭窗口）…")
        self.mk_state.setStyleSheet("color:#2f7cf6;font-size:11px;")

        def done(res):
            self._migrating = False
            self._btn_migrate.setEnabled(True)
            ok, msg = res
            self._refresh_mk_state()
            if ok:
                Toast.show_toast(self.parent(), "迁移完成！游戏数据已存到目标盘")
            else:
                self.mk_state.setText("⚠ " + msg)
                self.mk_state.setStyleSheet("color:#d9534f;font-size:11px;")

        # relink.migrate 阻塞（robocopy 可能耗时），放工作线程；进度以静态文案 + 完成回调呈现
        run_async(self._relink.migrate, src, dst, on_done=done)


# ================================================================ 安装进度
class ProgressDailog(_DialogBase):
    """下载/安装进度窗口。

    绑定任务中心（tasks.Task）后，窗口只做**轮询展示**：
    后台线程从不调用这里的任何方法，因此取消/关闭窗口时不存在
    「工作线程操作已销毁控件」的崩溃路径。
    """

    cancelled = False

    def __init__(self, parent, title="正在安装"):
        super().__init__(parent, width=440, title=title)
        from PySide6.QtWidgets import QProgressBar
        self.task = None
        self.on_finished = None      # fn(task) 任务结束时回调（主线程）
        self._backgrounded = False
        self._closed_for_finish = False
        self.info = QLabel("准备中…")
        self.info.setStyleSheet("color:#5a6675;font-size:12px;")
        self.info.setWordWrap(True)
        self.body.addWidget(self.info)
        self.detail = QLabel("")
        self.detail.setStyleSheet("color:#8a95a3;font-size:11px;")
        self.body.addWidget(self.detail)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        self.bar.setStyleSheet(
            "QProgressBar{background:rgba(31,39,51,40);border-radius:5px;}"
            "QProgressBar::chunk{background:#2f7cf6;border-radius:5px;}")
        self.body.addWidget(self.bar)
        # 依赖明细：整合包安装时逐条展示「正在下载哪个文件 / 已完成 / 失败」
        from .taskdetail import TaskDetailList
        self.detail_list = TaskDetailList(height=126)
        self.body.addWidget(self.detail_list)
        self.cancel_flag = [False]
        row = self.add_buttons([("后台下载", "ghost", self._to_background),
                                ("取消", "ghost", self._cancel)])
        self._btn_bg = row.itemAt(0).widget()
        self._btn_cancel = row.itemAt(1).widget()
        self._poll = QTimer(self)
        self._poll.setInterval(180)
        self._poll.timeout.connect(self._tick)

    # ---------------- 绑定 ----------------
    def bind(self, task, on_finished=None):
        """绑定任务中心的任务，开始轮询展示。"""
        self.task = task
        self.on_finished = on_finished
        self.cancel_flag = task.cancel_flag
        self._closed_for_finish = False
        self._poll.start()
        self._tick()
        return self

    def is_background(self):
        return self._backgrounded

    # ---------------- 按钮 ----------------
    def _to_background(self):
        """收进后台：窗口隐藏，任务继续，主页任务面板接手展示。

        注意：轮询不停止 —— 隐藏的窗口仍要感知任务结束，以便弹出结果提示，
        否则后台下载完成后就没人收尾了（_dl_running 卡住、结果窗口不弹）。
        """
        if self.task is None or self.task.status != "running":
            return
        self._backgrounded = True
        self.task.background = True
        self.hide()
        Toast.show_toast(self.parent(), "已转入后台，进度可在主页查看", kind="info")

    def _cancel(self):
        if self.task is not None:
            if self.task.status != "running":
                self.reject()
                return
            from ..core.tasks import TASKS
            TASKS.cancel(self.task.tid)
        else:
            if self.cancel_flag[0]:
                return
            self.cancel_flag[0] = True
        self.info.setText("正在取消，请稍候…")
        self._btn_cancel.setText("取消中…")
        self._btn_cancel.setEnabled(False)
        if self._btn_bg is not None:
            self._btn_bg.setEnabled(False)

    # ---------------- 轮询 ----------------
    def _tick(self):
        t = self.task
        if t is None:
            return
        from ..core import tasks as task_mod
        if t.status != "running":
            self._poll.stop()
            self._render(t)
            if not self._closed_for_finish:
                self._closed_for_finish = True
                cb, self.on_finished = self.on_finished, None
                if cb:
                    try:
                        cb(t)
                    except Exception:
                        import traceback
                        traceback.print_exc()
                if not self._backgrounded:
                    self.reject()
                else:
                    self.deleteLater()
            return
        self._render(t)

    def _render(self, t):
        from ..core import tasks as task_mod
        pct = int(t.fraction * 100)
        if t.total_bytes or t.total_items:
            self.bar.setRange(0, 100)
            self.bar.setValue(pct)
        else:
            self.bar.setRange(0, 0)
        head = t.phase or "处理中"
        if t.total_bytes:
            head = f"{head}　{pct}%"
        self.info.setText(head)
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
        self.detail.setText(" · ".join(parts))
        # 依赖明细（没有明细的任务会自动隐藏这块）
        try:
            self.detail_list.update_from(t)
        except Exception:
            import traceback
            traceback.print_exc()

    def update_progress(self, done, total, text=None):
        """兼容旧调用：仅在没有绑定任务时使用（主线程内）。"""
        if total:
            self.bar.setRange(0, 100)
            self.bar.setValue(int(done / max(total, 1) * 100))
        if text:
            self.info.setText(text)

    def reject_silent(self):
        self.cancel_flag[0] = True

    def closeEvent(self, e):
        # 关窗视为转后台，不中断下载（避免误关导致任务丢失）
        if self.task is not None and self.task.status == "running":
            self._backgrounded = True
            self.task.background = True
        self._poll.stop()
        super().closeEvent(e)


# ================================================================ 项目详情
# ================================================================ 依赖关系面板
_DEP_STYLE = {
    "required":     ("rgba(214,69,69,24)",  "#d64545"),
    "optional":     ("rgba(47,124,246,24)", "#2f7cf6"),
    "incompatible": ("rgba(196,120,20,24)", "#c47814"),
    "embedded":     ("rgba(31,39,51,22)",   "#5a6675"),
    "dependent":    ("rgba(46,158,91,24)",  "#2e9e5b"),
    "tool":         ("rgba(120,80,200,24)", "#7850c8"),
    "include":      ("rgba(31,39,51,22)",   "#5a6675"),
}


def _size_text(n):
    try:
        n = int(n or 0)
    except Exception:                                  # noqa: BLE001
        return ""
    if n <= 0:
        return ""
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.0f} KB"
    return f"{n} B"


def _dep_icon_url(row):
    u = row.get("icon") or ""
    if u and "?" not in u:
        return u + "?imwidth=64"
    return u


class _DepRow(QWidget):
    """一条依赖：图标 / 名称 / 版本 / 类型标签 / 下载"""

    def __init__(self, row, on_download=None, on_open=None, show_kind=True,
                 parent=None):
        super().__init__(parent)
        self.row = row
        self._cb = on_download
        self._open = on_open
        h = QHBoxLayout(self)
        h.setContentsMargins(6, 3, 4, 3)
        h.setSpacing(8)

        icon = AsyncImageView(radius=6, placeholder="")
        icon.setFixedSize(26, 26)
        icon.load(_dep_icon_url(row))
        h.addWidget(icon)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        nm = QLabel(row.get("name") or "-")
        nm.setStyleSheet("color:#243040;font-size:12px;background:transparent;")
        nm.setMaximumWidth(200)
        nm.setToolTip((row.get("desc") or row.get("name") or "")[:300])
        col.addWidget(nm)
        bits = [b for b in (row.get("version") or "", _size_text(row.get("size"))) if b]
        sub = QLabel(" · ".join(bits) or (row.get("platform") or "—"))
        sub.setStyleSheet("color:#7b8794;font-size:10px;background:transparent;")
        sub.setMaximumWidth(200)
        col.addWidget(sub)
        # 注意：必须留一个显式 stretch，否则 QHBoxLayout 在超宽时会把
        # 多余空间均摊进所有间隙，图标和文字会被推开（曾表现为左侧空 39px）
        h.addLayout(col)
        h.addStretch(1)

        # 分组标题已经写明类型，行里不再重复挂标签（省下宽度给下载按钮）
        if show_kind:
            bg, fg = _DEP_STYLE.get(row.get("kind"), ("rgba(31,39,51,22)", "#5a6675"))
            chip = QLabel(row.get("kind_cn") or "")
            chip.setStyleSheet(f"QLabel{{background:{bg};color:{fg};font-size:10px;"
                               f"border-radius:7px;padding:2px 6px;}}")
            h.addWidget(chip)

        if row.get("available") and self._cb:
            b = PillButton("下载", "ghost")
            b.setFixedHeight(24)
            b.setFixedWidth(54)
            b.clicked.connect(lambda: self._cb(self.row))
            h.addWidget(b)
        else:
            lb = QLabel("无匹配")
            lb.setStyleSheet("color:#9aa5b1;font-size:10px;background:transparent;")
            lb.setToolTip("当前 Minecraft 版本 / 加载器下没有可用文件")
            h.addWidget(lb)
        self.setFixedHeight(36)


class _DepGroup(QWidget):
    """可收放的一组依赖"""

    def __init__(self, title, rows, on_download=None, on_open=None,
                 on_download_all=None, expanded=False, hint="", parent=None):
        super().__init__(parent)
        self.rows = rows or []
        self.hint = hint
        self.expanded = bool(expanded)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(5)

        self.head = QWidget()
        self.head.setAttribute(Qt.WA_StyledBackground, True)
        self.head.setCursor(Qt.PointingHandCursor)
        self.head.setFixedHeight(28)
        hh = QHBoxLayout(self.head)
        hh.setContentsMargins(9, 0, 6, 0)
        hh.setSpacing(6)
        self.arrow = QLabel()
        self.arrow.setStyleSheet("color:#8a95a3;font-size:11px;background:transparent;")
        hh.addWidget(self.arrow)
        tp = QLabel(title)
        tp.setStyleSheet("color:#33404f;font-size:12px;font-weight:600;"
                         "background:transparent;")
        hh.addWidget(tp)
        cnt = QLabel(str(len(self.rows)))
        cnt.setStyleSheet("QLabel{background:rgba(31,39,51,20);color:#5a6675;"
                          "font-size:10px;border-radius:7px;padding:1px 6px;}")
        hh.addWidget(cnt)
        hh.addStretch(1)
        avail = [r for r in self.rows if r.get("available")]
        self.btn_all = None
        if len(avail) > 1 and on_download_all:
            self.btn_all = PillButton(f"全部下载 {len(avail)}", "ghost")
            self.btn_all.setFixedHeight(22)
            self.btn_all.setFixedWidth(88)
            self.btn_all.clicked.connect(lambda: on_download_all(avail))
            hh.addWidget(self.btn_all)
        lay.addWidget(self.head)

        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(4, 0, 2, 4)
        bl.setSpacing(0)
        for r in self.rows:
            bl.addWidget(_DepRow(r, on_download, on_open, show_kind=False))
        if hint:
            lb = QLabel(hint)
            lb.setWordWrap(True)
            lb.setStyleSheet("color:#9aa5b1;font-size:10px;background:transparent;"
                             "padding:2px 8px 2px 8px;")
            bl.addWidget(lb)
        lay.addWidget(self.body)

        self.head.mousePressEvent = self.toggle
        self._style()
        self.body.setVisible(self.expanded)

    def _style(self):
        bg = "rgba(255,255,255,150)" if self.expanded else "rgba(255,255,255,95)"
        self.head.setStyleSheet(
            "QWidget{background:%s;border:1px solid rgba(31,39,51,14);"
            "border-radius:9px;}" % bg)

    def toggle(self, e=None):
        self.expanded = not self.expanded
        self.body.setVisible(self.expanded)
        self._style()
        self.arrow.setText("▾" if self.expanded else "▸")
        # 通知外层重算滚动区高度
        w = self.parentWidget()
        while w is not None and not hasattr(w, "_relayout"):
            w = w.parentWidget()
        if w is not None:
            w._relayout()

    def content_height(self):
        h = 28
        if self.expanded:
            h += 36 * len(self.rows)
            if self.hint:
                h += 26
            h += 6
        return h


class _VerRow(QWidget):
    """往期版本里的一行：版本号 + 适配标记 + MC/加载器 + 日期 + 大小，整行可点。

    第五十八轮：详情页要能「选一个往期版本再下」，所以必须是真正的可选行，
    不是一串死文字 —— 选中态由 `set_selected()` 统一刷，外面只认 `.row`。
    """
    clicked = Signal(object)

    def __init__(self, row, adapted=False, parent=None):
        super().__init__(parent)
        self.row = row
        self._sel = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(30)
        self.setAttribute(Qt.WA_StyledBackground, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 10, 0)
        lay.setSpacing(7)

        self.mark = QLabel("")
        self.mark.setFixedWidth(11)
        self.mark.setStyleSheet("color:#2f7cf6;font-size:12px;background:transparent;")
        lay.addWidget(self.mark)

        lb = QLabel(str(row.get("label") or "?"))
        lb.setStyleSheet("color:#2a3442;font-size:12px;background:transparent;")
        lb.setMinimumWidth(104)
        lb.setMaximumWidth(190)
        lay.addWidget(lb, 0)

        ch = modrinth_channel(row.get("channel"))
        if ch and ch != "正式":
            chip = QLabel(ch)
            chip.setStyleSheet("color:#b07d18;background:rgba(240,180,40,28);"
                               "border-radius:7px;padding:1px 6px;font-size:10px;")
            lay.addWidget(chip)
        if adapted:
            chip = QLabel("适配当前")
            chip.setStyleSheet("color:#1d7a4a;background:rgba(47,180,110,30);"
                               "border-radius:7px;padding:1px 6px;font-size:10px;")
            lay.addWidget(chip)

        lay.addStretch(1)
        meta = QLabel(" · ".join(x for x in (row.get("mc"), row.get("loader")) if x) or "-")
        meta.setStyleSheet("color:#5a6675;font-size:11px;background:transparent;")
        meta.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        meta.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(meta, 1)
        tail = QLabel(f"{row.get('date') or ''}  {_fmt_size(row.get('size'))}".strip())
        tail.setStyleSheet("color:#8a95a3;font-size:10px;background:transparent;")
        lay.addWidget(tail, 0)
        self._style()

    def _style(self):
        bg = "rgba(47,124,246,30)" if self._sel else "rgba(255,255,255,110)"
        bd = "rgba(47,124,246,90)" if self._sel else "rgba(31,39,51,12)"
        self.setStyleSheet(
            f"_VerRow{{background:{bg};border:1px solid {bd};border-radius:8px;}}")

    def set_selected(self, v):
        self._sel = bool(v)
        self.mark.setText("✓" if self._sel else "")
        self._style()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.row)
        super().mousePressEvent(e)


class DetailDialog(_DialogBase):
    def __init__(self, parent, hit, on_install, installed=False,
                 ctx=None, on_dep_download=None, on_dep_download_many=None,
                 kind="", caps=None, on_langpack=None):
        super().__init__(parent, width=520)
        self.hit = hit
        self._groups = []
        self._dep_marks = []          # 与 _groups 对应的 (标题, kind)
        self._on_dep_dl = on_dep_download
        self._on_dep_dl_many = on_dep_download_many
        self._ctx = ctx or {}
        self._on_install = on_install
        # 第七十六轮：「汉化」按钮的回调 —— 由 browse 负责「没装先装、装完续汉化」
        self._on_langpack = on_langpack
        self._lang_proj = {}          # BBSMC 上这个整合包的汉化项目（没有就是 {}）
        self._lang_vers = []          # 该汉化项目的版本列表
        self._lang_ready = False      # 查完了没（查完之前按钮一律灰）
        self._installed = bool(installed)
        self._ver_rows = []
        self._ver_picked = None       # 选中的往期版本行；None = 自动最新
        # 第六十一轮：所选游戏装不了这类内容（原版装不了模组/光影）时，
        # 整页照样能看，但「下载」按钮一律灰掉并写清原因
        from ..core import gamelaunch as _gl
        self._kind = str(kind or "")
        self._cap_note = _gl.caps_note(self._kind, caps or {})
        title = cn_names.title_of(hit)
        row = QHBoxLayout()
        row.setSpacing(14)
        icon = AsyncImageView(radius=12, placeholder="")
        icon.setFixedSize(84, 84)
        from .browse import icon_url_for
        icon.load(icon_url_for(hit))
        row.addWidget(icon)
        right = QVBoxLayout()
        right.setSpacing(4)
        t = QLabel(title)
        f = QFont(theme.FONT)
        f.setPixelSize(16)
        f.setBold(True)
        t.setFont(f)
        t.setWordWrap(True)
        right.addWidget(t)
        raw_title = str(hit.get("title") or hit.get("name") or "")
        if title and raw_title and title != raw_title:
            # 中文名优先显示，原名留一行小的：玩家在别处（存档/视频）看到的是原名
            lb_raw = QLabel(raw_title)
            lb_raw.setStyleSheet("color:#8a95a3;font-size:11px;")
            lb_raw.setWordWrap(True)
            right.addWidget(lb_raw)
        dl = hit.get("downloads", hit.get("downloadCount", 0))
        follows = hit.get("follows", 0)
        author = hit.get("author") or ((hit.get("authors") or [{}])[0].get("name", "-"))
        meta = QLabel(f"作者 {author} · 下载 {_fmt(dl)} · 关注 {_fmt(follows)}")
        meta.setStyleSheet("color:#5a6675;font-size:12px;")
        right.addWidget(meta)
        right.addStretch(1)
        row.addLayout(right, 1)
        self.body.addLayout(row)

        tags_row = QHBoxLayout()
        tags_row.setSpacing(6)
        tags = (hit.get("display_categories") or hit.get("categories") or [])[:6]
        for c in tags:
            if not isinstance(c, str):
                continue
            chip = QLabel(cn_category(c))
            chip.setStyleSheet(
                "QLabel{background:rgba(47,124,246,26);color:#2f7cf6;font-size:11px;"
                "border-radius:8px;padding:3px 9px;}")
            tags_row.addWidget(chip)
        tags_row.addStretch(1)
        if tags:
            self.body.addLayout(tags_row)

        en_desc = str(hit.get("description") or hit.get("summary") or "")
        desc = QLabel(cn_names.desc_of(hit) or "暂无简介")
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#2a3442;font-size:12px;")
        desc.setMaximumHeight(76)
        if cn_names.cn_desc(hit) and en_desc:
            desc.setToolTip(en_desc)          # 中文简介是我们配的，原文留在 tooltip 里
        self.body.addWidget(desc)

        if self._cap_note:
            warn = QLabel("这个装不进去：" + self._cap_note)
            warn.setWordWrap(True)
            warn.setStyleSheet(
                "QLabel{background:rgba(179,55,43,22);color:#b3372b;"
                "font-size:11.5px;border-radius:8px;padding:7px 9px;}")
            self.body.addWidget(warn)

        # ---- 往期版本（第五十八轮）----
        vhead = QHBoxLayout()
        vhead.setSpacing(6)
        vlb = QLabel("往期版本")
        f3 = QFont(theme.FONT)
        f3.setPixelSize(13)
        f3.setBold(True)
        vlb.setFont(f3)
        vhead.addWidget(vlb)
        self._ver_sum = QLabel("正在获取…")
        self._ver_sum.setStyleSheet("color:#8a95a3;font-size:11px;")
        vhead.addWidget(self._ver_sum)
        vhead.addStretch(1)
        self.body.addLayout(vhead)

        self._ver_host = QWidget()
        self._ver_lay = QVBoxLayout(self._ver_host)
        self._ver_lay.setContentsMargins(0, 0, 0, 0)
        self._ver_lay.setSpacing(4)
        self._ver_scroll = QScrollArea()
        self._ver_scroll.setWidgetResizable(True)
        self._ver_scroll.setFrameShape(QScrollArea.NoFrame)
        self._ver_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._ver_scroll.setStyleSheet(
            "QScrollArea{background:transparent;}"
            "QScrollBar:vertical{width:6px;background:transparent;margin:2px;}"
            "QScrollBar::handle:vertical{background:rgba(120,132,148,90);"
            "border-radius:3px;min-height:24px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        self._ver_scroll.setWidget(self._ver_host)
        self._ver_scroll.setFixedHeight(112)
        self.body.addWidget(self._ver_scroll)

        # ---- 依赖关系（分类收放）----
        head = QHBoxLayout()
        head.setSpacing(6)
        lb = QLabel("依赖关系")
        f2 = QFont(theme.FONT)
        f2.setPixelSize(13)
        f2.setBold(True)
        lb.setFont(f2)
        head.addWidget(lb)
        self._dep_sum = QLabel("正在检测…")
        self._dep_sum.setStyleSheet("color:#8a95a3;font-size:11px;")
        head.addWidget(self._dep_sum)
        head.addStretch(1)
        self.body.addLayout(head)

        self._dep_host = QWidget()
        self._dep_lay = QVBoxLayout(self._dep_host)
        self._dep_lay.setContentsMargins(0, 0, 0, 0)
        self._dep_lay.setSpacing(6)
        self._dep_scroll = QScrollArea()
        self._dep_scroll.setWidgetResizable(True)
        self._dep_scroll.setFrameShape(QScrollArea.NoFrame)
        self._dep_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._dep_scroll.setStyleSheet(
            "QScrollArea{background:transparent;}"
            "QScrollBar:vertical{width:6px;background:transparent;margin:2px;}"
            "QScrollBar::handle:vertical{background:rgba(120,132,148,90);"
            "border-radius:3px;min-height:24px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        self._dep_scroll.setWidget(self._dep_host)
        self._dep_scroll.setFixedHeight(58)
        self.body.addWidget(self._dep_scroll)

        brow = QHBoxLayout()
        brow.addStretch(1)
        brow.setSpacing(10)
        b_cancel = PillButton("取消", "ghost")
        b_cancel.setFixedHeight(36)
        b_cancel.setMinimumWidth(92)
        b_cancel.clicked.connect(self.reject)
        brow.addWidget(b_cancel)
        # 第七十六轮：「汉化」放在「下载」左边。有没有汉化是**联网查的**，
        # 查完之前一律灰（第七十六轮之前点「下载」没反应那种「点了没反应」
        # 就是按钮状态没跟着数据走），查完再决定灰不灰。
        self._btn_lang = None
        if str(self._kind or "") == "modpack":
            self._btn_lang = PillButton("汉化", "ghost")
            self._btn_lang.setFixedHeight(36)
            self._btn_lang.setMinimumWidth(92)
            self._btn_lang.clicked.connect(self._start_lang)
            brow.addWidget(self._btn_lang)
        self._btn_install = PillButton("下载并安装", "accent")
        self._btn_install.setFixedHeight(36)
        self._btn_install.setMinimumWidth(92)
        self._btn_install.clicked.connect(lambda: self._install(self._on_install))
        brow.addWidget(self._btn_install)
        self.body.addLayout(brow)
        self._sync_install()
        if self._btn_lang is not None:
            self._sync_lang()
            run_async(self._load_lang, on_done=self._on_lang_ok,
                      on_err=self._on_lang_err)

        self._dead = False
        mc = ""
        loader = ""
        try:
            vs = self._ctx.get("versions") or []
            if vs:
                mc = vs[0]
            ls = self._ctx.get("loaders") or []
            if ls:
                loader = ls[0]
        except Exception:                              # noqa: BLE001
            pass
        self._mc = mc
        self._loader = loader
        self._ready = QLabel("正在获取版本列表，请稍候…")
        self._ready.setStyleSheet("color:#8a95a3;font-size:11px;padding:5px 8px;")
        self._ver_lay.addWidget(self._ready)
        run_async(modrinth.version_rows, hit, "", 60, mc, loader,
                  on_done=self._on_vers, on_err=self._on_vers_err)

        self._loading = QLabel("正在获取前置与下游依赖，请稍候…")
        self._loading.setStyleSheet("color:#8a95a3;font-size:11px;padding:6px 8px;")
        self._dep_lay.addWidget(self._loading)
        run_async(deps.analyze, hit, mc, loader,
                  on_done=self._on_deps, on_err=self._on_deps_err)

    # ---------- 往期版本 ----------
    def _on_vers_err(self, msg):
        if getattr(self, "_dead", False):
            return
        try:
            self._ver_sum.setText("获取失败")
            self._ready.setText(f"版本列表获取失败：{str(msg)[:70]}")
        except RuntimeError:
            pass

    def _on_vers(self, rows):
        if getattr(self, "_dead", False):
            return
        try:
            while self._ver_lay.count():
                it = self._ver_lay.takeAt(0)
                w = it.widget()
                if w is not None:
                    w.hide()        # 第八十二轮：先 hide，别让排队 show 把它变成窗口
                    w.setParent(None)
                    w.deleteLater()
            self._ver_rows = []
        except RuntimeError:
            return
        if not rows:
            self._ver_sum.setText("暂无")
            self._ver_lay.addWidget(self._mk_note(
                "没拿到版本列表（接口暂时不通，或该项目只有一个文件）——"
                "「下载并安装」仍会按自动匹配装最新兼容版本"))
            return
        # 第 0 行 = 「自动」，让玩家随时能退回默认行为
        auto = _VerRow({"label": "自动（最新兼容版本）", "mc": "", "loader": "",
                        "date": "", "size": 0, "channel": "release", "src": ""})
        auto.clicked.connect(self._pick)
        self._ver_lay.addWidget(auto)
        self._ver_rows.append(auto)
        matched = 0
        for r in rows:
            # 精确比 MC 版本（"1.20" 不能算命中 "1.20.11"，子串比会误标「适配当前」）
            mcs = [x.strip() for x in str(r.get("mc") or "").split(",")]
            adapted = bool(self._mc) and self._mc in mcs
            matched += 1 if adapted else 0
            w = _VerRow(r, adapted=adapted)
            w.clicked.connect(self._pick)
            self._ver_lay.addWidget(w)
            self._ver_rows.append(w)
        self._pick(auto.row)
        if matched:
            self._ver_sum.setText(
                f"共 {len(rows)} 个（按 {self._mc}/{self._loader or '任意'} 筛）")
        else:
            self._ver_sum.setText(f"共 {len(rows)} 个 · 点一行选中再点「下载并安装」")
        self._ver_sum.setToolTip(
            "「适配当前」= 这个版本的 MC 版本与当前筛选一致；"
            "不选直接下载则自动挑最新的兼容版本")

    def _pick(self, row):
        if getattr(self, "_dead", False):
            return
        # 「自动」那一行的 src 是空串 —— 它代表「不指定版本」，不能当选中版本用，
        # 否则「已安装」的项目也会被放出可点按钮，装出第二份同名文件
        self._ver_picked = None if (not row or not row.get("src")) else row
        for w in self._ver_rows:
            try:
                w.set_selected(w.row is row)
            except RuntimeError:
                pass
        self._sync_install()
        # 换了往期版本 → 对应的汉化也变了，提示要跟着更新
        self._sync_lang()

    def _sync_install(self):
        """按「有没有选往期版本」刷按钮：选了就往期，没选就自动最新。

        第六十一轮：所选游戏装不了这类内容时，无论选没选往期版本都不给下。
        """
        b = getattr(self, "_btn_install", None)
        if b is None:
            return
        if getattr(self, "_cap_note", ""):
            b.setText("不可下载")
            b.kind = "ghost"
            b.setEnabled(False)
            b.setCursor(Qt.WhatsThisCursor)
            b.setToolTip(self._cap_note)
            return
        if self._ver_picked:
            b.setText("下载并安装所选版本")
            b.kind = "accent"
            b.setEnabled(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(f"将安装 {self._ver_picked.get('label') or ''}"
                         f"（{self._ver_picked.get('mc') or '?'} / "
                         f"{self._ver_picked.get('loader') or '?'}）")
            return
        b.setText("已安装" if self._installed else "下载并安装")
        b.setEnabled(not self._installed)
        b.setCursor(Qt.ArrowCursor if self._installed else Qt.PointingHandCursor)
        b.setToolTip("" if not self._installed else
                     "已装过这个项目；想换版本就在上面选一个往期版本")

    # ---------- 汉化（第七十六轮）----------
    def _load_lang(self):
        """工作线程：问 BBSMC 这个整合包有没有汉化，有就把版本表一起拿回来。

        一次拿全（项目详情 + 版本列表）而不是分两次点：版本表才几十 KB，
        提前拿到才能做到「换一个往期版本，汉化按钮的提示立刻跟着变」。
        """
        from ..core import bbsmc
        proj = bbsmc.find_language(self.hit)
        if not proj:
            return {"proj": {}, "versions": []}
        return {"proj": proj, "versions": bbsmc.language_versions(proj)}

    def _on_lang_ok(self, res):
        if getattr(self, "_dead", False):
            return
        res = res or {}
        self._lang_proj = res.get("proj") or {}
        self._lang_vers = res.get("versions") or []
        self._lang_ready = True
        self._sync_lang()

    def _on_lang_err(self, msg):
        if getattr(self, "_dead", False):
            return
        self._lang_ready = True
        self._lang_vers = []
        self._sync_lang(str(msg or ""))

    def _sync_lang(self, err: str = ""):
        """按「查没查到 + 有没有对应所选版本的汉化」刷「汉化」按钮。

        · 还没查完        → 灰，写「正在检查」
        · BBSMC 没有      → 灰，写「没有这个整合包的汉化」（这就是需求里
                             「如果没有选项则呈现灰色，且无法点击」）
        · 有但版本对不上  → **仍然可点**，提示按最新汉化走 —— 版本对不上时
                             由 `_start_lang` 在弹窗里说清楚，比直接灰掉更好：
                             玩家其实只想玩玩看，不是非要精确到小版本
        """
        b = getattr(self, "_btn_lang", None)
        if b is None:
            return
        if not self._lang_ready:
            b.setText("汉化")
            b.kind = "ghost"
            b.setEnabled(False)
            b.setCursor(Qt.WhatsThisCursor)
            b.setToolTip("正在检查 BBSMC 上有没有这个整合包的汉化…")
            return
        if not self._lang_vers:
            b.setText("汉化")
            b.kind = "ghost"
            b.setEnabled(False)
            b.setCursor(Qt.WhatsThisCursor)
            b.setToolTip("BBSMC 上没有这个整合包的汉化包"
                         + (f"（{err[:40]}）" if err else ""))
            return
        b.setEnabled(True)
        b.kind = "accent"
        b.setCursor(Qt.PointingHandCursor)
        hit = self._lang_match()
        if hit:
            b.setToolTip(f"BBSMC 汉化：{hit.get('name')}")
        else:
            newest = self._lang_vers[0].get("name") if self._lang_vers else ""
            b.setToolTip("所选版本没有对应的汉化；点下去会按最新的汉化"
                         f"（{newest}）。要精确对应请先在下面选那个整合包版本")

    def _lang_match(self):
        """按当前选中的往期版本挑对应汉化；挑不出返回 {}"""
        try:
            from ..core import bbsmc
        except Exception:                              # noqa: BLE001
            return {}
        vn, label = "", ""
        picked = self._ver_picked
        if picked:
            raw = picked.get("raw") or {}
            f = raw.get("file") or {}
            v = raw.get("version") or {}
            # 版本号本身
            vn = str(f.get("id") or "") or str(v.get("version_number") or "")
            label = str(picked.get("label") or "")
        ref = str(self.hit.get("slug") or "")
        return bbsmc.match_version(self._lang_vers, vn, label, ref)

    def _start_lang(self):
        """点「汉化」：把选中的汉化版本交给 browse 去下载 / 核对 / 落地。"""
        if not self._lang_vers:
            Toast.show_toast(self, "BBSMC 上没有这个整合包的汉化",
                             ms=3200, kind="error")
            return
        matched = self._lang_match()
        auto = False
        if not matched:
            # 版本对不上就用最新的那份，但要让玩家知道「不是精确对应」
            matched = self._lang_vers[0]
            auto = True
        picked = self._ver_picked
        self.accept()
        cb = self._on_langpack
        if not cb:
            return
        try:
            cb(self.hit, picked, matched, self._lang_proj, auto)
        except TypeError:
            try:
                cb(matched)
            except Exception:                          # noqa: BLE001
                pass

    # ---------- 依赖结果 ----------
    def _on_deps_err(self, msg):
        if getattr(self, "_dead", False):
            return
        try:
            self._dep_sum.setText("检测失败")
            self._loading.setText(f"依赖检测失败：{str(msg)[:70]}")
        except RuntimeError:
            pass

    def _clear_deps(self):
        while self._dep_lay.count():
            it = self._dep_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()            # 第八十二轮：先 hide，别让排队 show 把它变成窗口
                w.setParent(None)
                w.deleteLater()
        self._groups = []

    def _on_deps(self, res):
        if getattr(self, "_dead", False) or not res:
            return
        try:
            self._clear_deps()
        except RuntimeError:
            return
        fwd = res.get("forward") or []
        rev = res.get("reverse") or []
        tgt = res.get("target") or {}

        def pick(kind):
            return [r for r in fwd if r.get("kind") == kind]

        req = pick("required")
        opt = pick("optional") + pick("include")
        bad = pick("incompatible")
        emb = pick("embedded")

        specs = [
            ("必需前置", "required", req, True,
             "" if req else "未声明必需前置"),
            ("可选前置", "optional", opt, False,
             "可选内容，按需安装（不装也能启动）"),
            ("不兼容", "incompatible", bad, False,
             "与这些模组同时安装可能出错"),
            ("已内置", "embedded", emb, False,
             "这些内容作者已打包进本体，无需单独下载"),
            ("依赖此模组", "dependent", rev, False,
             "安装了这些模组的实例需要本模组才能正常启动"),
        ]
        added = 0
        for title, kind, rows, expanded, hint in specs:
            if not rows:
                continue
            g = _DepGroup(title, rows, on_download=self._on_dep_dl,
                          on_open=self._open_url,
                          on_download_all=self._on_dep_dl_many,
                          expanded=expanded, hint=hint)
            self._dep_lay.addWidget(g)
            self._groups.append(g)
            added += 1

        # 说明（CF 无反向接口 / 按哪个版本分析等），始终展示，避免「空=没检测」
        notes = [n for n in (res.get("notes") or []) if n]
        if not added:
            self._dep_lay.addWidget(self._mk_note(
                "未检测到前置或下游依赖（该项目可能完全独立）"))
        for n in notes[:3]:
            self._dep_lay.addWidget(self._mk_note(n))

        bits = []
        if req:
            bits.append(f"前置 {len(req)}")
        if opt:
            bits.append(f"可选 {len(opt)}")
        if rev:
            bits.append(f"下游 {len(rev)}")
        if bad:
            bits.append(f"冲突 {len(bad)}")
        self._dep_sum.setText(" · ".join(bits) if bits else "无依赖")
        if tgt.get("version"):
            self._dep_sum.setToolTip(
                f"按 {tgt.get('mc') or '?'} / {tgt.get('loader') or '?'} "
                f"的 {tgt['version']} 分析")
        self._relayout()

    def _mk_note(self, text):
        lb = QLabel(text)
        lb.setWordWrap(True)
        lb.setStyleSheet("color:#8a95a3;font-size:10px;background:transparent;"
                         "padding:2px 8px;")
        return lb

    def _relayout(self):
        h = 8
        for g in self._groups:
            h += g.content_height() + 6
        self._dep_scroll.setFixedHeight(max(52, min(h, 236)))

    # ---------- 安装 ----------
    def _install(self, cb):
        if getattr(self, "_cap_note", ""):
            # 按钮本来就是灰的，这里是双保险（快捷键 / 手滑点上来）
            Toast.show_toast(self, self._cap_note[:80], ms=4000, kind="error")
            return
        picked = self._ver_picked
        self.accept()
        if not cb:
            return
        # 第五十八轮：选过往期版本就把那一行交出去（browse 据此走「指定版本下载」），
        # 没选就按老样子传 None —— 老调用点（无参回调）也能照旧工作。
        try:
            cb(picked)
        except TypeError:
            cb()

    def done(self, r):
        self._dead = True
        super().done(r)


def modrinth_channel(ch) -> str:
    """版本通道的中文说法（release/beta/alpha 或 CF 的 1/2/3）"""
    try:
        return modrinth.channel_cn(ch)
    except Exception:                                  # noqa: BLE001
        return ""


def _fmt_size(n) -> str:
    try:
        n = int(n or 0)
    except Exception:                                  # noqa: BLE001
        return ""
    if n <= 0:
        return ""
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n/div:.1f}{unit}"
    return f"{n}B"


def _fmt(n):
    try:
        n = int(n)
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n/1_000:.1f}k"
        return str(n)
    except Exception:
        return str(n)


# ================================================================ 补全整合包（第一百零九轮）
class _PackCheck(QWidget):
    """18×18 自绘勾选框（不用系统 QCheckBox：磨砂卡片上方框几乎看不见，只剩个 ✓）"""

    def __init__(self, parent=None, size=18):
        super().__init__(parent)
        self.setFixedSize(size, size)
        # 第一百零九轮+：本身不接收鼠标事件，点击统一交给父行处理 —— 点方框还是点整行
        # 都只切换一次，不会出现「点方框多切换一遍」的双触发。
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.on = True

    def set_on(self, v):
        v = bool(v)
        if self.on != v:
            self.on = v
            self.update()

    def paintEvent(self, e):
        from PySide6.QtCore import QPointF, QRectF
        from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        if self.on:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#3f7d5a"))
            p.drawRoundedRect(r, 5, 5)
            p.setPen(QPen(QColor("#ffffff"), 1.8, Qt.SolidLine,
                          Qt.RoundCap, Qt.RoundJoin))
            sz = self.width()
            p.drawPolyline(QPolygonF([
                QPointF(sz * 0.24, sz * 0.52),
                QPointF(sz * 0.43, sz * 0.70),
                QPointF(sz * 0.77, sz * 0.31)]))
        else:
            p.setPen(QPen(QColor(120, 132, 148, 170), 1.4))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r, 5, 5)
        p.end()


class _PackCheckRow(QWidget):
    """一行：勾选框 + 包名 + 版本信息（点整行也能勾）"""

    def __init__(self, pack: dict, parent=None):
        super().__init__(parent)
        self.pack = pack
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(8)
        self.cb = _PackCheck()
        self.cb.set_on(True)
        lay.addWidget(self.cb)
        from .widgets import ElideLabel
        name = ElideLabel(str(pack.get("name") or "?"))
        name.setStyleSheet("color:#1f2733;font-size:13px;font-weight:600;")
        lay.addWidget(name, 1)
        bits = []
        if pack.get("mc"):
            bits.append(str(pack["mc"]))
        if pack.get("loader"):
            bits.append(f"{pack['loader']} {pack.get('loader_ver') or ''}".strip())
        if pack.get("fmt"):
            bits.append({"modrinth": "MR",
                         "bagui": "八桂"}.get(str(pack["fmt"]), str(pack["fmt"])))
        meta = QLabel(" · ".join(bits))
        meta.setStyleSheet("color:#7c8794;font-size:11px;")
        lay.addWidget(meta, 0)
        self.setCursor(Qt.PointingHandCursor)

    def is_checked(self):
        return bool(self.cb.on)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.cb.set_on(not self.cb.on)


class FillPacksDialog(_DialogBase):
    """补全整合包文件：**先选包，再带进度条补**。

    第一百零九轮的反馈：「点了补全就在那儿转，也不知道补完了没」。
    现在第一步列出本机能补的整合包（默认全选），点「开始补全」才动；
    第二步是同窗内的进度页 —— 进度条 + 「正在补哪一个 / 卡在哪一步」；
    结束停在结果页，明确说「补完 N 个 / 还缺什么」，不自动消失。
    """

    def __init__(self, parent, packs=None):
        super().__init__(parent, width=540, title="补全整合包文件")
        from PySide6.QtWidgets import QStackedWidget
        from .widgets import ElideLabel, ProgressBar
        self._rows = []
        self._busy = False
        self._packs = list(packs if packs is not None else [])
        self._stack = QStackedWidget()
        self.body.addWidget(self._stack, 1)

        # ---------- 页 1：选包 ----------
        pick = QWidget()
        pl = QVBoxLayout(pick)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(10)
        tip = QLabel("照每个包自己的清单核对一遍，缺的文件重新下回来。"
                     "勾选要补的整合包：")
        tip.setStyleSheet("color:#5a6675;font-size:12px;")
        tip.setWordWrap(True)
        pl.addWidget(tip)
        if not self._packs:
            empty = QLabel("没有找到带清单（manifest.json / modrinth.index.json）的整合包。")
            empty.setStyleSheet("color:#8a95a3;font-size:12px;")
            empty.setWordWrap(True)
            pl.addWidget(empty)
        else:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QScrollArea.NoFrame)
            area.setFixedHeight(min(300, 44 * len(self._packs) + 8))
            area.setStyleSheet("QScrollArea{background:transparent;}"
                               "QScrollArea>QWidget>QWidget{background:transparent;}")
            host = QWidget()
            host.setStyleSheet("background:transparent;")
            hl = QVBoxLayout(host)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(2)
            for p in self._packs:
                row = _PackCheckRow(p)
                hl.addWidget(row)
                self._rows.append(row)
            hl.addStretch(1)
            area.setWidget(host)
            pl.addWidget(area)
            sel = QHBoxLayout()
            sel.setSpacing(8)
            b_all = PillButton("全选", "ghost")
            b_all.setFixedHeight(30)
            b_all.clicked.connect(lambda: [r.cb.set_on(True) for r in self._rows])
            b_none = PillButton("全不选", "ghost")
            b_none.setFixedHeight(30)
            b_none.clicked.connect(lambda: [r.cb.set_on(False) for r in self._rows])
            sel.addWidget(b_all)
            sel.addWidget(b_none)
            sel.addStretch(1)
            pl.addLayout(sel)
        self._stack.addWidget(pick)

        # ---------- 页 2：进度 ----------
        prog = QWidget()
        gl = QVBoxLayout(prog)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setSpacing(10)
        self.lb_stage = ElideLabel("准备中…")
        self.lb_stage.setStyleSheet("color:#1f2733;font-size:13px;font-weight:600;")
        gl.addWidget(self.lb_stage)
        self.bar = ProgressBar(height=10)
        gl.addWidget(self.bar)
        self.lb_result = QLabel("")
        self.lb_result.setStyleSheet("color:#5a6675;font-size:12px;")
        self.lb_result.setWordWrap(True)
        gl.addWidget(self.lb_result)
        gl.addStretch(1)
        self._stack.addWidget(prog)

        # ---------- 底部按钮 ----------
        brow = self.add_buttons([("取消", "ghost", self.reject),
                                 ("开始补全", "accent", self._start)])
        # `add_buttons` 先 addStretch(1)，所以 itemAt(0) 是空占位：
        # 从尾部取才是按钮（0 号是 spacer，取到 None 会让按钮一直"没反应"）
        _n = brow.count()
        self._btn_cancel = brow.itemAt(_n - 2).widget()
        self._btn_go = brow.itemAt(_n - 1).widget()
        if not self._packs:
            self._btn_go.setEnabled(False)
        self._prog = {"pct": 0.0, "text": "", "done": False, "result": None,
                      "err": ""}
        self._tick = QTimer(self)
        self._tick.setInterval(80)
        self._tick.timeout.connect(self._paint)
        self._fit()

    def _fit(self):
        """按当前页调整高度（弹窗是固定宽度 + 自适应高度）"""
        self._stack.adjustSize()
        h = self._stack.sizeHint().height() + 150
        self.setFixedHeight(max(230, min(560, h)))
        self.root.setGeometry(self.rect())

    def _start(self):
        if self._busy:
            return
        paths = [r.pack["path"] for r in self._rows if r.is_checked()]
        if not paths:
            Toast.show_toast(self, "先勾选要补的整合包", kind="error")
            return
        from ..core import packfill
        self._busy = True
        self._prog.update({"pct": 0.0, "text": "准备中…", "done": False,
                           "result": None, "err": ""})
        self._stack.setCurrentIndex(1)
        self._btn_go.setEnabled(False)
        self._btn_go.setText("补全中…")
        self._btn_cancel.setText("取消")
        self._btn_cancel.setEnabled(True)
        self._btn_cancel.clicked.disconnect()
        self._btn_cancel.clicked.connect(self._cancel)
        self.bar.set_value(0)
        self.lb_result.setText("")
        self._fit()
        self._tick.start()

        # ⚠ 工作线程只许写 self._prog 这个 dict
        def on_prog(done, total, text=""):
            try:
                t = float(total or 0)
                self._prog["pct"] = (float(done) / t) if t > 0 else 0.0
            except (TypeError, ValueError):
                self._prog["pct"] = 0.0
            self._prog["text"] = str(text or "")

        def done(msgs):
            self._prog["done"] = True
            self._prog["result"] = list(msgs or [])

        def err(e):
            self._prog["done"] = True
            self._prog["err"] = str(e)

        self._cancel_flag = [False]
        run_async(packfill.fill_all, progress_cb=on_prog,
                  cancel_flag=self._cancel_flag, only=paths,
                  on_done=done, on_err=err)

    def _cancel(self):
        if getattr(self, "_cancel_flag", None):
            self._cancel_flag[0] = True
            self.lb_stage.setText("正在取消…")
            self._btn_cancel.setEnabled(False)

    def _paint(self):
        st = self._prog
        if st["done"]:
            self._tick.stop()
            self._busy = False
            msgs = st.get("result") or []
            if st.get("err"):
                self.lb_stage.setText("补全失败")
                self.lb_result.setText(st["err"][:300])
            elif not msgs:
                self.lb_stage.setText("整合包都是完整的")
                self.lb_result.setText("没有要补的文件。")
                self.bar.set_value(1.0)
            else:
                bad = [m for m in msgs if ("还缺" in m or "没解压出来" in m
                                          or "无法核对" in m or "仍缺" in m)]
                self.lb_stage.setText("补全完成" + ("（有未解决项）" if bad else ""))
                self.lb_result.setText("\n".join(msgs[:8])
                                       + ("\n…" if len(msgs) > 8 else ""))
                self.bar.set_value(1.0)
            self._btn_go.setEnabled(True)
            self._btn_go.setText("关闭")
            try:
                self._btn_go.clicked.disconnect()
            except RuntimeError:
                pass
            self._btn_go.clicked.connect(self.accept)
            self._btn_cancel.hide()
            self._fit()
            return
        self.bar.set_value(float(st.get("pct") or 0.0))
        self.lb_stage.setText(str(st.get("text") or "补全中…"))
