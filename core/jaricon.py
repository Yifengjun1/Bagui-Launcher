# -*- coding: utf-8 -*-
"""从 jar / zip 里抠出图标（第六十五轮，第九十二轮大修）。

模组圈的几套约定（按可信度从高到低）：
- **meta 里自己声明的**：`fabric.mod.json` / `quilt.mod.json` 的 `icon`（字符串或
  `{"64": "…"}` 这种像素档位）、`META-INF/mods.toml` / `META-INF/neoforge.mods.toml`
  的 `logoFile`、老 Forge 的 `mcmod.info` `logoFile`。**路径相对 jar 根，叫什么都可能**
  （`logo.png`、`almanac.png`、`assets/xxx/logo.png`…）。
- **`assets/<modid>/icon.png`**（Mod Menu / EMI / JEI 都认这个）；
- 根目录的 `pack.png` / `icon.png` / `logo.png`；
- 退一步：`assets/<modid>/` 底下更深处的 `icon.*` / `logo.*`（如
  `assets/attributefix/textures/logo.png`），**只有目录名像这个模组时才认**。

第九十二轮为什么要加 meta 解析：拿玩家本机真实整合包量过，只认 `assets/<id>/icon.png`
时 **482 个包里只有 95 个抠得出图标（19.7%）** —— 剩下 276 个全是「meta 声明了
logoFile 但规则没认」，界面上就是一片绿方块 J。这就是玩家说的「文件夹里文件图标不全」。

这里只负责**把图片字节读出来**，不碰 Qt —— 画到界面上是 `ui/fileicons.py` 的事，
这样探针能直接测「有没有读到」「读到的是不是那一张」。

性能：只读 zip 的中央目录 + 几个小 meta + 一张图，不整包解压；单个条目超过
`_MAX_BYTES`（meta 声明的放宽到 `_MAX_BYTES_DECL`）直接跳过（有的包拿 4K 截图当图标）。
调用方按 (路径, mtime, 尺寸) 缓存，重复打开不反复读盘。

⚠ 别跟 `core/packicon.py` 搞混：那个管的是「整合包实例目录里的图标文件」。
"""
import json
import os
import re
import zipfile

_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
_MAX_BYTES = 2 * 1024 * 1024          # 2MB：正常 icon / pack.png 都在 100KB 以内
_MAX_BYTES_DECL = 6 * 1024 * 1024     # meta 自己声明的那张：放宽些（banner 常常 1~4MB）
_ARCHIVE_EXT = (".jar", ".zip", ".mrpack")

# assets/<一层目录>/icon.(png|jpg…) —— 单层的 icon 永远最可信
_RE_ASSETS_ICON = re.compile(r"^assets/([^/]+)/icon\.(png|jpg|jpeg|webp)$")
# assets/<一层目录>/logo.(png|…) —— Forge 圈的常见写法（第九十二轮）
_RE_ASSETS_LOGO = re.compile(r"^assets/([^/]+)/logo\.(png|jpg|jpeg|webp)$")
# assets/<目录>/<更深…>/(icon|logo).(png|…) —— 如 attributesfix/textures/logo.png
_RE_ASSETS_DEEP = re.compile(
    r"^assets/([^/]+)/(?:[^/]+/)+?(icon|logo)\.(png|jpg|jpeg|webp)$")
_ROOT_ICON = ("pack.png", "icon.png", "logo.png")

# meta 文件名（zip 内，全小写比较）
_META_JSON = ("fabric.mod.json", "quilt.mod.json")
_META_TOML = ("meta-inf/mods.toml", "meta-inf/neoforge.mods.toml")
_RE_LOGOFILE = re.compile(r"logoFile\s*=\s*[\"']([^\"']+)[\"']")
_RE_LOGOFILE_JSON = re.compile(r'"logoFile"\s*:\s*"([^"]*)"')


def _flat_name(n: str) -> str:
    return str(n or "").replace("\\", "/").lstrip("/")


def _px_key(s: str) -> int:
    """fabric `icon` 的档位键（"64"/"128"…）按数字比大小，非数字算 0"""
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return 0


def _declared_logos(zf, low):
    """包里 meta 声明的 logo 条目名（已确认包里真的存在、确实是图片），按可信度排序。

    `low` 是 `{小写条目名: 真实条目名}`。返回真实条目名列表（不含路径大小写差异）。
    """
    raw = []
    for cand in _META_JSON:
        real = low.get(cand)
        if not real:
            continue
        try:
            data = json.loads(zf.read(real).decode("utf-8", "replace")) or {}
        except Exception:                                 # noqa: BLE001
            continue
        ic = data.get("icon") if isinstance(data, dict) else None
        if isinstance(ic, str):
            raw.append(ic)
        elif isinstance(ic, dict):
            pairs = [(k, v) for k, v in ic.items() if isinstance(v, str)]
            pairs.sort(key=lambda kv: -_px_key(kv[0]))
            raw.extend(v for _k, v in pairs)
    for cand in _META_TOML:
        real = low.get(cand)
        if not real:
            continue
        try:
            txt = zf.read(real).decode("utf-8", "replace")
        except Exception:                                 # noqa: BLE001
            continue
        raw.extend(_RE_LOGOFILE.findall(txt))
    real = low.get("mcmod.info")
    if real:
        try:
            txt = zf.read(real).decode("utf-8", "replace")
        except Exception:                                 # noqa: BLE001
            txt = ""
        raw.extend(_RE_LOGOFILE_JSON.findall(txt))

    out, seen = [], set()
    for p in raw:
        p = _flat_name(p)
        if not p:
            continue
        lowp = p.lower()
        if not lowp.endswith(_IMG_EXT):
            continue
        hit = low.get(lowp)                # 包里的真实条目名（大小写按实际）
        if not hit or hit in seen:
            continue
        seen.add(hit)
        out.append(hit)
    return out


def is_archive(name: str) -> bool:
    low = str(name or "").lower()
    if low.endswith(".disabled"):
        low = low[:-len(".disabled")]
    return low.endswith((".jar", ".zip", ".mrpack"))


def _is_jar(path: str) -> bool:
    low = str(path or "").lower()
    if low.endswith(".disabled"):
        low = low[:-len(".disabled")]
    return low.endswith(".jar")


def _stem(name: str) -> str:
    """`xaeroworldmap-fabric-1.20.1.jar.disabled` → `xaeroworldmap-fabric-1.20.1`"""
    base = os.path.basename(str(name or "")).lower()
    if base.endswith(".disabled"):
        base = base[:-len(".disabled")]
    for ext in _ARCHIVE_EXT:
        if base.endswith(ext):
            return base[:-len(ext)]
    return base


def _flat(s: str) -> str:
    """比 id 用：去掉 `-` `_` `.` `+` 空格，`farmers-delight` 与 `farmersdelight` 视为同一个"""
    return re.sub(r"[-_.+ ]", "", s)


def _score_dir(dirname: str, stem: str) -> int:
    """assets 下的目录名有多像这个 jar 的 mod id（分越高越可信）"""
    d, s = _flat(dirname), _flat(stem)
    if not d or not s:
        return 0
    if d == s:
        return 30
    head = _flat(stem.split("-")[0])
    if head and d == head:            # jei-1.21.1-19.21.0.247 → assets/jei
        return 25
    if d in s:                        # create-1.21.1-6.0.4 → assets/create
        return 20
    if s in d:                        # chipped → assets/chippedextra
        return 15
    return 0


def _order(names, path: str, declared=()) -> list:
    """把 zip 的条目名排成「最可能是图标」的顺序。

    `declared`（第九十二轮）：meta 里声明的 logo 条目名，排在**所有启发式之前** ——
    那是作者自己指的图，`almanac.png` / `biomesoplenty_logo.png` 这种名字启发式永远猜不到。
    """
    stem = _stem(path)
    root, assets, meta = [], [], []
    rank = {str(d).lower(): i for i, d in enumerate(declared or ())}
    for raw in names:
        n = _flat_name(raw)
        low = n.lower()
        if not low or low.endswith("/") or not low.endswith(_IMG_EXT):
            continue
        if low in rank:
            meta.append((100 - min(rank[low], 99), len(n), n))
            continue
        if low in _ROOT_ICON:
            root.append((_ROOT_ICON.index(low), len(n), n))
            continue
        if low == "assets/icon.png":
            assets.append((5, len(n), n))
            continue
        m = _RE_ASSETS_ICON.match(low)
        if m:
            assets.append((40 + _score_dir(m.group(1), stem), len(n), n))
            continue
        m = _RE_ASSETS_LOGO.match(low)
        if m:
            # 同目录的 logo.* 比 icon.* 次一档，但仍远胜「根目录随便一张 png」
            s = _score_dir(m.group(1), stem)
            if s:
                assets.append((30 + s, len(n), n))
            continue
        m = _RE_ASSETS_DEEP.match(low)
        if m:
            # 深层（textures/…）只在这个目录名像本模组时才认，否则宁可不猜
            s = _score_dir(m.group(1), stem)
            if s:
                depth = low.count("/") - 1
                assets.append((10 + s // 3 + max(0, 3 - depth), len(n), n))

    meta.sort(key=lambda t: (-t[0], t[1], t[2]))
    assets.sort(key=lambda t: (-t[0], t[1], t[2]))
    root.sort()
    # 模组先看 meta / assets（那才是模组图标）；材质包 / 光影包先看根目录 pack.png
    order = (meta + assets + root) if _is_jar(path) else (meta + root + assets)
    return [t[2] for t in order]


def _quick_hit(order, path: str) -> bool:
    """旧规则就能命中的快路径：单层 `assets/<id>/icon.*` 或根 `pack.png` / `icon.png`。

    命中这些就不必去读 meta（每个包省 1~3 次小文件解压）；不命中才走 meta 解析。
    """
    if not order:
        return False
    first = order[0]
    low = first.lower()
    if _is_jar(path):
        return bool(_RE_ASSETS_ICON.match(low))
    return low in ("pack.png", "icon.png")


def candidates(path: str) -> list:
    """按「最可能是图标」排好序的条目名（要打开 zip 才知道里面有什么）"""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            low = {_flat_name(n).lower(): _flat_name(n) for n in names}
            base = _order(names, path)
            if _quick_hit(base, path):
                return base
            return _order(names, path, _declared_logos(zf, low))
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        return []


def read_icon(path: str):
    """把图标原图读成字节。读不到返回 `(None, "")`。

    返回 `(bytes, 命中的条目名)` —— 条目名让探针 / 日志能说清「用的是哪一张」。

    **只开一次 zip**：先 `namelist()` 排序、再 `read()` 命中的那条。以前是
    `candidates()` 开一次 + `read_icon()` 再开一次，一个 jar 两次中央目录读取，
    实测 213 个模组要 4.3 秒（复用同一个句柄降到 4.0 秒，剩下的是磁盘本身慢）。

    第九十二轮：旧规则能命中时**不读 meta**（快路径）；命不中才解析 meta 声明的 logo，
    并且对 meta 声明的那张放宽到 `_MAX_BYTES_DECL`（作者指的图就算 3MB 也认）。
    """
    path = str(path or "")
    if not path or not os.path.isfile(path):
        return None, ""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            low = {_flat_name(n).lower(): _flat_name(n) for n in names}
            order = _order(names, path)
            big = {}
            if not _quick_hit(order, path):
                declared = _declared_logos(zf, low)
                if declared:
                    order = _order(names, path, declared)
                    big = {d.lower(): True for d in declared}
            for name in order:
                try:
                    info = zf.getinfo(name)
                    limit = (_MAX_BYTES_DECL if big.get(name.lower())
                             else _MAX_BYTES)
                    if int(getattr(info, "file_size", 0) or 0) > limit:
                        continue                 # 太大，多半是封面图而不是图标
                    data = zf.read(name)
                except (KeyError, OSError, RuntimeError, ValueError):
                    continue
                if data:
                    return data, name
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        return None, ""
    return None, ""
