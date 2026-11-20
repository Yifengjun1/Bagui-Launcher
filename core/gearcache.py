# -*- coding: utf-8 -*-
"""概览缓存（第六十七轮）：给「独立游戏设置 → 概览」里的文件夹数据一块临时落盘区。

为什么要有它
------------
概览里展开模组清单时，一行一个文件：目录要 `scandir` + 每个文件 `stat`，图标还要
**打开每个 jar / zip 读 `assets/<id>/icon.png`**（一个约 10ms）。两百多个模组一边滚
一边读，滑到哪卡到哪 —— 主线程被 GIL 抢、磁盘还在转。

于是把两样东西临时落到磁盘：
  · **目录清单**（名字 / 大小 / 是否禁用）：一次 scandir 的结果，重开面板不用再扫；
  · **图标字节**（原封不动的 png）：下次直接读这个小文件，不再开 jar。

放在哪
------
`<安装目录>\\cache\\`，按来源分两摊（用户要求的目录结构）：
    cache/import_cache/   「已导入」的游戏（别的启动器里指过来的）
    cache/app_cache/      应用自己目录里的游戏（下载 / 整合包）
判定见 `scope_for()`：游戏目录落在 `paths.app_game_dir()` / `official_game_dir()`
里算「应用」，其余都算「已导入」。**不按 kind 判** —— kind 只说明是版本还是实例，
同一个 instances 目录既可能被导入也可能被下载。

生命周期
--------
纯临时：设置窗**取消 / 保存 / 直接关掉**时整摊清空（`clear()`），下次打开重新缓存。
所以这里**不做长期失效策略**，只靠 (路径, mtime) 键保证「文件换了就不会读到旧图」；
清单再用目录 mtime 做一次校验，变了就重扫。

约定
----
· 纯逻辑，不 import Qt（探针可以直接测，工作线程也能随便调）；
· **任何异常都当「没有缓存」**：缓存坏了最多慢一点，绝不能让概览打不开；
· 写盘一律 `.part` + `os.replace`，主线程一边读一边写的半截文件不会被读到。
"""
import hashlib
import json
import os

from . import modfiles, paths

CACHE_DIR = "cache"
SCOPES = {"import": "import_cache", "app": "app_cache"}
DEFAULT_SCOPE = "app"

LIST_LIMIT = 300            # 跟 FileListPanel 的老上限一致
LIST_VER = 2                # 清单格式版本：2 = 行里带 mtime（第六十七轮补丁）
MAX_ICON_BYTES = 1024 * 1024
MAX_ICON_FILES = 3000       # 一次会话里不该到这个数，超了按最旧的删


# ------------------------------------------------------------------ 目录
def root(create: bool = True) -> str:
    """`<安装目录>\\cache`（安装目录 = Osmanthus flower）"""
    p = os.path.join(paths.install_root(), CACHE_DIR)
    if create:
        try:
            os.makedirs(p, exist_ok=True)
        except OSError:
            return ""
    return p


def scope_dir(scope: str, create: bool = True) -> str:
    """某一摊（import_cache / app_cache）的目录"""
    name = SCOPES.get(str(scope or "").lower(), SCOPES[DEFAULT_SCOPE])
    r = root(create)
    if not r:
        return ""
    p = os.path.join(r, name)
    if create:
        try:
            os.makedirs(os.path.join(p, "icons"), exist_ok=True)
        except OSError:
            return ""
    return p


def scope_for(item) -> str:
    """这个游戏的内容缓存在哪一摊。

    判据是**游戏目录在哪**：在我们自己的 `.minecraft` / official 目录底下就是「应用」，
    其余（Prism、官方启动器、随便指的文件夹）都是「已导入」。
    """
    it = item or {}
    p = str(it.get("game_dir") or it.get("path") or it.get("dir") or "")
    if not p:
        return DEFAULT_SCOPE
    p = _norm(p)
    for base in (paths.app_game_dir(), paths.official_game_dir()):
        b = _norm(base) if base else ""
        if b and (p == b or p.startswith(b + os.sep)):
            return "app"
    return "import"


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:20]


def _norm(path: str) -> str:
    """算缓存键用的路径。

    **故意不用 `realpath`**：它在 Windows 上要逐级解析、一次 2~3ms，而这里每次列表 /
    每个图标都要算一次 —— 213 个模组光算键就 0.6 秒，缓存反而比不缓存还慢。
    路径都是我们自己拼出来的绝对路径，`abspath` + `normcase` 足够当键了
    （万一有人从 junction 和真实路径各进来一次，最多是一次缓存未命中，不会读到错的东西）。
    """
    return os.path.normcase(os.path.abspath(str(path or "")))


def _write(path: str, data: bytes):
    """原子写：先 .part 再 replace（读的一侧永远看不到半截文件）"""
    tmp = f"{path}.{os.getpid()}.part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# ------------------------------------------------------------------ 目录清单
def _list_path(folder: str, scope: str) -> str:
    d = scope_dir(scope)
    if not d:
        return ""
    return os.path.join(d, "list_" + _hash(_norm(folder)) + ".json")


def _folder_sig(folder: str) -> str:
    """目录指纹：mtime_ns + 条目数。改了名 / 加了文件都会变"""
    try:
        st = os.stat(folder)
        return f"{int(getattr(st, 'st_mtime_ns', 0))}"
    except OSError:
        return ""


def list_files(folder: str, scope: str, limit: int = LIST_LIMIT) -> tuple:
    """带缓存的目录清单。返回 (rows, 被截掉的个数)。

    命中缓存时**完全不碰磁盘**（除了那一次 `os.stat` 拿 mtime），
    所以「禁用两个模组后刷新」「切页再切回来」都不会再扫一遍目录。

    缓存里存的是**全量（最多 LIST_LIMIT 条）+ 真实总数**，`limit` 只在返回时切 ——
    否则第一次按 300 条缓存，后面按 10 条再要一次就会拿到 300 条（上一版就是这么错的）。
    """
    folder = str(folder or "")
    limit = max(1, int(limit))
    rows, cut, total = [], 0, 0
    if not folder:
        return rows, cut
    lp = _list_path(folder, scope)
    sig = _folder_sig(folder)
    if lp and sig:
        try:
            with open(lp, "r", encoding="utf-8") as f:
                got = json.load(f)
            if (str(got.get("folder") or "") == _norm(folder)
                    and str(got.get("sig") or "") == sig
                    and int(got.get("v") or 0) == LIST_VER
                    and isinstance(got.get("rows"), list)):
                allrows = got["rows"]
                try:
                    total = int(got.get("total") or len(allrows))
                except (TypeError, ValueError):
                    total = len(allrows)
                out = allrows[:limit]
                return out, max(0, total - len(out))
        except (OSError, ValueError, TypeError):
            pass
    rows, cut = modfiles.list_files(folder, limit=LIST_LIMIT)
    total = len(rows) + cut
    if lp and sig:
        try:
            _write(lp, json.dumps({"v": LIST_VER, "folder": _norm(folder),
                                   "sig": sig, "rows": rows, "total": total,
                                   "cut": cut},
                                  ensure_ascii=False).encode("utf-8"))
        except (OSError, TypeError, ValueError):
            pass
    out = rows[:limit]
    return out, max(0, total - len(out))


# ------------------------------------------------------------------ 图标字节
def _icon_key(path: str, mtime) -> str:
    """图标键：路径（**去掉 .disabled**）+ mtime。

    去掉 `.disabled` 是有用的：改名是 `os.rename`，mtime 不变，于是
    「禁用 → 启用」来回切都命中同一条缓存，不会每切一次就重开 jar。
    """
    full = str(path or "")
    base = os.path.basename(full)
    if modfiles.is_disabled(base):
        full = os.path.join(os.path.dirname(full), modfiles.enabled_name(base))
    try:
        mt = str(int(float(mtime)))
    except (TypeError, ValueError):
        mt = ""
    return _hash(_norm(full) + "@" + mt)


def icon_get(path: str, mtime, scope: str):
    """读缓存图标。返回 bytes；`b""` = 明确知道「这个包没有图标」；None = 没缓存过"""
    d = scope_dir(scope, create=False)
    if not d:
        return None
    key = _icon_key(path, mtime)
    png = os.path.join(d, "icons", key + ".png")
    none = os.path.join(d, "icons", key + ".none")
    try:
        st = os.stat(png)
        if st.st_size and st.st_size <= MAX_ICON_BYTES:
            with open(png, "rb") as f:
                return f.read()
    except OSError:
        pass
    if os.path.isfile(none):
        return b""
    return None


def icon_put(path: str, mtime, scope: str, data):
    """把图标字节写进缓存（data 为空 = 记一个「没图标」的标记）"""
    d = scope_dir(scope, create=False)
    if not d:
        return False
    icons = os.path.join(d, "icons")
    try:
        os.makedirs(icons, exist_ok=True)
    except OSError:
        return False
    key = _icon_key(path, mtime)
    png = os.path.join(icons, key + ".png")
    none = os.path.join(icons, key + ".none")
    try:
        if data:
            if len(data) > MAX_ICON_BYTES:
                return False
            _write(png, data)
            if os.path.isfile(none):
                os.remove(none)
        else:
            if not os.path.isfile(none):
                _write(none, b"")
        return True
    except OSError:
        return False


def _prune_icons(scope: str):
    """图标缓存太多时按最旧的删（正常用不到，兜底而已）"""
    d = os.path.join(scope_dir(scope, create=False) or "", "icons")
    try:
        items = [os.path.join(d, n) for n in os.listdir(d)]
    except OSError:
        return
    if len(items) <= MAX_ICON_FILES:
        return
    try:
        items.sort(key=lambda p: os.path.getmtime(p))
        for p in items[:len(items) - MAX_ICON_FILES]:
            os.remove(p)
    except OSError:
        pass


# ------------------------------------------------------------------ 生命周期
NOTE_NAME = "缓存说明.txt"
_NOTE = """这个文件夹是什么？
====================

八桂启动器用来「看一眼摸组文件夹时不卡」的临时缓存，随时可以整个删掉，
删了下次打开设置会自己再建。

cache\\import_cache\\   「已导入」的游戏（从别的启动器接管过来的）
cache\\app_cache\\      本启动器自己目录里的游戏（下载 / 整合包装的）

里面只有两样东西：
  · list_*.json   文件夹清单（有哪些文件、多大、有没有被禁用）
  · icons\\       模组 / 材质包自带的图标，缩到 64 像素存一份

生命周期：打开「独立游戏设置 → 概览」展开文件夹时写进去；设置窗
**取消 / 保存 / 直接关掉**之后自动清空内容（空目录留着），
下次再打开重新缓存。启动器被强杀时可能留下一点，下次开机也会清掉。

所以：看到这里是空的，是正常的；看到有文件，说明你现在正开着设置窗。
"""


def ensure_dirs() -> list:
    """把 `cache\\` 两条支路 + `icons\\` 建出来，再放一份说明。

    第六十七轮补丁：玩家反馈「安装目录里也没有 cache」—— 以前这三个目录是
    第一次展开概览时才顺带建的，没打开过设置就一个都看不见。现在开机就建好，
    里面放一份说明.txt 讲清它是干什么的（用户本来就问了这句话）。
    """
    out = []
    for s in SCOPES:
        d = scope_dir(s, create=True)
        if d:
            out.append(d)
    r = root(create=True)
    if r:
        p = os.path.join(r, NOTE_NAME)
        try:
            if not os.path.isfile(p):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(_NOTE)
        except OSError:
            pass
    return out


def reset():
    """开机用：先清上次强杀留下的内容，再把目录骨架建好"""
    clear()
    ensure_dirs()


def info(scope: str = "") -> dict:
    """给探针 / 设置页看的一眼统计：有几个文件、占多大"""
    out = {}
    for s in ([scope] if scope else list(SCOPES)):
        d = scope_dir(s, create=False)
        n = size = 0
        for base, _dirs, files in os.walk(d) if d else []:
            for f in files:
                n += 1
                try:
                    size += os.path.getsize(os.path.join(base, f))
                except OSError:
                    pass
        out[s] = {"dir": d, "files": n, "bytes": size}
    return out


def clear(scope: str = ""):
    """清空缓存内容（取消 / 保存设置时调）。

    只清**内容**：`cache\\`、`import_cache\\`、`app_cache\\`、`icons\\` 这几层目录留着 ——
    用户在安装目录里能看见这块缓存区，但点进去是空的，下次打开又填上。
    删不动就静默（文件被占用也不该让设置窗保存失败）。
    """
    targets = [scope] if scope else list(SCOPES)
    for s in targets:
        d = scope_dir(s, create=False)
        if not d or not os.path.isdir(d):
            continue
        icons = os.path.join(d, "icons")
        if os.path.isdir(icons):
            try:
                for n in os.listdir(icons):
                    p = os.path.join(icons, n)
                    if os.path.isfile(p):
                        os.remove(p)
            except OSError:
                pass
        try:
            for n in os.listdir(d):
                if n.startswith("list_") or n.endswith(".part"):
                    os.remove(os.path.join(d, n))
        except OSError:
            pass
