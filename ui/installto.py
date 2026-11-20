# -*- coding: utf-8 -*-
"""「保存模组 / 光影 / 材质包 / 数据包」对话框（第五十七轮）。

要解决的三件事，一条都不能少：

1. **按所选游戏分配**：目标文件夹由「选择要启动的游戏」决定（`<实例>/mods` 等），
   不再一律丢进官方启动器的根目录；没选游戏时如实说明「将装到官方启动器目录」，
   并在对话框里给出**完整路径**，玩家点保存前就知道会落到哪。
2. **只有模组 / 数据包能改名**：光影和材质包的文件名被它们自己的内容引用
   （shaderpack 的 txt、材质包的 pack.mcmeta 路径），改了游戏就认不出来，
   所以那两个只显示默认名、输入框禁用。
3. **落点一目了然**：不再只丢一串路径给人看 —— 上面写清「进哪个游戏的哪个
   文件夹」，下面直接把那个文件夹里现在的东西列出来（第六十轮）。保存完成
   后由调用方打开**内置**文件管理器并高亮刚写进去的文件，不弹系统资源管理器。
"""
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QStyle, QVBoxLayout,
                               QWidget)

from ..core import installer
from . import theme
from .dialogs import _DialogBase
from .widgets import ElideLabel, IconButton, Toast


LIST_ROWS = 200          # 目标文件夹里东西再多也只铺这么多行


def _entries(path):
    """目标文件夹现在的样子 -> (dirs, files, 总条数, 错误信息)

    第六十轮：保存窗口不再只显示一串路径 —— 直接把那个文件夹里的内容列出来，
    玩家一眼就能看到「我要放进去的地方现在有什么」。
    """
    dirs, files = [], []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    (dirs if e.is_dir() else files).append(e.name)
                except OSError:
                    continue
    except FileNotFoundError:
        return [], [], 0, ""
    except OSError as exc:
        return [], [], 0, str(exc)
    dirs.sort(key=str.lower)
    files.sort(key=str.lower)
    return dirs, files, len(dirs) + len(files), ""


class ContentSaveDialog(_DialogBase):
    """确认「装到哪个游戏的哪个文件夹」+ 模组/数据包改名。

    `kind`          mod / datapack / shader / resourcepack
    `default_name`  下载下来的原始文件名
    `dest_dir`      目标文件夹（调用方已按所选游戏算好）
    `game_label`    所选游戏的显示名（空 = 还没选游戏）
    """

    RENAMEABLE = ("mod", "datapack")          # 光影 / 材质包不许改名
    CN = {"mod": "模组", "datapack": "数据包",
          "shader": "光影", "resourcepack": "材质包"}

    def __init__(self, parent, kind, default_name, dest_dir, game_label="",
                 same_name=""):
        cn = self.CN.get(kind, "内容")
        super().__init__(parent, width=600, title=f"保存{cn}")
        self.kind = kind
        self.default_name = os.path.basename(str(default_name or "")) or "download"
        self.dest_dir = str(dest_dir or "")
        # 目标文件夹里已经有同名文件 → 提前警告一句（保存会覆盖）
        self._same = str(same_name or "")
        if not self._same and self.dest_dir:
            try:
                if os.path.isfile(os.path.join(self.dest_dir, self.default_name)):
                    self._same = self.default_name
            except OSError:
                pass

        # ---- 落点 ----
        # 完整路径一行放不下（会省略号截断，末尾那个 mods / shaderpacks 恰好被吃掉），
        # 所以把子文件夹名单独写进标题句里 —— 玩家最想知道的就是「进哪个文件夹」
        sub = os.path.basename(self.dest_dir.rstrip("\\/")) or "游戏目录"
        who = QLabel(f"将装到所选游戏「{game_label}」的 {sub}/ 文件夹"
                     if game_label else
                     f"还没选要启动的游戏 · 将装到官方启动器目录的 {sub}/")
        wf = QFont(theme.FONT)
        wf.setPixelSize(12.5)
        wf.setBold(True)
        who.setFont(wf)
        who.setWordWrap(True)
        who.setStyleSheet("color:%s;background:transparent;"
                          % ("#2f7cf6" if game_label else "#8a6d3b"))
        self.body.addWidget(who)

        box = QWidget()
        box.setAttribute(Qt.WA_StyledBackground, True)
        box.setStyleSheet("QWidget{background:rgba(255,255,255,150);"
                          "border-radius:10px;}")
        bl = QHBoxLayout(box)
        bl.setContentsMargins(10, 8, 8, 8)
        bl.setSpacing(8)
        self.path_label = ElideLabel(self.dest_dir)
        self.path_label.setToolTip(self.dest_dir)
        self.path_label.setStyleSheet("color:#3a4757;font-size:11.5px;"
                                      "background:transparent;")
        bl.addWidget(self.path_label, 1)
        btn_open = IconButton("refresh", 26, 13, color="#5a6675")
        btn_open.setToolTip("重新读一遍这个文件夹的内容")
        btn_open.clicked.connect(self._reload_list)
        bl.addWidget(btn_open, 0, Qt.AlignVCenter)
        self.body.addWidget(box)

        # ---- 这个文件夹里现在有什么（第六十轮：不再只显示一串路径）----
        self.list = QListWidget()
        self.list.setFrameShape(QListWidget.NoFrame)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setUniformItemSizes(True)
        self.list.setFixedHeight(132)
        self.list.setStyleSheet(
            "QListWidget{background:rgba(255,255,255,150);border:none;"
            "border-radius:10px;padding:4px;font-size:12px;color:#1f2733;}"
            "QListWidget::item{padding:5px 8px;border-radius:8px;}"
            "QListWidget::item:hover{background:rgba(47,127,246,20);}"
            "QListWidget::item:selected{background:rgba(47,127,246,42);"
            "color:#152238;}")
        self.body.addWidget(self.list)
        self.lb_list = QLabel("")
        self.lb_list.setStyleSheet("color:#7b8794;font-size:11px;"
                                   "background:transparent;")
        self.lb_list.setWordWrap(True)
        self.body.addWidget(self.lb_list)
        self._reload_list()

        # ---- 文件名 ----
        row = QHBoxLayout()
        row.setSpacing(8)
        lb = QLabel("文件名")
        lb.setStyleSheet("color:#4c5866;font-size:12px;background:transparent;")
        lb.setFixedWidth(52)
        row.addWidget(lb)
        self.edit = QLineEdit(self.default_name)
        self.edit.setFixedHeight(34)
        row.addWidget(self.edit, 1)
        self.body.addLayout(row)

        self.renameable = kind in self.RENAMEABLE
        if self.renameable:
            tip = "可以改名保存（扩展名会自动保留）"
        else:
            # 光影/材质包的文件名被自身内容引用，改名游戏就认不出来
            self.edit.setEnabled(False)
            self.edit.setStyleSheet("color:#8a95a3;")
            tip = f"{cn}只能用原文件名保存（改了游戏会认不出来）"
        self.lb_tip = QLabel(tip)
        self.lb_tip.setStyleSheet("color:%s;font-size:11px;background:transparent;"
                                  % ("#6b7684" if self.renameable else "#8a6d3b"))
        self.lb_tip.setWordWrap(True)
        self.body.addWidget(self.lb_tip)

        if self._same:
            warn = QLabel(f"注意：这里已经有一个「{self._same}」，保存会覆盖它")
            warn.setStyleSheet("color:#b3372b;font-size:11px;background:transparent;")
            warn.setWordWrap(True)
            self.body.addWidget(warn)

        self.add_buttons([("取消", "ghost", self.reject),
                          ("保存", "accent", self._ok)])
        self.edit.setFocus()
        self.edit.selectAll()

    # ------------------------------------------------------------ 结果
    def file_name(self) -> str:
        """最终要落盘的文件名（洗过：非法字符换下划线、扩展名只补不改）"""
        if not self.renameable:
            return self.default_name
        return installer.safe_dest_name(self.edit.text(), self.default_name)

    def _ok(self):
        if self.renameable and not self.edit.text().strip():
            Toast.show_toast(self.window(), "文件名不能为空", ms=2600, kind="error")
            self.edit.setFocus()
            return
        self.accept()

    def _reload_list(self):
        """把目标文件夹里现在的内容铺出来（文件夹已存在才铺）。

        第六十轮：这里就是「内置文件管理器」的那一小块 —— 玩家点「保存」之前
        就能看到目标文件夹里已经有哪些 jar，不用先跑去开资源管理器确认。
        """
        self.list.clear()
        path = (self.dest_dir or "").strip()
        dirs, files, total, err = ([], [], 0, "") if not path else _entries(path)
        if not path or not os.path.isdir(path):
            self.lb_list.setText(
                "这个文件夹还没建出来 —— 保存时会自动创建" if path else
                "还没确定要存到哪")
            return
        if err:
            self.lb_list.setText("这个文件夹读不出来：%s" % err[:80])
            return
        style = self.style()
        ic_dir = style.standardIcon(QStyle.SP_DirIcon)
        ic_file = style.standardIcon(QStyle.SP_FileIcon)
        shown = 0
        for name in dirs:
            if shown >= LIST_ROWS:
                break
            it = QListWidgetItem(ic_dir, name)
            it.setData(Qt.UserRole, "dir")
            self.list.addItem(it)
            shown += 1
        for name in files:
            if shown >= LIST_ROWS:
                break
            it = QListWidgetItem(ic_file, name)
            it.setData(Qt.UserRole, "file")
            self.list.addItem(it)
            shown += 1
        # 已经存在同名文件的那一行选中它，红色警告在上面，这里给个视觉落点
        picked = ""
        if self._same:
            for i in range(self.list.count()):
                it = self.list.item(i)
                if (it.data(Qt.UserRole) == "file"
                        and it.text().lower() == self._same.lower()):
                    self.list.setCurrentItem(it)
                    self.list.scrollToItem(it)
                    picked = self._same
                    break
        if not picked:
            self.list.clearSelection()
            self.list.setCurrentRow(-1)
        if total == 0:
            self.lb_list.setText("这个文件夹现在是空的 —— 保存后这里会多出 1 个文件")
        else:
            more = ("；只列出前 %d 项" % LIST_ROWS) if total > shown else ""
            self.lb_list.setText("这个文件夹里现在有 %d 项%s"
                                 % (total, more))

    def _open_dir(self):
        """（保留给探针 / 老调用）转去内置或系统的文件管理器"""
        from .fspicker import pick_view
        pick_view(self, "文件在这里", self.dest_dir)


def ask(parent, kind, default_name, dest_dir, game_label="", same_name=""):
    """弹一次，返回文件名（取消返回空串）。调用方据此决定「装还是取消」。"""
    d = ContentSaveDialog(parent, kind, default_name, dest_dir, game_label,
                          same_name=same_name)
    if d.exec() == ContentSaveDialog.Accepted:
        return d.file_name()
    return ""
