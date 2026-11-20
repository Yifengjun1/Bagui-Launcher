# -*- coding: utf-8 -*-
"""导出整合包：把本机某个游戏实例打成一个能分发的 zip。

产物结构（尽量贴近业界习惯，玩家拿别的启动器也能看懂）::

    manifest.json          ← 版本、加载器、模组清单（带 sha1）
    README.txt             ← 给拿到包的人看的三行说明
    overrides/mods/*.jar   ← 实际内容都在 overrides/ 下
    overrides/config/...
    overrides/options.txt

为什么是 `overrides/`：整合包都用这个约定，
以后想再兼容它们的导入器，只需要换掉 manifest 那一段，不必重新搬文件。

三条纪律：
1. **只读源目录**：导出过程绝不改动游戏文件（连时间戳都不碰）；
2. **符号链接 / junction 一律跳过**：本机 `.minecraft` 常是 junction（→D:\\MC），
   跟着走会把几十 G 的真实游戏目录全打进去，而且可能绕圈；
3. **先算总量再拷**：玩家点「导出」时要立刻看到「共 N 个文件 / X GB」，
   而不是进度条走到 60% 才发现空间不够；
4. **绝不导出启动器本体**（第一百零五轮）：PCL-CE 的 `includePcl` 双层压缩
   不适用于我们，不把 exe / 启动器私有配置塞进每个整合包。

第一百零五轮照 PCL-CE `PageInstanceExport.*` 补的三件可引用部分：
· **规则引擎**：glob + `!` 反选 + 顺序覆盖，`export_config.txt` 可导入导出；
· **动态其它文件夹**：固定 4 段会漏 `kubejs/` / `scripts/` / `defaultconfigs/`
  / `openloader/`，现在扫出来给玩家自己勾（默认不勾、先显示体积）；
· **可选标准 `.mrpack`**：新增格式，**不替换** `bagui-pack/1`（否则自家导入器
  回归基线全崩）。默认全量内嵌文件，`downloads` 留空，绝不因为「查不到 CDN
  地址」就删 overrides 副本 —— 第 95 轮「不等价就不许静默降级」。
"""
import fnmatch
import hashlib
import itertools
import json
import os
import time
import zipfile

# ---------------------------------------------------------------- 可导出内容
SECTIONS = (
    ("mods", "模组", "mods 里的 jar（含 .disabled）"),
    ("config", "配置", "config 目录、options.txt 等"),
    ("saves", "存档", "saves 里的世界（体积可能很大）"),
    ("resources", "材质 / 光影", "resourcepacks 与 shaderpacks"),
)
SECTION_KEYS = tuple(k for k, _n, _d in SECTIONS)
DEFAULT_SECTIONS = ("mods", "config")

# 这些目录/文件对别人没用，还会让包变大：日志、崩溃报告、缓存、版本隔离残留
_SKIP_NAMES = {
    "logs", "crash-reports", "crashes", ".cache", "cache", "downloads",
    "backups", "backup", "temp", "tmp", ".git", "__pycache__",
    "screenshots", "replay_recordings", "replay_videos", "debug",
    "patchouli_books_backup", "structurecachev1", ".fabric", "avatar-cache",
    "cosmetic-cache", "mcef-libraries", "mcef-cache", ".connector",
}
# `assets` / `versions` / `libraries` / `natives` / `runtime` **不放全局跳过表**：
# PCL-CE L937 只在「当前文件夹就是游戏根」这一层跳过它们。做成按名字全局跳过
# 会把整合包里 `kubejs/assets/`、`scripts/assets/` 这些正经内容一起误杀
# （第 105 轮端到端实测：勾了 kubejs 却一个文件都没多出来）。
# 根目录那一层由 _COVERED_FOLDERS 在 extra_folders() 里挡住。
_SKIP_SUFFIX = (".log", ".log.gz", ".tmp", ".part", ".lock", ".dat_old",
                ".bakacoreinfo")
# 精确文件名（PCL-CE `GetAllRules()` L537-542 的固定排除）
_SKIP_FILES = {"hmclversion.cfg", "log4j2.xml", "launcher_profiles.json",
               "usercache.json", "usernamecache.json"}

# 默认排除规则：初始「保留」，命中 `!` 才丢。用规则而不是再堆一层 if，
# 是为了让 `export_config.txt` 能把默认规则和玩家自定义规则一起改。
DEFAULT_EXCLUDE_RULES = (
    "!*.log", "!*.log.gz", "!*.dat_old", "!*.BakaCoreInfo",
    "!hmclversion.cfg", "!log4j2.xml",
    "!mods/*.disabled", "!mods/*.old",
    "!mods/.connector/**", "!mods/mcef-libraries/**", "!mods/mcef-cache/**",
    "!**/logs/**", "!**/crash-reports/**", "!**/cache/**",
)

# 动态扫描时该跳过的目录（照 PCL-CE L175-208 的思路，但按我们的能力重划）：
# · 我们固定 4 段已经覆盖的（mods / config / saves / 资源包光影），别重复列；
# · 又大又没用的缓存 / 游戏共享体积（PCL-CE 标「始终跳过」的那批）。
# **PCL-CE 自己有专门复选框的 kubejs / scripts / defaultconfigs / openloader /
# patchouli_books / xaero 等，我们没有那套选项，所以必须放出来让玩家勾** ——
# 这正是「固定 4 段会漏掉整合包关键脚本与配置」那个缺口的解药，照抄它的
# coveredFolders 反而会把缺口原样保留下来。
_COVERED_FOLDERS = frozenset({
    "mods", "config", "saves", "resourcepacks", "texturepacks", "shaderpacks",
    "assets", "versions", "libraries", "natives", "runtime",
    "structurecachev1", ".fabric", ".git", "avatar-cache", "cosmetic-cache",
    "screenshots", "schematics", "replay_recordings", "replay_videos",
    "logs", "crash-reports", "crashes", "cache", ".cache", "temp", "tmp",
    "backups", "backup", "downloads", "pcl", "bgl_datat_config",
    "__pycache__", "mcef-libraries", "mcef-cache", ".connector",
})
_COVERED_PREFIXES = ()
_COVERED_SUFFIXES = ("-natives",)

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_CONFIG_SEP = "=" * 62


# ---------------------------------------------------------------- 规则引擎
def standardize_lines(raw, add_suffix_star=True) -> list:
    """规则文本行 → 规整后的规则列表（照 PCL-CE `StandardizeLines()`）。

    空行 / `#` / `=` 开头跳过；反斜杠统一正斜杠；以 `/` 结尾的目录补 `**`。
    """
    out = []
    for line in raw or []:
        s = str(line).strip()
        if not s or s.startswith("#") or s.startswith("="):
            continue
        s = s.replace("\\", "/")
        if add_suffix_star and s.endswith("/"):
            s += "**"
        out.append(s)
    return out


def like_string(path, pattern) -> bool:
    """大小写不敏感 glob；`#` 当单数字（PCL-CE `LikeString()` 的约定）。

    `fnmatch` 的 `*` 本来就跨 `/`，`**` 折成 `*` 即可覆盖整棵子树，不新增依赖。
    但 `**/` 必须还能匹配**零层**目录：玩家写 `!**/kubejs/**` 想排除的正是
    游戏根下那个 `kubejs/`，若只折成 `*/kubejs/*`，`kubejs/a.js` 前面没有
    那一层斜杠就永远命中不了（第 105 轮验收实测）。这里把每个 `**/` 分别
    按「吃掉若干层」和「一层都不吃」各试一次，规则表很短，组合数有上限。
    """
    s = str(path or "").replace("\\", "/").lower()
    p = str(pattern or "").replace("\\", "/").replace("#", "[0-9]").lower()
    if not p:
        return False
    parts = p.split("**/")
    variants = {p}
    if 1 < len(parts) <= 5:
        for bits in itertools.product((0, 1), repeat=len(parts) - 1):
            q = parts[0]
            for i, bit in enumerate(bits):
                q += ("**/" if bit else "") + parts[i + 1]
            variants.add(q)
    for v in variants:
        q = v
        while "**" in q:
            q = q.replace("**", "*")
        if fnmatch.fnmatchcase(s, q):
            return True
    return False


def apply_rules(rel, rules, default_keep=True) -> bool:
    """顺序覆盖地判一条相对路径该不该留（PCL-CE L948-952）。

    `!` = 反选：命中就丢；正向命中就留。**后写的赢**，所以玩家能在排除表
    后面再补一条正向规则把某个文件捞回来。
    """
    keep = bool(default_keep)
    for r in rules or []:
        rule = str(r).strip()
        if not rule:
            continue
        revert = rule.startswith("!")
        pat = rule[1:] if revert else rule
        if like_string(rel, pat):
            keep = not revert
    return keep


def effective_rules(rules=None) -> list:
    """玩家没给规则就用默认排除表。"""
    if rules is None:
        return list(DEFAULT_EXCLUDE_RULES)
    return standardize_lines(rules if isinstance(rules, (list, tuple)) else [rules])


def dump_config_text(rules=None, extra=()) -> str:
    """导出成 `export_config.txt`：规则段 + 追加文件夹段，可再导入复用。"""
    lines = [
        "# 八桂启动器 · 整合包导出规则",
        "# 一行一条；! 开头表示排除；按顺序覆盖，后写的赢；# 是注释",
        "# 路径用 /，支持 * 与目录结尾的 /（自动补 **）",
        "", _CONFIG_SEP, "规则", _CONFIG_SEP,
    ]
    lines.extend(effective_rules(rules))
    lines += ["", _CONFIG_SEP, "追加文件夹", _CONFIG_SEP,
              "# 每行一个要一起打包的文件夹名（相对游戏目录）"]
    for e in extra or []:
        lines.append(str(e))
    return "\n".join(lines) + "\n"


def load_config_text(text) -> tuple:
    """读回 `export_config.txt` → (规则, 追加文件夹)。坏文本不抛异常。

    **不许静默关掉安全网**：空文件、随手选错的文件、没有「规则」段的旧文本
    若一条规则都没解析出来，一律退回默认排除表。否则玩家导入一个空 txt，
    `.log` / 缓存 / 崩溃报告就全被打包进去了（第 105 轮验收实测）。
    只有玩家**明确写了「规则」段且留空**，才当作「我就是要不排除任何东西」。
    """
    raw = str(text or "")
    rules, extra, seg, rules_seen = [], [], "", False
    try:
        for line in raw.splitlines():
            s = line.strip()
            if s == _CONFIG_SEP:
                continue
            if s in ("规则", "追加文件夹"):
                seg = "rules" if s == "规则" else "extra"
                if seg == "rules":
                    rules_seen = True
                continue
            if not s or s.startswith("#"):
                continue
            (rules if seg == "rules" else extra).append(s)
        if not rules and not seg:        # 没有分段的老文本 = 纯规则
            rules = [l.strip() for l in raw.splitlines()
                     if l.strip() and not l.strip().startswith("#")]
    except Exception:                                       # noqa: BLE001
        return list(DEFAULT_EXCLUDE_RULES), []
    if not rules and not rules_seen:
        return list(DEFAULT_EXCLUDE_RULES), [str(e) for e in extra]
    return standardize_lines(rules), [str(e) for e in extra]


def _is_link(path: str) -> bool:
    """junction / 符号链接判定。

    `os.path.islink()` 对 junction 返回 False（本机 `.minecraft` 就是 junction），
    只能问 Windows 拿 FILE_ATTRIBUTE_REPARSE_POINT。非 Windows 退回 islink。
    """
    try:
        if os.path.islink(path):
            return True
    except OSError:
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes
        get = ctypes.windll.kernel32.GetFileAttributesW
        get.argtypes = [ctypes.c_wchar_p]
        get.restype = ctypes.c_uint32
        attrs = get(path)
        return attrs not in (0xFFFFFFFF, 0) and bool(
            attrs & _FILE_ATTRIBUTE_REPARSE_POINT)
    except Exception:
        return True          # 查不出来就当链接跳过：宁可少打包，不可绕死


def _skip_file(name: str) -> bool:
    low = str(name).lower()
    if low in _SKIP_NAMES or low in _SKIP_FILES:
        return True
    return low.endswith(_SKIP_SUFFIX)


def _walk_files(root: str, limit: int = 40000):
    """遍历目录下的文件（跳过链接、日志、缓存），返回绝对路径列表"""
    out = []
    if not root or not os.path.isdir(root):
        return out
    root = os.path.abspath(root)
    for cur, dirs, files in os.walk(root):
        # 就地剪枝：链接目录和日志目录都不进去
        dirs[:] = [d for d in dirs
                   if not _skip_file(d)
                   and not _is_link(os.path.join(cur, d))]
        for f in files:
            if _skip_file(f):
                continue
            p = os.path.join(cur, f)
            if _is_link(p):
                continue
            out.append(p)
            if len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------- 来源目录
def _dirs_for(section: str, work: str, root: str) -> list[tuple[str, str]]:
    """某一段内容要从哪几个目录取，映射到 overrides 下的哪个子目录

    返回 [(源目录 或 单文件, overrides 内相对前缀)]。

    `work` 是这个游戏**实际生效**的目录（隔离开着时就是实例目录），`root`
    是游戏根目录。隔离实例的 mods / config / saves 都在实例目录里，只看根目录
    会把整合包导出成空包；反过来关掉隔离的版本，内容又在根目录。两个都收，
    同名文件以更靠近这个游戏的那份（work）优先。
    """
    same = os.path.normcase(os.path.abspath(work or ".")) == \
        os.path.normcase(os.path.abspath(root or "."))
    bases = [work] + ([] if same else [root])
    if section in ("mods", "saves", "resources"):
        names = ("mods",) if section == "mods" else (
            ("saves",) if section == "saves" else ("resourcepacks", "shaderpacks"))
        return [(os.path.join(b, n), n) for n in names for b in bases if b]
    if section == "config":
        out = [(os.path.join(b, "config"), "config") for b in bases if b]
        seen = set()
        for name in ("options.txt", "servers.dat"):
            for b in bases:
                if not b:
                    continue
                p = os.path.join(b, name)
                if os.path.isfile(p) and name not in seen:
                    seen.add(name)
                    out.append((p, ""))
                    break        # 同一个文件只取最靠近的那个
        return out
    return []


def _bases(item) -> tuple:
    """（实际生效目录, 游戏根目录），两处都要收：隔离 / 非隔离实例都能导出。"""
    from . import launchprefs
    it = item or {}
    return launchprefs.work_dir(it), launchprefs.game_root(it)


def _all_bases(item) -> list:
    work, root = _bases(item)
    same = os.path.normcase(os.path.abspath(root or ".")) == \
        os.path.normcase(os.path.abspath(work or "."))
    return [b for b in ([work] + ([] if same else [root])) if b]


def extra_folders(item) -> list:
    """扫出「固定四段没覆盖到」的其它文件夹，供玩家自己勾（默认不勾）。

    照 PCL-CE `ReloadOtherFolders()` L163-231，另补两条我们的纪律：
    · junction / symlink 一律不进（`_is_link`），否则会把整个真实游戏目录列出来；
    · 只读地数文件数与字节数，玩家勾之前就能看到「这个文件夹有多大」。
    """
    seen, out = set(), []
    for b in _all_bases(item):
        if not os.path.isdir(b) or _is_link(b):
            continue
        try:
            names = os.listdir(b)
        except OSError:
            continue
        for name in names:
            low = name.lower()
            if name in seen or low in _COVERED_FOLDERS or _skip_file(name):
                continue
            if low.startswith(_COVERED_PREFIXES) or low.endswith(_COVERED_SUFFIXES):
                continue
            p = os.path.join(b, name)
            if not os.path.isdir(p) or _is_link(p):
                continue
            seen.add(name)
            files = _walk_files(p)
            if not files:
                continue
            total = 0
            for f in files:
                try:
                    total += os.path.getsize(f)
                except OSError:
                    pass
            out.append({"name": name, "rel": name.replace("\\", "/"),
                        "path": p, "files": len(files), "bytes": total})
    out.sort(key=lambda d: d["name"].lower())
    return out


def _collect(item, sections, extra=(), rules=None) -> list:
    """把 (zip 内相对路径, 源文件绝对路径) 列出来；同名只留第一个。

    `extra` 是玩家勾的「其它文件夹」名列表；`rules` 是导出规则表。
    规则作用在 **overrides 内的相对路径**上，与 PCL-CE L945-952 一致。
    """
    work, root = _bases(item)
    seen = {}
    for sec in sections:
        for src, prefix in _dirs_for(sec, work, root):
            if os.path.isfile(src):                    # options.txt 这类单文件
                arc = "overrides/" + os.path.basename(src)
                seen.setdefault(arc, src)
                continue
            for p in _walk_files(src):
                rel = os.path.relpath(p, src).replace("\\", "/")
                arc = "overrides/" + (prefix + "/" + rel if prefix else rel)
                seen.setdefault(arc, p)
    for name in extra or []:
        rel = str(name).replace("\\", "/").strip("/")
        # 只认单层目录名：不许写 ../ 或绝对路径越出游戏目录
        if not rel or "/" in rel or rel in (".", "..") or os.path.isabs(rel):
            continue
        for b in _all_bases(item):
            src = os.path.join(b, rel)
            if not os.path.isdir(src) or _is_link(src):
                continue
            for p in _walk_files(src):
                sub = os.path.relpath(p, src).replace("\\", "/")
                seen.setdefault("overrides/" + rel + "/" + sub, p)
    eff = effective_rules(rules)
    return sorted(((arc, p) for arc, p in seen.items()
                   if apply_rules(arc[len("overrides/"):], eff)),
                  key=lambda t: t[0])


_FIRST_TO_SECTION = {
    "mods": "mods", "config": "config", "saves": "saves",
    "resourcepacks": "resources", "shaderpacks": "resources",
}


def plan(item, sections=DEFAULT_SECTIONS, extra=(), rules=None) -> dict:
    """先算总量：文件数 / 字节数 / 各段文件数（不读文件内容，很快）"""
    pairs = _collect(item, sections, extra, rules)
    total = 0
    per = {k: 0 for k in SECTION_KEYS}
    for arc, p in pairs:
        try:
            total += os.path.getsize(p)
        except OSError:
            continue
        parts = arc.split("/")
        first = parts[1] if len(parts) > 1 else ""
        sec = _FIRST_TO_SECTION.get(first, "config")   # 根上的是 options.txt 之类
        if sec in per:
            per[sec] += 1
    return {"files": len(pairs), "bytes": total, "per": per, "pairs": pairs}


def _sha1(path: str) -> str:
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def manifest_for(item, sections, pl: dict, extra=(), mrpack=False) -> dict:
    it = item or {}
    from . import launchprefs
    loader = str(it.get("loader") or "")
    lver = str(it.get("loader_ver") or "")
    loaders = []
    if loader:
        loaders.append({"id": f"{loader}-{lver}".rstrip("-"), "primary": True})
    mods = []
    for arc, p in pl["pairs"]:
        if not arc.startswith("overrides/mods/"):
            continue
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        mods.append({"file": os.path.basename(arc), "size": size,
                     "sha1": _sha1(p)})
    return {
        "format": "bagui-pack/1",
        "name": str(it.get("name") or it.get("id") or "整合包"),
        "versionId": launchprefs.game_key(it)[-40:],
        "minecraft": {"version": str(it.get("mc") or ""), "modLoaders": loaders},
        "exportedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "include": list(sections),
        "extra": list(extra or ()),
        "standard": bool(mrpack),
        "counts": {"files": pl["files"], "bytes": pl["bytes"], "mods": len(mods)},
        "mods": mods,
    }


def manifest_mrpack(item, pl: dict) -> dict:
    """标准 Modrinth 整合包清单（PCL-CE L1205-1211 的字段口径）。

    **默认全量内嵌**：`downloads` 留空数组，文件照旧躺在 `overrides/` 里。
    PCL-CE 会对查到 CDN 地址的模组 `File.Delete(modFile.path)` 删副本，
    这里刻意不学 —— 没校验过 URL 就把文件从包里拿掉，等于把整合包完整性
    押在第三方 CDN 上（第 95 轮：不等价就不许静默降级）。
    """
    it = item or {}
    files = []
    for arc, p in pl["pairs"]:
        rel = arc[len("overrides/"):] if arc.startswith("overrides/") else arc
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        # 大文件不重复读一遍算 sha512（sha1 在 manifest_for 里已经算过）
        sha512 = ""
        if size < (1 << 24):
            try:
                h = hashlib.sha512()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                sha512 = h.hexdigest()
            except OSError:
                pass
        files.append({
            "path": rel,
            "hashes": {"sha1": _sha1(p), "sha512": sha512},
            "downloads": [],
            "fileSize": size,
        })
    deps = {"minecraft": str(it.get("mc") or "")}
    loader = str(it.get("loader") or "").lower()
    lver = str(it.get("loader_ver") or "")
    if loader and lver:
        key = {"forge": "forge", "neoforge": "neoforge",
               "fabric": "fabric-loader", "quilt": "quilt-loader"}.get(loader)
        if key:
            deps[key] = lver
    return {
        "game": "minecraft",
        "formatVersion": 1,
        "versionId": str(it.get("version_id") or "1.0.0"),
        "name": str(it.get("name") or it.get("id") or "整合包"),
        "summary": "由「八桂启动器」导出（文件已内嵌在 overrides 里）",
        "files": files,
        "dependencies": deps,
    }


_README = """{name}
------------------------------------------------------------
Minecraft {mc}{loader_line}
导出时间：{time}
内容：{sections}（共 {files} 个文件，{size}）

怎么用：
1. 解压这个 zip；
2. 把 overrides 里的东西放进你的 .minecraft（mods → mods、config → config、
   options.txt → 根目录）；
3. 用启动器安装对应版本{loader_tip}。

manifest.json 里写着完整模组清单（含 sha1），校验或重新下载都用得上。
由「八桂启动器」导出。
"""


def export_pack(item, dest_zip: str, sections=DEFAULT_SECTIONS,
                progress_cb=None, cancel_flag=None, compress=True,
                extra=(), rules=None, mrpack=False) -> dict:
    """执行导出。返回 {ok, path, files, bytes, mods, ...}

    `progress_cb(done_files, total_files)` 在工作线程里被调用 ——
    调用方**只能写数据**，不许碰控件。

    新增参数：`extra` 玩家勾的其它文件夹名列表、`rules` 规则表
    （None = 默认排除）、`mrpack` 是否额外写一份标准 `modrinth.index.json`。
    **不导出启动器本体**。
    """
    sections = tuple(s for s in sections if s in SECTION_KEYS) or DEFAULT_SECTIONS
    extra = tuple(str(e) for e in (extra or ()))
    it = item or {}
    pl = plan(it, sections, extra, rules)
    if not pl["files"]:
        raise RuntimeError("这个游戏没有可导出的内容（模组 / 配置都是空的）")

    dest_zip = os.path.abspath(dest_zip)
    parent = os.path.dirname(dest_zip)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = dest_zip + ".part"
    total = pl["files"]
    done = 0
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    comp = 6 if compress else 0

    try:
        with zipfile.ZipFile(tmp, "w", mode, compresslevel=comp) as z:
            man = manifest_for(it, sections, pl, extra, mrpack)
            z.writestr("manifest.json",
                       json.dumps(man, ensure_ascii=False, indent=2))
            if mrpack:
                z.writestr("modrinth.index.json",
                           json.dumps(manifest_mrpack(it, pl),
                                      ensure_ascii=False, indent=2))
            loader_line = (f" · {it.get('loader')} {it.get('loader_ver')}"
                           if it.get("loader") else " · 原版")
            sec_cn = [dict((k, n) for k, n, _d in SECTIONS)[s] for s in sections]
            if extra:
                sec_cn.append(f"其它文件夹 {len(extra)} 个")
            z.writestr("README.txt", _README.format(
                name=man["name"], mc=it.get("mc") or "未知",
                loader_line=loader_line,
                time=man["exportedAt"],
                sections="、".join(sec_cn),
                files=pl["files"], size=_fmt_bytes(pl["bytes"]),
                loader_tip="（" + str(it.get("loader")) + "）"
                if it.get("loader") else ""))
            for arc, src in pl["pairs"]:
                if cancel_flag and cancel_flag[0]:
                    raise RuntimeError("已取消")
                z.write(src, arc)
                done += 1
                if progress_cb:
                    try:
                        progress_cb(done, total)
                    except Exception:
                        pass
        os.replace(tmp, dest_zip)
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise

    return {"ok": True, "path": dest_zip, "files": pl["files"],
            "bytes": pl["bytes"], "size": os.path.getsize(dest_zip),
            "mods": len(man["mods"]), "standard": bool(mrpack)}


def _fmt_bytes(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return (f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}")
        n /= 1024
    return f"{n:.1f} TB"


fmt_size = _fmt_bytes          # 界面用（内部也调，别在两处各写一份）


def default_name(item, mrpack=False) -> str:
    """给导出的 zip 取个不会和磁盘上已有序号打架的名字"""
    it = item or {}
    base = "".join(ch for ch in str(it.get("name") or it.get("id") or "整合包")
                   if ch not in '\\/:*?"<>|').strip() or "整合包"
    mc = str(it.get("mc") or "").strip()
    if mc and mc.lower() not in base.lower():
        base += "-" + mc
    return base + (".mrpack" if mrpack else ".zip")


def free_name(folder: str, base: str) -> str:
    """`folder` 下不重名的完整路径（重名加 (2)(3)…）"""
    folder = os.path.abspath(folder)
    stem, ext = os.path.splitext(base)
    cand = os.path.join(folder, base)
    i = 2
    while os.path.exists(cand):
        cand = os.path.join(folder, f"{stem} ({i}){ext}")
        i += 1
        if i > 999:
            break
    return cand
