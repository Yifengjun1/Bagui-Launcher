# -*- coding: utf-8 -*-
"""启动偏好：版本隔离 / 优先进程 / Java / 运行内存（全局 + 每游戏覆盖）。

第四十四轮把原来散在「设置 → 运行内存」和每个官启档案里的东西，收成一套
**可解释、可测试**的偏好层：界面只读写这里，启动引擎（`core/gamelaunch.py`）
也只读这里，中间不再各拼一套。

三条铁律：
1. **每游戏设置永远优先于全局**，但「跟随全局」三个字要能真的跟随（存 None 而不是存当时的快照）；
2. 值非法（老配置、手改坏、未来的新档位）一律**退回默认**，不抛异常 ——
   这个模块会被界面和启动线程同时读，抛异常等于启动不了游戏；
3. 每个判定都要能用人话解释（`why_*` 系列），否则玩家看不懂「为什么给我 4GB」。

**为什么 `ISO_DEFAULT = "all"`**：独立启动从实现那天起，工作目录就是
`versions/<版本>`（PCL2 的实例语义），也就是一直「隔离所有」。默认值必须等于
升级前的实际行为，否则老玩家一更新就发现存档「不见了」（其实还在旧目录里）。
想共享的人可以自己切到「关闭」。
"""
import os
import re

from . import ram
from .config import CONFIG

# ---------------------------------------------------------------- 选项表
ISO_MODES = (
    ("off", "关闭", "所有版本共用一个游戏目录（存档 / 模组混在一起）"),
    ("mod", "Mod 隔离", "只隔离 Forge / NeoForge / Fabric / Quilt 版本，避免模组互相打架"),
    ("pirate", "非正版隔离", "只隔离快照 / 预发布 / 远古 / 愚人节版本，正式版照旧共享"),
    ("both", "Mod / 非正式版隔离", "上面两档取或：有加载器或不是正式版就隔离"),
    ("all", "隔离所有", "每个版本各有独立目录，存档与模组彻底分开"),
)
ISO_DEFAULT = "all"
ISO_KEYS = tuple(k for k, _n, _d in ISO_MODES)

PRIORITIES = (
    ("high", "高", "优先保证游戏帧数与响应，其他程序（浏览器、下载）可能变卡"),
    ("normal", "中", "系统和游戏平分 CPU，日常最稳（默认）"),
    ("low", "低", "优先保证其他程序流畅，游戏可能掉帧"),
)
PRIORITY_DEFAULT = "normal"
PRIORITY_KEYS = tuple(k for k, _n, _d in PRIORITIES)

MEMORY_MODES = (
    ("auto", "自动配置", "按已装模组数量与本机剩余内存自动分配（默认）"),
    ("custom", "自定义", "自己拖滑条 / 输入数值"),
)
MEMORY_DEFAULT = "auto"
MEMORY_KEYS = tuple(k for k, _n, _d in MEMORY_MODES)

JAVA_AUTO_LABEL = "自动选择（按游戏需要挑最合适的 Java）"


# ---------------------------------------------------------------- 全局偏好
def _get(key, default=None):
    try:
        v = CONFIG.get(key)
    except Exception:
        return default
    return default if v is None else v


def iso_mode() -> str:
    v = str(_get("iso_mode", ISO_DEFAULT) or ISO_DEFAULT).lower()
    return v if v in ISO_KEYS else ISO_DEFAULT


def set_iso_mode(v: str):
    v = str(v or "").lower()
    CONFIG.set("iso_mode", v if v in ISO_KEYS else ISO_DEFAULT)


def priority() -> str:
    v = str(_get("launch_priority", PRIORITY_DEFAULT) or PRIORITY_DEFAULT).lower()
    return v if v in PRIORITY_KEYS else PRIORITY_DEFAULT


def set_priority(v: str):
    v = str(v or "").lower()
    CONFIG.set("launch_priority", v if v in PRIORITY_KEYS else PRIORITY_DEFAULT)


def priority_cn(v: str = "") -> str:
    v = v or priority()
    return dict((k, n) for k, n, _d in PRIORITIES).get(v, "中")


def java_choice() -> tuple[str, str]:
    """全局 Java：("auto", "") 或 ("path", exe)"""
    mode = str(_get("java_mode", "auto") or "auto").lower()
    path = str(_get("java_path", "") or "")
    if mode == "path" and path and os.path.isfile(path):
        return "path", path
    return "auto", ""


def set_java_choice(mode: str, path: str = ""):
    mode = "path" if str(mode).lower() == "path" else "auto"
    path = str(path or "").strip().strip('"')
    if mode == "path" and not os.path.isfile(path):
        mode = "auto"          # 文件被删了/选错了 → 回到自动，别留一个坏路径
        path = ""
    CONFIG.set("java_mode", mode)
    CONFIG.set("java_path", path)


def memory_mode() -> str:
    v = str(_get("memory_mode", MEMORY_DEFAULT) or MEMORY_DEFAULT).lower()
    return v if v in MEMORY_KEYS else MEMORY_DEFAULT


def set_memory_mode(v: str):
    v = str(v or "").lower()
    CONFIG.set("memory_mode", v if v in MEMORY_KEYS else MEMORY_DEFAULT)


def custom_memory() -> int:
    try:
        mb = int(_get("memory_mb", 0) or 0)
    except Exception:
        mb = 0
    return mb if mb >= 512 else 4096


def set_custom_memory(mb: int):
    try:
        mb = int(mb)
    except Exception:
        mb = 4096
    CONFIG.set("memory_mb", max(1024, min(mb, ram.HARD_MAX_MB)))


# ---------------------------------------------------------------- 每游戏设置
def game_key(item) -> str:
    """一个游戏（版本或整合包）的稳定键：优先真实路径，其次版本 id"""
    if isinstance(item, str):
        p, vid = item, ""
    else:
        item = item or {}
        p = str(item.get("path") or item.get("dir") or "")
        vid = str(item.get("version_id") or item.get("id") or "")
    if p:
        try:
            return os.path.normcase(os.path.realpath(p))
        except Exception:
            return os.path.normcase(p)
    return "id:" + vid.lower()


def _all_game_conf() -> dict:
    d = CONFIG.get("game_settings")
    return d if isinstance(d, dict) else {}


def game_conf(item) -> dict:
    """该游戏的覆盖设置（缺省字段用 None / "global" 表示「跟随全局」）"""
    k = game_key(item)
    raw = _all_game_conf().get(k)
    # 第 104 轮新增：每实例自定义 JVM / 游戏参数、预启动命令、窗口、进服、
    # 内存附加项。空串 / "global" / 0 全部表示「跟随全局」。
    d = {"iso": None, "java": "global", "java_path": "",
         "memory": "global", "memory_mb": 0,
         "jvm": "", "game": "", "pre_launch": "", "pre_launch_wait": "global",
         "window": "", "server": "", "ram_lock": "global", "ram_init": 0}
    if isinstance(raw, dict):
        iso = raw.get("iso")
        d["iso"] = iso if iso in ("on", "off") else None
        jm = str(raw.get("java") or "global")
        d["java"] = jm if jm in ("global", "custom") else "global"
        d["java_path"] = str(raw.get("java_path") or "")
        mm = str(raw.get("memory") or "global")
        d["memory"] = mm if mm in ("global", "auto", "custom") else "global"
        try:
            d["memory_mb"] = int(raw.get("memory_mb") or 0)
        except Exception:
            d["memory_mb"] = 0
        for f in ("jvm", "game", "pre_launch", "window", "server"):
            d[f] = str(raw.get(f) or "")
        w = str(raw.get("pre_launch_wait") or "global")
        d["pre_launch_wait"] = w if w in ("global", "on", "off") else "global"
        lk = str(raw.get("ram_lock") or "global")
        d["ram_lock"] = lk if lk in ("global", "on", "off") else "global"
        try:
            d["ram_init"] = int(raw.get("ram_init") or 0)
        except Exception:
            d["ram_init"] = 0
    return d


def set_game_conf(item, **kv):
    """写回一条覆盖设置（只写传进来的字段）"""
    k = game_key(item)
    allc = dict(_all_game_conf())
    cur = game_conf(item)
    cur.update({a: b for a, b in kv.items() if a in cur})
    # 全是「跟随全局」就把这条删掉，配置里不留垃圾；第 104 轮的新字段
    # 也必须一起判 —— 否则只填了自定义 JVM 参数的实例会被当成空条目抹掉。
    _follow = (cur["iso"] is None and cur["java"] == "global"
               and cur["memory"] == "global" and not cur["jvm"]
               and not cur["game"] and not cur["pre_launch"]
               and cur["pre_launch_wait"] == "global" and not cur["window"]
               and not cur["server"] and cur["ram_lock"] == "global"
               and not cur["ram_init"])
    if _follow:
        allc.pop(k, None)
    else:
        allc[k] = cur
    CONFIG.set("game_settings", allc)
    return cur


def clear_game_conf(item):
    k = game_key(item)
    allc = dict(_all_game_conf())
    if allc.pop(k, None) is not None:
        CONFIG.set("game_settings", allc)


# ---------------------------------------------------------------- 判定
_NON_RELEASE_RE = re.compile(
    r"(?:^|[-_ ])(?:pre|rc|snapshot)\d*"      # 1.20-pre1 / 1.21-rc1
    r"|^\d{2}w\d{2}[a-z]$"                     # 23w13a 快照
    r"|infinite|^3d\s|shareware"               # 20w14infinite / 3D Shareware
    r"|^rd-|^cave|^combat",                    # 远古的洞穴/战斗测试版
    re.I)


def version_type(item) -> str:
    item = item or {}
    t = str(item.get("type") or "").lower()
    if t in ("release", "snapshot", "old_beta", "old_alpha"):
        return t
    vid = str(item.get("id") or item.get("version_id") or item.get("name") or "")
    return "snapshot" if _NON_RELEASE_RE.search(vid) else "release"


def is_non_release(item) -> bool:
    """快照 / 预发布 / 远古 / 愚人节版本"""
    return version_type(item) != "release"


def has_loader(item) -> bool:
    it = item or {}
    if str(it.get("loader") or ""):
        return True
    # 加载器字段没写全时，看名字（PCL2 的实例目录常叫 1.20.1-Forge_47.4.4）
    name = str(it.get("name") or it.get("id") or "")
    return bool(re.search(r"forge|fabric|quilt|neoforge", name, re.I))


def disk_fact_isolate(item) -> bool:
    """第 105 轮：实例目录里**已经有** mods / saves → 它本来就是隔离实例。

    照 PCL-CE `McInstance.ShouldBeIndie()` L122-131 的「磁盘事实兜底」。
    玩家把全局从「隔离所有」切到「关闭」时，若这里不做兜底，游戏会转去读
    游戏根目录，实例里那堆模组和存档当场「消失」—— 和本文件开头第 44 轮
    注释里记的是同一类事故。只做很便宜的一次 scandir，不递归、不读内容。
    """
    it = item or {}
    inst = str(it.get("path") or it.get("dir") or "")
    if not inst:
        return False
    try:
        mods = os.path.join(inst, "mods")
        if os.path.isdir(mods):
            with os.scandir(mods) as itr:
                for e in itr:
                    if e.is_file(follow_symlinks=False):
                        return True
        saves = os.path.join(inst, "saves")
        if os.path.isdir(saves):
            with os.scandir(saves) as itr:
                for e in itr:
                    if e.is_dir(follow_symlinks=False):
                        return True
    except (OSError, ValueError):
        return False
    return False


def should_isolate(item) -> bool:
    """这个游戏该不该用独立目录（存档 / 模组各一份）

    优先级：本游戏显式覆盖 > 全局档位 > 磁盘事实兜底。
    · 显式 `iso=off` 是玩家**明确要共享**，磁盘事实不许偷偷翻盘；
    · 全局 `off` 同理（第 44 轮：「想共享的人可以自己切到关闭」）；
    · 其余档位判完为 False 时，实例目录里已有 mods / saves 就继续隔离，
      免得玩家切个档位把存档弄丢。改这里一处，`work_dir()` 一变，导出、
      内存模组计数、概览目录打开全跟着生效。
    """
    ov = game_conf(item)["iso"]
    if ov is not None:
        return ov == "on"
    mode = iso_mode()
    if mode == "off":
        return False
    if mode == "all":
        return True
    if mode == "mod":
        decided = has_loader(item)
    elif mode == "pirate":
        decided = is_non_release(item)
    elif mode == "both":
        decided = has_loader(item) or is_non_release(item)
    else:
        decided = False
    return bool(decided) or disk_fact_isolate(item)


def why_isolate(item) -> str:
    ov = game_conf(item)["iso"]
    if ov is not None:
        return "按本游戏的独立设置（" + ("开启" if ov == "on" else "关闭") + "）"
    mode = iso_mode()
    cn = dict((k, n) for k, n, _d in ISO_MODES).get(mode, "关闭")
    if mode == "off":
        return "全局「关闭」：所有版本共用一个游戏目录"
    if mode == "mod":
        if has_loader(item):
            return "全局「Mod 隔离」：有加载器 → 隔离"
        return ("全局「Mod 隔离」：无加载器 → 共享"
                + ("（但实例目录里已有模组 / 存档，继续隔离以免丢内容）"
                   if disk_fact_isolate(item) else ""))
    if mode == "pirate":
        if is_non_release(item):
            return "全局「非正版隔离」：非正式版 → 隔离"
        return ("全局「非正版隔离」：正式版 → 共享"
                + ("（但实例目录里已有模组 / 存档，继续隔离以免丢内容）"
                   if disk_fact_isolate(item) else ""))
    if mode == "both":
        if has_loader(item) or is_non_release(item):
            return "全局「Mod / 非正式版隔离」：命中条件 → 隔离"
        return ("全局「Mod / 非正式版隔离」：未命中 → 共享"
                + ("（但实例目录里已有模组 / 存档，继续隔离以免丢内容）"
                   if disk_fact_isolate(item) else ""))
    return f"全局「{cn}」"


def game_root(item) -> str:
    """该游戏的「游戏根目录」（mods / saves / config 所在的那一层）

    三种来源按可信度排：显式 `game_dir` > 目录形态 > 目录自己。
    · `…/versions/<id>` 是版本目录，根是上两级（独立启动历来这么算）；
    · Prism 的 `instances/<名字>` 本身就是完整游戏目录，
      再往上退两级会退到启动器安装目录，模组文件夹就指错了。
    """
    it = item or {}
    gd = str(it.get("game_dir") or "")
    if gd:
        return gd
    p = str(it.get("path") or it.get("dir") or "")
    if not p:
        return ""
    p = os.path.abspath(p)
    parent = os.path.dirname(p)
    if os.path.basename(parent).lower() == "versions":
        return os.path.dirname(parent)
    return p


def work_dir(item) -> str:
    """这个游戏**实际的工作目录**（mods / saves / config 到底在哪一层）。

    隔离开着 → 实例目录（`versions/<id>` 或整合包目录），这是独立启动的 cwd；
    隔离关着 → 游戏根目录。概览里的文件夹、导出的取文件、内存的模组计数
    都必须看这一层 —— 用根目录去数模组，隔离实例的模组会被数成 0。
    """
    it = item or {}
    inst = str(it.get("path") or it.get("dir") or "")
    try:
        if inst and should_isolate(it):
            return inst
    except Exception:
        pass
    return game_root(it) or inst


def datapack_dir(item) -> tuple[str, str]:
    """数据包目录：装在**世界**里，所以取「最近玩过的世界」的 datapacks。

    返回 (目录, 说明)。没有存档时不抛异常 —— 概览页只是给个按钮，
    点下去不该弹出报错框。说明用来在界面上讲清楚为什么落到 saves。
    """
    root = work_dir(item)
    if not root:
        return "", "还没确定游戏目录"
    saves = os.path.join(root, "saves")
    best, best_t = "", -1.0
    try:
        for n in os.listdir(saves):
            wp = os.path.join(saves, n)
            lv = os.path.join(wp, "level.dat")
            if os.path.isdir(wp) and os.path.isfile(lv):
                t = os.path.getmtime(lv)
                if t > best_t:
                    best, best_t = n, t
    except Exception:
        pass
    if best:
        return os.path.join(saves, best, "datapacks"), f"最近玩过的世界：{best}"
    return saves, "还没有存档：数据包要放进世界的 datapacks，先开个世界"


def game_dirs(item) -> dict:
    """该游戏的各个内容目录（概览里一键打开用）"""
    it = item or {}
    gd = game_root(it)
    wd = work_dir(it)
    inst = str(it.get("path") or it.get("dir") or "")
    dp, _note = datapack_dir(it)
    return {
        "root": gd,             # 游戏根目录（.minecraft 那一层）
        "work": wd,             # 实际生效的目录（隔离时是实例目录）
        "mods": os.path.join(wd, "mods") if wd else "",
        "saves": os.path.join(wd, "saves") if wd else "",
        "shaderpacks": os.path.join(wd, "shaderpacks") if wd else "",
        "resourcepacks": os.path.join(wd, "resourcepacks") if wd else "",
        "datapacks": dp,
        "instance": inst,
    }


def memory_for(item, game_dir: str = "") -> tuple[int, str]:
    """该游戏该给多少内存，返回 (MB, 说明)"""
    conf = game_conf(item)
    mode = conf["memory"]
    if mode == "global":
        mode = memory_mode()
        src = "全局"
    else:
        src = "本游戏"
    if mode == "custom":
        if conf["memory"] == "custom" and conf["memory_mb"] > 0:
            return conf["memory_mb"], f"按本游戏自定义：{ram.fmt(conf['memory_mb'])}"
        mb = custom_memory()
        return mb, f"按{src}自定义：{ram.fmt(mb)}"
    # 自动：数这个游戏**实际生效**的那个 mods（隔离时是实例目录里的）
    dirs = game_dirs(item)
    gd = game_dir or dirs["work"]
    mods = ram.count_mods(os.path.join(gd, "mods") if gd else "")
    mb, why = ram.auto_alloc(mods)
    return mb, f"按{src}自动配置：{why}"


def java_for(item) -> str:
    """该游戏用哪个 java.exe；返回空串表示「交给 gamelaunch 自动挑」"""
    conf = game_conf(item)
    if conf["java"] == "custom" and conf["java_path"] and os.path.isfile(conf["java_path"]):
        return conf["java_path"]
    mode, path = java_choice()
    return path if mode == "path" else ""


# ---------------------------------------------------------------- 第一百零四轮：启动项 / 自定义参数
# PCL-CE 的全局 Launch 组（App/Config.cs:474-575）与实例组 ArgConfig(:608-637)
# 是同一套旋钮、实例非空就**完全替换**全局。我们照这个语义做，但：
#   · 独立启动不读 launcher_profiles.json（第 96 轮铁律），参数只进 build_command；
#   · 开放自定义 JVM 参数前必须先做「按选项去重」，否则重复 -Xms / -cp /
#     --gameDir 会直接触发 MultipleArgumentsForOptionException（第 48 轮）；
#   · 默认优化参数串做成开关、默认关 —— 不硬加进每个玩家的启动命令。
# 默认优化参数串（PCL-CE 默认 JvmArgs，可一键开关）
DEFAULT_JVM_OPT_ARGS = (
    "-XX:+UseG1GC",
    "-XX:-UseAdaptiveSizePolicy",
    "-XX:-OmitStackTraceInFastThrow",
    "-Dfml.ignoreInvalidMinecraftCertificates=true",
    "-Dlog4j2.formatMsgNoLookups=true",
)

WINDOW_MODES = (
    ("windowed", "窗口", "按设定宽高开普通窗口"),
    ("fullscreen", "全屏", "开局直接全屏"),
    ("maximized", "最大化", "开局窗口最大化"),
)
WINDOW_MODE_KEYS = tuple(k for k, _n, _d in WINDOW_MODES)


def parse_args(text) -> list:
    """一行自定义参数 → token 列表（带引号的整段当一个参数）。

    不用 shlex：posix=True 会吃掉 Windows 路径里的反斜杠，posix=False 又不把
    引号当分组。这里按字符扫一遍，保留反斜杠、剥掉成对引号。玩家贴
    `-Dfoo="C:\\Program Files\\x"` 会稳稳当成一个参数。
    """
    s = str(text or "").strip()
    out, cur, quote = [], "", None
    for ch in s:
        if ch in ('"', "'"):
            if quote == ch:
                quote = None
            elif quote is None:
                quote = ch
            else:
                cur += ch
            continue
        if ch.isspace() and quote is None:
            if cur:
                out.append(cur)
                cur = ""
            continue
        cur += ch
    if cur:
        out.append(cur)
    return out


def _bool(key, default=False) -> bool:
    try:
        v = CONFIG.get(key, default)
    except Exception:                                  # noqa: BLE001
        return default
    if isinstance(v, str):
        return v.lower() in ("1", "true", "on", "yes")
    return bool(v if v is not None else default)


def _text(key) -> str:
    try:
        return str(CONFIG.get(key, "") or "")
    except Exception:                                  # noqa: BLE001
        return ""


def default_jvm_opt_enabled() -> bool:
    """默认优化 JVM 参数串的开关（默认关：不硬加，玩家可一键开）"""
    return _bool("jvm_opt_default", False)


def set_default_jvm_opt(on: bool):
    CONFIG.set("jvm_opt_default", bool(on))


def default_jvm_opt_args() -> list:
    return list(DEFAULT_JVM_OPT_ARGS) if default_jvm_opt_enabled() else []


def custom_jvm(item) -> list:
    """实例级非空 = 完全覆盖全局；全局空则不追加任何自定义 JVM 参数。"""
    v = str(game_conf(item).get("jvm") or "").strip()
    if not v:
        v = _text("jvm_args_global")
    return parse_args(v)


def custom_game(item) -> list:
    v = str(game_conf(item).get("game") or "").strip()
    if not v:
        v = _text("game_args_global")
    return parse_args(v)


def pre_launch(item) -> tuple[str, bool]:
    """(命令, 是否等待完成)：实例级非空覆盖全局，等待开关同理可分实例。"""
    c = game_conf(item)
    cmd = str(c.get("pre_launch") or "").strip() or _text("pre_launch_global").strip()
    w = str(c.get("pre_launch_wait") or "global")
    if w in ("on", "off"):
        wait = w == "on"
    else:
        wait = _bool("pre_launch_wait", True)
    return cmd, wait


def window_settings(item) -> dict:
    """窗口宽高 / 模式 / 标题（对应 build_command 的 repl 与 --width/--height）。"""
    c = game_conf(item)
    raw = str(c.get("window") or "").strip()
    w, h, mode, title = 854, 480, "windowed", ""
    if raw:
        # 设置页（dialogs / gamesettings）提示玩家填的是「宽x高|模式|标题」，
        # 例如 1280x720|fullscreen|我的世界。第 104 轮首版这里按「宽|高|模式|标题」
        # 解析，int("1280x720") 直接 ValueError → 静默退回 854x480，玩家改了窗口
        # 尺寸却完全不生效。两种写法都认，尺寸段优先按 x 拆。
        parts = [p.strip() for p in raw.split("|")]
        head, rest = parts[0].lower(), parts[1:]
        if "x" in head:
            try:
                sw, sh = head.split("x", 1)
                w, h = max(320, int(sw)), max(240, int(sh))
            except (ValueError, TypeError):
                pass
        else:
            try:
                w, h = max(320, int(head)), max(240, int(rest[0]))
                rest = rest[1:]
            except (ValueError, IndexError, TypeError):
                pass
        if rest and rest[0] in WINDOW_MODE_KEYS:
            mode = rest[0]
            rest = rest[1:]
        # 模式位不是合法值时，这一段当作标题的一部分，别把玩家写的字吞掉
        if rest:
            title = "|".join(rest)
    else:
        try:
            w = max(320, int(CONFIG.get("window_width", 854) or 854))
            h = max(240, int(CONFIG.get("window_height", 480) or 480))
        except (ValueError, TypeError):
            pass
        m = _text("window_mode")
        mode = m if m in WINDOW_MODE_KEYS else "windowed"
        title = _text("window_title")
    return {"width": w, "height": h, "mode": mode, "title": title}


def launcher_options(item) -> dict:
    """B6 低优先可选项：javaw / 启动后自动进服务器 / 启动器可见性 /
    GPU 偏好 / IPv4-IPv6 优先。全部有默认值，配置坏了不抛异常。"""
    c = game_conf(item)
    return {
        "use_javaw": _bool("javaw", False),
        "server": str(c.get("server") or "").strip() or _text("server_to_enter").strip(),
        "visibility": _text("launcher_visibility") or "keep",
        "gpu": _text("gpu_preference") or "auto",
        "ip_stack": _text("ip_stack") or "auto",
    }


def ram_options(item) -> dict:
    """C2：LockMemory / 独立初始堆 / -Xmn 开关 / 32 位封顶，实例级可覆盖。"""
    c = game_conf(item)
    lock = str(c.get("ram_lock") or "global")
    lock_on = _bool("ram_lock_memory", False) if lock == "global" else lock == "on"
    try:
        init = int(c.get("ram_init") or 0) or int(CONFIG.get("ram_init_mb", 0) or 0)
    except (ValueError, TypeError):
        init = 0
    return {"lock": lock_on, "init": max(0, init),
            "xmn": _bool("ram_xmn", True),
            "auto_min": _bool("memory_auto_min", True),
            "cap32": _bool("ram_32_cap", True)}
