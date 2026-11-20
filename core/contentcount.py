# -*- coding: utf-8 -*-
"""实例内容类型计数（第八十九轮，第九十轮改为「按磁盘实际布局探」）。

给「添加已有文件夹」的每一行画「模组 / 材质包 / 光影 / 数据包」图标用。
**非 UI 模块**，探针可直测。

第九十轮为什么要重写：原来的实现直接拿 `launchprefs.game_dirs()` 里的 `work`
当唯一目录，而那个值是「按本软件的隔离开关算出来的」，不是磁盘事实。于是

  · PCL2 的 `versions/<id>`（默认配置下算作「不隔离」）→ 指向 .minecraft 根，
    而模组其实在 `versions/<id>/mods` 里 → 全数成 0；
  · Prism / MultiMC 的实例把游戏目录放在 `<实例>/.minecraft` → 也全数成 0。

所以现在改成：把「实例目录 / 实例/.minecraft / 实例/minecraft / 游戏根 / 根/.minecraft」
全当候选层，每层各自数，**每种类型取计数最大的那一层**（不做跨层求和 —— 有的整合包
两层都放东西，求和会翻倍）。数据包除了各世界的 `saves/*/datapacks`，还算版本根上的
`datapacks/`（`Zombie Invade 100 Days` 这种就放在那儿）。
"""
import os
import time

_CACHE = {}          # normkey -> (ts, counts)
_TTL = 20.0          # 秒：短时间内重复渲染直接复用

_EMPTY = {"mods": 0, "resourcepack": 0, "shader": 0, "datapack": 0, "saves": 0}

# 实例目录下面这几种再套一层的形态（Prism 的 .minecraft、少数包的 minecraft）
_SUBS = ("", ".minecraft", "minecraft")

# 模组目录里只认这几种：.jar / .zip 及其禁用态，别把 .index、日志算成模组
_MOD_EXT = (".jar", ".zip", ".jar.disabled", ".zip.disabled")
# 材质包 / 光影允许是解压后的文件夹，所以不按扩展名筛，只跳隐藏项


def _norm(p):
    try:
        return os.path.normcase(os.path.abspath(p or ""))
    except (OSError, ValueError):
        return str(p or "").lower()


def _count_entries(d, exts=None):
    """目录里的可见条目数（忽略 . 开头的）；给了 exts 就只数这些后缀的文件。"""
    if not d:
        return 0
    try:
        with os.scandir(d) as it:
            n = 0
            for e in it:
                if e.name.startswith("."):
                    continue
                if exts is not None:
                    low = e.name.lower()
                    if not any(low.endswith(x) for x in exts):
                        continue
                n += 1
            return n
    except OSError:
        return 0


def _dirs_under(d):
    """d 下所有子目录的完整路径（读不了就空）"""
    out = []
    if not d:
        return out
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if e.is_dir() and not e.name.startswith("."):
                        out.append(e.path)
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _datapacks(base):
    """某一层里的数据包数量。

    三个来源相加，各自不重叠：
      · 各世界的 `saves/*/datapacks`
      · 版本根的 `datapacks/`（有些整合包随版本走）
      · NeoForge 的 `global_packs/required_data`、`optional_data`
        （整合包随包附带的数据包，`Zombie Invade 100 Days` 那种就是放这儿）
    """
    n = 0
    for w in _dirs_under(os.path.join(base, "saves")):
        n += _count_entries(os.path.join(w, "datapacks"))
    n += _count_entries(os.path.join(base, "datapacks"))
    gp = os.path.join(base, "global_packs")
    n += _count_entries(os.path.join(gp, "required_data"))
    n += _count_entries(os.path.join(gp, "optional_data"))
    return n


def _layer_counts(base):
    """一层目录里的各类计数"""
    return {
        "mods": _count_entries(os.path.join(base, "mods"), _MOD_EXT),
        "resourcepack": _count_entries(os.path.join(base, "resourcepacks")),
        "shader": _count_entries(os.path.join(base, "shaderpacks")),
        "datapack": _datapacks(base),
        # 存档：只数目录（一个世界一个子目录），文件别算进来
        "saves": len(_dirs_under(os.path.join(base, "saves"))),
    }


def _layers(dirs):
    """候选层（去重、必须真实存在）：实例目录 / 其 .minecraft / 游戏根 / 根的 .minecraft"""
    dirs = dirs or {}
    bases, seen = [], set()
    for root in (dirs.get("instance"), dirs.get("work"), dirs.get("root")):
        root = str(root or "")
        if not root:
            continue
        for sub in _SUBS:
            d = os.path.join(root, sub) if sub else root
            k = _norm(d)
            if k in seen or not os.path.isdir(d):
                continue
            seen.add(k)
            bases.append(d)
    return bases


def counts(dirs):
    """给定 launchprefs.game_dirs() 的结果（含 instance/work/root），返回各内容类型计数。

    每类取「候选层里数得最多」的那一层：Prism 这类巢状布局只会在 .minecraft 层有东西，
    PCL2 的 versions/<id> 只在实例自己那层有东西，正常实例两层一样多。
    """
    res = dict(_EMPTY)
    for base in _layers(dirs):
        c = _layer_counts(base)
        for k, v in c.items():
            if v > res[k]:
                res[k] = v
    return res


def counts_for_entry(entry):
    """后台线程入口：算实例各内容类型数量（带短 TTL 缓存）。"""
    entry = entry or {}
    key = _norm(entry.get("path") or entry.get("dir") or "")
    now = time.time()
    c = _CACHE.get(key)
    if c and now - c[0] < _TTL:
        return c[1]
    try:
        from . import launchprefs
        # instance 字段由 game_dirs 现算（entry 自己的 path），保证候选层覆盖到实例目录
        res = counts(launchprefs.game_dirs(entry))
    except Exception:                                  # noqa: BLE001
        res = dict(_EMPTY)
    if key:
        _CACHE[key] = (now, res)
    return res


def forget(entry=None):
    """丢掉某个实例（或全部）的计数缓存。

    什么时候要调：概览里就地禁用 / 删除模组之后。缓存键只有路径、TTL 20 秒，
    不主动失效的话玩家会看到「刚删完还是 12 个」，像没生效。
    """
    if entry is None:
        _CACHE.clear()
        return
    key = _norm((entry or {}).get("path") or (entry or {}).get("dir") or "")
    if key:
        _CACHE.pop(key, None)
