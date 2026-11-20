# -*- coding: utf-8 -*-
"""两条并存的游戏目录（第四十五轮）。

以前只有一个 `.minecraft`：它既是本启动器自己的地盘，又替官方启动器存数据。
第四十五轮把两者拆开 ——

    应用路径   <安装目录>\\.minecraft                        ← 本启动器专用（独立启动 / 下载到「应用」）
    官方路径   <安装目录>\\official_assets\\.minecraft        ← 官方启动器用（下载到「官方」）

为什么官方那份要放 `official_assets` 子目录：官方启动器认 `.minecraft` 这个目录名，
以后官方目录要迁移 / 备份 / 换盘，全都在 `official_assets` 里面折腾，不会碰到
应用自己的东西；反过来我们清缓存、换整合包也不会把官方启动器的数据搅乱。

三条纪律：
1. **搬数据只在同一个卷里改名**（`os.replace`，瞬时）—— 跨卷就退化成目录联接，
   绝不做几十 G 的复制；
2. **老用户的游戏不能"消失"**：升级前那份 `.minecraft` 里如果已经有真实游戏数据，
   它整体归到「官方」路径，并把下载目标默认切到「官方」，让玩家一打开就看到原样；
3. 迁移**只做一次**（`paths_split_v45`），任何异常都吞掉并留痕，绝不拦着软件启动。
"""
import os
import shutil
import sys

from . import gamedir
from .config import CONFIG, INSTALL_DIR_NAME

OFFICIAL_DIR = "official_assets"        # 官方启动器数据都放这个子目录里
APP_DIR_NAME = ".minecraft"
TARGETS = ("app", "official")
TARGET_CN = {"app": "应用", "official": "官方"}
# 兜底值：配置里没有明确选择、也读不到「独立启动」开关时用哪个（第四十六轮起，
# 真正的默认值是 default_target()，它会看独立启动开关）。
DEFAULT_TARGET = "official"
TARGET_KEY = "download_target"          # 用户明确选过的目标（空串 = 跟随默认）
SPLIT_KEY = "paths_split_v45"
# 第六十七轮补丁：应用路径可以被指到「已有的那个 .minecraft」上。
# 场景：玩家把新版本装到别的目录，新的 <安装目录>\.minecraft 是空的，
# 以前下的游戏还躺在旧目录里 → 游戏管理一片空白。这条配置记住「用哪一份」。
APP_OVERRIDE_KEY = "app_game_override"
# 向导换安装目录时，把上一份应用路径记在这里 —— 万一 override 也被清掉还能找回
PREV_APP_KEY = "prev_app_game_dir"


def app_game_override() -> str:
    """玩家指定过的应用路径（空串 = 用 `<安装目录>\\.minecraft`）"""
    p = str(CONFIG.get(APP_OVERRIDE_KEY) or "").strip()
    return os.path.abspath(p) if p else ""


def set_app_game_dir(path: str):
    """把应用路径指到某个 .minecraft（空串 = 回默认）。

    只动配置，**绝不搬 / 删任何文件** —— 玩家原来的游戏就一直待在原地。
    顺手把老的 `game_dir` 对齐，免得 `installer.detect_game_dir()` 和这里各说一套。
    """
    p = os.path.abspath(str(path or "").strip()) if str(path or "").strip() else ""
    CONFIG.set(APP_OVERRIDE_KEY, p)
    if p:
        CONFIG.set("game_dir", p)
    else:
        CONFIG.set("game_dir", "")


def has_games(d: str) -> bool:
    """这个目录里确实装着东西吗（有版本 / 有整合包实例）"""
    d = str(d or "")
    if not d or not os.path.isdir(d):
        return False
    for sub in ("versions", "instances"):
        p = os.path.join(d, sub)
        try:
            if os.path.isdir(p) and any(os.path.isdir(os.path.join(p, n))
                                        for n in os.listdir(p)):
                return True
        except OSError:
            continue
    return False


def _mc_root_of(p: str) -> str:
    """从任意一条记录路径往上找那一层 `.minecraft`（找不到返回空串）

    记录里存的是 `...\\.minecraft\\mods\\xxx.jar` / `...\\.minecraft\\versions\\<id>`
    这种，往上走几层就能定位到这份游戏目录 —— 这是「换过安装目录」之后唯一还留在
    配置里的线索。
    """
    p = str(p or "")
    if not p:
        return ""
    cur = p if os.path.isdir(p) else os.path.dirname(p)
    for _ in range(7):
        if not cur or cur == os.path.dirname(cur):
            break
        if os.path.basename(cur).lower() == APP_DIR_NAME:
            return cur
        cur = os.path.dirname(cur)
    return ""


def _derived_game_dirs() -> list:
    """从安装记录 / 每游戏设置里反推候选游戏目录（新→旧都算，去重）"""
    out = []
    recs = CONFIG.get("installed") or {}
    items = []
    if isinstance(recs, dict):
        items += [str(v.get("path") or "") for v in recs.values() if isinstance(v, dict)]
    gs = CONFIG.get("game_settings") or {}
    if isinstance(gs, dict):
        items += [str(k) for k in gs]
    for p in items:
        d = _mc_root_of(p)
        if d and os.path.normcase(d) not in {os.path.normcase(x) for x in out}:
            out.append(d)
    return out


def recover_app_game_dir() -> tuple:
    """「换过安装目录，游戏全不见了」的自救（第六十七轮补丁）。

    新版本被装到另一个目录时，向导会把 `game_dir` 改成 `<新安装目录>\\.minecraft` ——
    那个目录是空的，而玩家以前下的游戏还躺在旧目录里，游戏管理于是一片空白。

    候选按可信度排，第一个「确实有游戏」的认回来（只改配置，不搬一个字节）：
      1. 向导上次记下的 `prev_app_game_dir`；
      2. 配置里原来的 `game_dir`；
      3. 从 `installed` / `game_settings` 记录里反推的 `.minecraft`。
    **不去碰 `%APPDATA%\\.minecraft`**：那是官方启动器的地盘，认成应用路径会把两份
    搅在一起（第四十五轮拆分就是为了不再搅）—— 除非我们自己的记录就指向它。

    返回 (是否改过, 现在用的目录, 原来的目录)，没改就是 (False, "", "")。
    """
    cur = app_game_dir()
    if has_games(cur):
        return False, "", ""
    cands = []
    for key in (PREV_APP_KEY, "game_dir"):
        p = str(CONFIG.get(key) or "").strip()
        if p:
            p = os.path.abspath(p)
            if p not in cands:
                cands.append(p)
    for p in _derived_game_dirs():
        if p not in cands:
            cands.append(p)
    cands = [p for p in cands
             if os.path.normcase(p) != os.path.normcase(cur)]
    for p in cands:
        if has_games(p):
            set_app_game_dir(p)
            return True, p, cur
    # 一个都不像：把指向不存在 / 空的 override 收拾掉，回到默认
    if app_game_override():
        set_app_game_dir("")
    return False, "", ""


def _explicit_root() -> str:
    """明确配置过的安装目录（向导写的 / 测试注入的），没配置返回空串"""
    env = os.environ.get("MCASSIST_INSTALL_DIR")
    if env:
        return os.path.abspath(env)
    cfg = str(CONFIG.get("install_dir") or "").strip()
    return os.path.abspath(cfg) if cfg else ""


def install_root() -> str:
    """安装目录（Osmanthus flower）。

    优先级：`MCASSIST_INSTALL_DIR`（测试）→ 安装向导写进配置的 install_dir →
    exe 所在目录（打包后）→ 配置里游戏目录的上层（绿色版 / 开发模式）→ 项目根。
    """
    root = _explicit_root()
    if root:
        return root
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    gd = str(CONFIG.get("game_dir") or "").strip()
    if gd:
        return os.path.dirname(os.path.abspath(gd))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def app_game_dir(root: str | None = None) -> str:
    """应用路径：纯属于这个启动器的 .minecraft

    没指定 `root` 时先看 `APP_OVERRIDE_KEY`（第六十七轮补丁）：换过安装目录、或者
    手动把应用路径指到旧的那份 `.minecraft` 时，一切（下载 / 独立启动 / 游戏管理 /
    设置概览）都跟着它走，不会出现「游戏管理空着、游戏却还在硬盘上」。

    没有安装目录信息时（绿色版 / 开发模式 / 从没跑过安装向导的老玩家）
    直接沿用配置里那个游戏目录 —— 不能把下载内容丢到 exe 旁边一个陌生的
    `.minecraft` 里，那样玩家的老版本、存档全都「看不见」了。
    """
    if root:
        return os.path.join(root, APP_DIR_NAME)
    ov = app_game_override()
    if ov and os.path.isdir(ov):
        return ov
    if _explicit_root():
        return os.path.join(install_root(), APP_DIR_NAME)
    gd = str(CONFIG.get("game_dir") or "").strip()
    if gd:
        return os.path.abspath(gd)
    return os.path.join(install_root(), APP_DIR_NAME)


def official_game_dir(root: str | None = None) -> str:
    """官方路径：`<安装目录>\\official_assets\\.minecraft`"""
    return os.path.join(root or install_root(), OFFICIAL_DIR, APP_DIR_NAME)


# ---------------------------------------------------------------- 下载目标
def standalone_on() -> bool:
    """「独立启动」开关是不是开着（默认关）。

    第四十六轮：这个开关决定**默认**看哪条目录 ——
      关着 → 游戏只能靠官方启动器跑，默认看「官方」；
      开着 → 玩家用的是本启动器自己的目录，默认看「应用」。
    """
    try:
        return bool(CONFIG.get("standalone_launch"))
    except Exception:
        return False


def default_target() -> str:
    """没明确选过目标时的默认值（第 114 轮：跟 target() 同一套仲裁）。

    独立启动开着 → app；没装官启 → app（官启目录没消费者）；否则官方。
    """
    if standalone_on():
        return "app"
    return "app" if not official_launcher_available() else DEFAULT_TARGET


def explicit_target() -> str:
    """玩家明确点选过的目标；没选过返回空串（= 跟随默认）"""
    v = str(CONFIG.get(TARGET_KEY) or "").strip().lower()
    return v if v in TARGETS else ""


# ---------------------------------------------------------------- 官方启动器是否在场
# 第 114 轮：`find_launcher_exe()` 末尾对商店版是**无条件**返回 aumid 兜底，
# 没装也会返回，不能拿它当「本机有没有官启」的判据。这里独立做严格检测，
# 并且进程内缓存 —— target() 被列表渲染高频调用，不能每次扫注册表。
_OFFICIAL_AVAIL_CACHE = None


def reset_official_cache():
    """配置里改过官启路径 / 装完卸完官启后调一下，下次 target() 重新检测"""
    global _OFFICIAL_AVAIL_CACHE
    _OFFICIAL_AVAIL_CACHE = None


def _official_via_registry() -> bool:
    """注册表卸载项里有没有 Minecraft Launcher（只在文件检测全落空时才跑，缓存一次）"""
    if os.name != "nt":
        return False
    try:
        import winreg
    except Exception:
        return False
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                k = winreg.OpenKey(root, sub)
            except OSError:
                continue
            with k:
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(k, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(k, name) as sk:
                            dn, _ = winreg.QueryValueEx(sk, "DisplayName")
                    except OSError:
                        continue
                    if "Minecraft" in str(dn) and "Launcher" in str(dn):
                        return True
    return False


def official_launcher_available() -> bool:
    """本机真的装了官方启动器吗（严格检测 + 进程内缓存）。

    认这四样里的任意一个：玩家手动指定的 exe、标准安装目录、商店版包目录、
    注册表卸载项；另外「官启目录里已经有版本 / 档案」也算 —— 真在用官启体系
    的玩家不能被误判成没官启而赶到应用目录。
    """
    global _OFFICIAL_AVAIL_CACHE
    if _OFFICIAL_AVAIL_CACHE is not None:
        return _OFFICIAL_AVAIL_CACHE
    ok = False
    try:
        custom = str(CONFIG.get("launcher_path") or "").strip()
        if custom and os.path.isfile(custom):
            ok = True
    except Exception:
        pass
    if not ok:
        for d in (r"C:\Program Files (x86)\Minecraft Launcher",
                  r"C:\Program Files\Minecraft Launcher"):
            for exe in ("MinecraftLauncher.exe", "Minecraft Launcher.exe", "Minecraft.exe"):
                if os.path.isfile(os.path.join(d, exe)):
                    ok = True
                    break
            if ok:
                break
    if not ok:
        pkg = os.path.expandvars(
            r"%LOCALAPPDATA%\Packages\Microsoft.4297127D64EC6_8wekyb3d8bbwe")
        ok = os.path.isdir(pkg)
    if not ok:
        try:
            off = official_game_dir()
            vs = os.path.join(off, "versions")
            ok = (os.path.isfile(os.path.join(off, "launcher_profiles.json")) or
                  (os.path.isdir(vs) and any(os.path.isdir(os.path.join(vs, n))
                                             for n in os.listdir(vs))))
        except OSError:
            pass
    if not ok:
        ok = _official_via_registry()
    _OFFICIAL_AVAIL_CACHE = ok
    return ok


def target() -> str:
    """当前目标：第 114 轮统一成一条仲裁规则（不再两个开关各说各话）。

    优先级从高到低：
      1. 独立启动开着 → 一定 "app"。这是主开关，压过任何 sticky 的显式目标 ——
         旧版 `download_target` 一旦写成 official 就永久盖住开关，正是
         「开了独立启动还往官方目录下载」的病根；
      2. 本机没装官方启动器 → 一定 "app"。官方那份 .minecraft 没有消费者，
         装进去等于白发，没官启的人不该被默认赶到这条死路；
      3. 其余（非独立启动 + 有官启）→ 尊重玩家显式选过的目标，
         没选过就走官方（老基线不变）。

    下载 / 游戏管理 / 选择要启动的游戏三处共用这一份。
    """
    if standalone_on():
        return "app"
    if not official_launcher_available():
        return "app"
    return explicit_target() or DEFAULT_TARGET


def set_target(v: str, remember=True):
    """设为某个目标（remember=False 或非法值 = 回到「跟随默认」）"""
    v = str(v or "").strip().lower()
    CONFIG.set(TARGET_KEY, v if (remember and v in TARGETS) else "")


def reset_target():
    """清掉玩家的明确选择，回到「跟随独立启动」的默认"""
    CONFIG.set(TARGET_KEY, "")


def target_is_explicit() -> bool:
    return bool(explicit_target())


TARGET_AUTO_KEY = "target_auto_v46"


def init_target_rule() -> bool:
    """第四十六轮的一次性整理：把上一版写死的 `download_target` 清掉。

    第四十五轮安装向导会直接写 `download_target="app"`，那一版「默认」等于写死「应用」；
    这版改成「默认跟随独立启动开关」（关=官方，开=应用）。老配置里那条明确值会盖住
    新规则，所以升级后清一次，让玩家回到「跟随默认」。返回 True = 这次真的清了。
    """
    if CONFIG.get(TARGET_AUTO_KEY):
        return False
    CONFIG.set(TARGET_KEY, "")
    CONFIG.set(TARGET_AUTO_KEY, True)
    return True


def target_dir(v: str = "") -> str:
    v = (v or target()).lower()
    if v == "official":
        return official_game_dir()
    return app_game_dir()


def download_dir() -> str:
    """当前「下载 / 安装」应该落到哪个目录"""
    d = target_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


# ---------------------------------------------------------------- 加载器版本
def loaders_dir() -> str:
    """专门存放 Forge / NeoForge / Fabric / Quilt 加载器版本 json 的目录。

    第一百零九轮+：加载器版本（如 `neoforge-21.1.248`）和整合包实例都堆在
    `.minecraft/versions/` 里，玩家在「游戏管理 / 选择要启动的游戏」里会看到一堆
    `neoforge-xxx` `forge-xxx` 这种不是游戏的条目。把它们单独挪到安装目录下的
    `loaders/`（与 .minecraft 平级），既能从列表里彻底消失，又能让整合包实例不再
    依赖 `versions/<加载器>` 那一份（配合 repair_runtime 的 MergeJson 合并）。
    版本目录名（neoforge-21.1.248）保持不变，只换存放位置。
    """
    return os.path.join(install_root(), "loaders")


def version_json_lookup(game_dir: str, vid: str) -> str:
    """找 `versions/<vid>/<vid>.json`：先在本游戏目录里找，找不到再去全局 loaders/ 找。

    同时兼容两种布局 —— 加载器还在 `.minecraft/versions` 里的旧布局，以及挪到
    `loaders/` 里的新布局。返回第一个存在的路径；都不存在就回退到
    `game_dir/versions/<vid>/<vid>.json`（让上层按原逻辑报「找不到」）。
    """
    if vid:
        p = os.path.join(str(game_dir or ""), "versions", vid, vid + ".json")
        if os.path.isfile(p):
            return p
        lp = os.path.join(loaders_dir(), vid, vid + ".json")
        if os.path.isfile(lp):
            return lp
    return os.path.join(str(game_dir or ""), "versions", vid or "",
                        (vid or "") + ".json")


def ensure_dir(which: str = "", source: str = "") -> dict:
    """把某条路径整理成能用的 .minecraft（骨架 + 零拷贝共享），返回 gamedir 的信息"""
    p = target_dir(which)
    src = source
    if not src:
        # 目标还是空的，就看看本机有没有现成的游戏数据可以联过去
        src = gamedir.find_source(exclude=p)
    return gamedir.ensure_game_dir(p, source=src)


# ---------------------------------------------------------------- 迁移
def _same(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except Exception:
        return False


def _is_link(path: str) -> bool:
    return gamedir.is_junction(path) or os.path.islink(path)


def _has_content(path: str) -> bool:
    """里面真的装了东西吗（只有空骨架不算）"""
    if not path or not os.path.isdir(path):
        return False
    if os.path.isfile(os.path.join(path, "launcher_profiles.json")):
        return True
    for name in ("versions", "libraries", "assets", "mods", "saves",
                 "resourcepacks", "shaderpacks"):
        sub = os.path.join(path, name)
        try:
            if os.path.isdir(sub) and os.listdir(sub):
                return True
        except OSError:
            pass
    return False


def _move_dir(src: str, dst: str) -> bool:
    """同卷整体改名（瞬时）；跨卷或失败就返回 False，交给调用方退化处理"""
    if not os.path.isdir(src) or os.path.isdir(dst):
        return False
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
    except OSError:
        return False
    try:
        os.replace(src, dst)
        return True
    except OSError:
        return False


def _drop_link(path: str) -> bool:
    """只删链接本体（junction / 符号链接），不动它指向的真实数据"""
    try:
        if os.path.islink(path):
            os.unlink(path)
            return True
        if gamedir.is_junction(path):
            os.rmdir(path)          # junction 用 rmdir 删，只是摘掉链接
            return True
    except OSError:
        pass
    return False


def split_legacy() -> str:
    """第四十五轮的一次性迁移：把老的单一 `.minecraft` 归到「官方」路径。

    返回一句给玩家看的中文说明（空串 = 没什么可说的）。**只跑一次**。
    """
    if CONFIG.get(SPLIT_KEY):
        return ""
    app = app_game_dir()
    off = official_game_dir()
    old = str(CONFIG.get("game_dir") or "").strip()
    note = ""

    try:
        if old and not _same(old, app) and _has_content(old):
            # 玩家以前把游戏目录指到别处（或从没装过向导，用的是 %APPDATA%\.minecraft）
            # → 那一份就是官方数据，用目录联接挂进 official_assets，一个字节都不搬
            gamedir.ensure_game_dir(off, source=old)
            note = (f"检测到原有的游戏数据在 {old}，已接到「官方」路径下"
                    f"（目录联接共享，文件没有移动）。")
            reset_target()          # 迁移后跟随「独立启动」开关决定默认看哪条
        elif _has_content(app):
            if _is_link(app):
                real = os.path.realpath(app)
                gamedir.ensure_game_dir(off, source=real)
                _drop_link(app)
                note = ("应用目录原本指向外部游戏数据，已把那份数据归到「官方」路径，"
                        "应用目录重新建成独立的空目录。")
                reset_target()          # 迁移后跟随「独立启动」开关决定默认看哪条
            elif _move_dir(app, off):
                _rebase_after_move(app, off)
                note = ("原有的 .minecraft 已整体移入「官方」路径"
                        "（official_assets），下载时选「官方」就能看到全部老内容。")
                reset_target()          # 迁移后跟随「独立启动」开关决定默认看哪条
            else:
                gamedir.ensure_game_dir(off, source=app)
                note = "原有的游戏数据已通过目录联接共享到「官方」路径。"
                reset_target()          # 迁移后跟随「独立启动」开关决定默认看哪条
        else:
            gamedir.ensure_game_dir(off)
    except Exception as e:                      # noqa: BLE001
        note = f"整理游戏目录时出了点小问题：{str(e)[:60]}（可稍后在设置里查看）"
        try:
            from . import crashlog
            crashlog.note(f"paths.split_legacy 失败：{e}")
        except Exception:
            pass

    # 应用目录自己也要有个能用的骨架（独立启动要用它）
    try:
        gamedir.ensure_game_dir(app)
    except Exception:
        pass

    CONFIG.set("game_dir", app)
    CONFIG.set(SPLIT_KEY, True)
    return note


def find_source_for(*exclude) -> str:
    """找一个能共享的现成游戏目录，排除 exclude 里那几个"""
    ex = set()
    for x in exclude:
        if not x:
            continue
        try:
            ex.add(os.path.normcase(os.path.abspath(x)))
        except Exception:
            pass
    for c in gamedir.candidates():
        try:
            k = os.path.normcase(os.path.abspath(c))
        except Exception:
            continue
        if k in ex:
            continue
        if gamedir.looks_like_mc(c):
            return os.path.abspath(c)
    return ""


def write_probe(path: str) -> "tuple[bool, str]":
    """能不能往这个目录里写东西。

    安装向导靠它**提前**探一次：装到 C:\\Program Files 这种地方，第三秒就知道不行，
    而不是复制完 exe、建完快捷方式才炸出一串 WinError 5。
    """
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        return False, str(e)
    tmp = os.path.join(path, f".write_test_{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(b"ok")
        os.remove(tmp)
        return True, ""
    except OSError as e:
        return False, str(e)


def prepare_pair(app: str, official: str) -> dict:
    """安装向导用：把「应用路径」「官方路径」两条目录一次整理好。

    返回 {"app","official","note","moved","source","error"}；note 是给玩家看的中文说明。

    处理顺序（每一步都只在「目标还是空壳」时动手，绝不动已有真数据）：
      1. 安装目录里老的那个 `.minecraft` 有真内容 → 整体改名移进官方路径；
      2. 应用目录是个目录联接（老版本把外部数据联进来了）→ 摘链，真实数据归官方；
      3. 官方路径还是空的 → 找本机现成数据（%APPDATA%\\.minecraft / 商店版）零拷贝共享；
      4. 应用路径自己建空骨架（独立启动用，不和官方混）。
    """
    out = {"app": "", "official": "", "note": "", "moved": False,
           "source": "", "error": ""}
    bits = []
    try:
        a = os.path.abspath(app)
        o = os.path.abspath(official)
        if _same(a, o):
            # 两条路径指同一个目录就失去意义了，官方那条退回默认位置
            o = official_game_dir()
            bits.append("官方路径不能和应用路径相同，已改回默认位置")
        out["app"], out["official"] = a, o

        if _has_content(a) and not _is_link(a) and not _has_content(o):
            if _move_dir(a, o):
                out["moved"] = True
                _rebase_after_move(a, o)
                bits.append("原有的 .minecraft 已整体移入官方路径"
                            "（同一个盘里改名，文件没有复制）")
            else:
                gamedir.ensure_game_dir(o, source=a)
                out["source"] = a
                bits.append("原有的游戏数据已通过目录联接共享到官方路径"
                            "（不在同一个盘，没有复制文件）")
        elif _is_link(a) and not _has_content(o):
            real = os.path.realpath(a)
            if _has_content(real):
                gamedir.ensure_game_dir(o, source=real)
                out["source"] = real
                _drop_link(a)
                bits.append(f"应用目录原本指向 {real}，那份数据已归到官方路径"
                            f"（目录联接共享，文件没有移动）")

        if not _has_content(o):
            src = find_source_for(a, o)
            if src:
                out["source"] = src
                bits.append(f"官方路径已零拷贝共享现有游戏数据：{src}")

        if gamedir.looks_like_mc(o):
            # 已经是能用的官方目录（玩家自己指定的位置也在这一支）——一个字节都不碰
            info_o = {"error": ""}
        elif out["source"]:
            info_o = gamedir.ensure_game_dir(o, source=out["source"])
        else:
            info_o = gamedir.ensure_game_dir(o)
        if info_o.get("error"):
            out["error"] = info_o["error"]

        info_a = gamedir.ensure_game_dir(a)
        if info_a.get("error"):
            out["error"] = info_a["error"]
        elif not _has_content(a):
            bits.append("应用路径已建好（独立于官方，第一次下载时自动填充）")
    except Exception as e:                       # noqa: BLE001
        out["error"] = str(e)
    out["note"] = "；".join(bits)
    return out


# 第一百零一轮：这些键存的是「名字 / 版本号 / 类型 / 时间 / 档案 id」，
# **不是路径**。旧版搬总目录时 walk() 无差别递归，把它们也当绝对路径切片，
# 于是 added_groups 五个文件夹的 name 全变成 `D:\MC\Osmanthus flower`
# （玩家截图里五行同名就是这么来的）、added_dirs 的 kind 全坏导致
# 「版本」被当成「整合包」。即使修好了 _looks_abs_path，只要这些字段的值
# 恰好长得像路径（玩家自己起的名、或历史坏值），下次搬家还会被改。
# 这里是纯收紧：名单里没有任何一个字段需要跟着总路径搬。
_NON_PATH_FIELDS = frozenset({
    "name", "kind", "mc", "loader", "loader_ver", "source", "version_id",
    "time", "group_name", "profile_id", "icon", "icon_kind", "icon_src",
})


def _looks_abs_path(s: str) -> bool:
    """这串字看起来是不是一个**绝对路径**（`D:\\x` / `\\\\server\\sh`）。

    第九十九轮：`fix()` 以前直接用 `os.path.abspath(s)` 判，而 abspath 会把
    **相对字面量按当前工作目录**展开 —— 启动器的工作目录一旦正好在旧总路径里，
    `"1.12.2-forge-14.23.5.2860"`（版本号）、`"1.20.1"`（mc 号）、
    甚至模组名都会被判成「在旧目录下」，然后被切成
    `新总路径 + 原串[22:]` —— 于是导入登记表里所有 name / mc / version_id /
    kind 全变成 `E:\\Osmanthus flower860` 这种鬼东西（玩家截图里那一串
    「一模一样」的整合包就是这么来的）。
    """
    if not isinstance(s, str) or len(s) < 3:
        return False
    if s[0] == "\\" and s[1] == "\\":
        return True                       # UNC
    return s[1] == ":" and s[2] in "\\/"


def _rebase_after_move(old: str, new: str) -> int:
    """把配置里还指在老目录里的绝对路径改到新位置（迁移之后立刻调用）。

    搬运是一次改名，但玩家的「独立启动目标」「已导入文件夹」存的都是绝对路径，
    不改的话升级完点一下就是「目录不存在」。只动这几类键，不碰别的。

    第九十九轮补三处：
      · 只认**看起来像绝对路径**的字符串（见 `_looks_abs_path`）；
      · 以路径为键的表（`imported` / `added_removed`）键本身也要跟着改，
        否则旧键永远匹配不上，删不掉也去不了重 —— 同一个实例会越攒越多；
      · `profiles_wanted`（官启档案登记表）的 `game_dir` 以前一个字节都没动，
        搬完官方启动器那边全是死路径。
    """
    if not old or not new:
        return 0
    old_abs = os.path.abspath(old)
    o = os.path.normcase(old_abs)
    n = os.path.abspath(new)
    if o == os.path.normcase(n):
        return 0
    changed = 0

    def fix(s):
        nonlocal changed
        if not isinstance(s, str) or not s:
            return s
        if not _looks_abs_path(s):
            return s
        try:
            k = os.path.normcase(os.path.abspath(s))
        except Exception:
            return s
        if k == o or k.startswith(o + os.sep):
            changed += 1
            return n + s[len(old_abs):]
        return s

    def walk(v):
        if isinstance(v, str):
            return fix(v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            # 语义字段整支跳过：键本身不是路径，值也不该跟着总路径搬。
            # 只跳过值，字典的**键**仍由 _PATH_KEYED 那段单独重绑。
            return {k: (x if k in _NON_PATH_FIELDS else walk(x))
                    for k, x in v.items()}
        return v

    # 以「路径」为键的表：**键也要改**（第九十九轮）。键是 normcase(realpath)，
    # 不改的话 `forget_import()` / `delete_game_instance()` 按新路径都删不掉旧键，
    # 于是同一份实例被反复登记，「游戏管理」里就会出现好几条一模一样的记录。
    _PATH_KEYED = ("imported", "added_removed")
    for key in _PATH_KEYED:
        cur = CONFIG.get(key)
        if not isinstance(cur, dict) or not cur:
            continue
        out = {}
        for k, v in cur.items():
            nk = fix(k) if isinstance(k, str) else k
            out[nk] = walk(v)
        if out != cur:
            CONFIG.set(key, out)

    for key in ("launch_target", "added_dirs", "added_groups",
                # 第一百零五轮：分类的 members 是 normcase(realpath(游戏目录))，
                # 跟 launch_target 一样必须跟着总路径搬；name / kind / time 已在
                # _NON_PATH_FIELDS 里，walk() 会整支跳过，不会把分类名改成路径。
                "game_categories",
                # profiles_wanted：官启档案登记表，game_dir / root 都是绝对路径
                "profiles_wanted", "installed"):
        cur = CONFIG.get(key)
        if cur:
            CONFIG.set(key, walk(cur))
    # game_settings 的键是 normcase(realpath(路径))：键也要跟着改，否则每游戏覆盖失效
    gs = CONFIG.get("game_settings") or {}
    if isinstance(gs, dict):
        out = {fix(k): v for k, v in gs.items()}
        if out != gs:
            CONFIG.set("game_settings", out)
    return changed


def status() -> dict:
    """给设置页 / 探针看的两条路径状态"""
    app = app_game_dir()
    off = official_game_dir()
    return {
        "root": install_root(),
        "app": app, "app_exists": os.path.isdir(app),
        "official": off, "official_exists": os.path.isdir(off),
        "target": target(), "official_dir": os.path.join(install_root(), OFFICIAL_DIR),
    }


def legacy_official_dir_name() -> str:
    """官方数据子目录名（界面上要展示）"""
    return f"{INSTALL_DIR_NAME}\\{OFFICIAL_DIR}"


def remove_dir_quietly(path: str):
    """删一个目录（只在探针 / 清理里用；真删不到就静默）"""
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
