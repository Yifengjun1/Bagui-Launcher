# -*- coding: utf-8 -*-
"""整合包运行基础（版本 JSON / client jar / libraries / assets）的解析、复用与补全。

为什么需要它：
官方启动器点「开始游戏」时，会先按档案的 lastVersionId 找到
`<游戏目录>/versions/<id>/<id>.json`，再准备 libraries 与 assets。
只把 mods 放进实例目录是启动不了的，表现为
    「游戏启动失败！内部错误。名称: REQUEST_FAILED
      错误详情: Unable to prepare assets for download」
或 lastVersionId 为空导致启动器根本无法解析版本。

策略（快 -> 慢）：
1. 游戏目录里已有匹配版本 -> 直接用；
2. 扫描本机其它启动器的 .minecraft（PCL2 / HMCL / 官方商店版等）找匹配版本
   -> 复制版本目录 + 用目录联接共享 libraries/assets（不占额外空间）；
3. 都找不到且只需原版 -> 从 Mojang 官方源补 json + client.jar + assetIndex。
Forge/NeoForge 无法凭空补（要跑官方安装器），此时返回明确指引。
"""
import json
import os
import re
import shutil
import subprocess

import requests

UA = "MC-GuanQi-Fuzhu-Gongju/1.0.0"
MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"

LOADER_KEYS = {
    "net.minecraftforge:forge": "forge",
    # 1.20.1+ 的 Forge 用 fmlloader 承载版本号（没有 net.minecraftforge:forge 坐标时靠它认）
    "net.minecraftforge:fmlloader": "forge",
    "net.neoforged:neoforge": "neoforge",
    "net.neoforged.fancymodloader:loader": "neoforge",
    "net.fabricmc:fabric-loader": "fabric",
    "org.quiltmc:quilt-loader": "quilt",
}
# 主坐标：优先用它们认加载器版本（辅助坐标可能带的是内部组件版本）
LOADER_PRIMARY = {"net.minecraftforge:forge", "net.neoforged:neoforge",
                  "net.fabricmc:fabric-loader", "org.quiltmc:quilt-loader"}
# 这些坐标只能用来判断「是哪个加载器」，版本号是内部组件版本（如 4.0.45），不能当加载器版本
LOADER_VER_SKIP = {"net.neoforged.fancymodloader:loader",
                   "net.minecraftforge:fmlloader"}
LOADER_CN = {"forge": "Forge", "neoforge": "NeoForge", "fabric": "Fabric", "quilt": "Quilt"}

_MC_RE = re.compile(r"1\.\d+(?:\.\d+)?")
_CACHE_ROOTS = None
_CACHE_VERSIONS = None


# ---------------------------------------------------------------- 基础读取
def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _maven_parts(name: str):
    """'net.minecraftforge:forge:1.20.1-47.4.18' -> ('net.minecraftforge:forge', '1.20.1-47.4.18')"""
    if not name:
        return "", ""
    parts = name.split(":")
    if len(parts) < 3:
        return "", ""
    return ":".join(parts[:2]), parts[2]


def _pack_loader_ver(vdir, d, kind):
    """整合包「实例壳」没写加载器主坐标时，把版本号从别处找回来。

    第一百零二轮：PCL2 / PCLCE 导出的整合包，版本 json 是一份瘦壳 ——
    启动参数和 Forge 主坐标都交给 `inheritsFrom` 继承，自己只留 fmlloader
    这类辅助坐标（在 `LOADER_VER_SKIP` 里会被清成空）。于是 `loader_ver`
    丢了，开局前 `_fix_loader` 拿空版本号判「没装全」，再用空版本号去补装
    （拼出来的下载 URL 版本段是空的，必然失败），玩家怎么重装都被拦在门外
    —— 其实 `libraries` 里 universal / client 两份 jar 一直是齐的。

    兜底顺序（先信结构化信息，最后才用坐标）：我们导入时写的
    `.mcassist.json` → 基底版本 id（inheritsFrom / mcassist.base）→
    Forge 1.20.1+ 的 fmlloader 坐标。目录名兜底仍由调用方保留。
    """
    try:
        meta = _read_json(os.path.join(vdir, ".mcassist.json")) or {}
    except Exception:                                    # noqa: BLE001
        meta = {}
    if str(meta.get("loader") or "").lower() == kind:
        lv = str(meta.get("loader_ver") or "").strip()
        if lv:
            return lv
    # 基底版本 id 形如 1.20.1-forge-47.4.10 / neoforge-21.1.172
    for s in (str(meta.get("base_version") or ""),
              str(d.get("inheritsFrom") or ""),
              str((d.get("mcassist") or {}).get("base") or "")):
        m = re.search(r"(?:forge|neoforge)[-_](\d[\w.+-]*)\s*$", s, re.I)
        if m:
            return m.group(1)
    # Forge 1.20.1+ 用 fmlloader 承载版本号：
    #   net.minecraftforge:fmlloader:1.20.1-47.4.10  →  47.4.10
    # 只对 forge 用；NeoForge 的 fancymodloader:loader:4.0.45 不是主版本号，
    # 拿它当 neoforge 版本会把加载器认错（正是 LOADER_VER_SKIP 存在的原因）。
    if kind == "forge":
        for lib in (d.get("libraries") or []):
            nm = str(lib.get("name") or "")
            if "fmlloader" not in nm.lower():
                continue
            parts = nm.split(":")
            if len(parts) < 3:
                continue
            v = re.sub(r"^\d+\.\d+(?:\.\d+)?-", "", parts[-1])
            if re.match(r"^\d", v):
                return v
    return ""


def describe_version(vdir: str):
    """解析一个 versions/<id> 目录，返回版本描述（无法识别返回 None）"""
    if not os.path.isdir(vdir):
        return None
    name = os.path.basename(vdir.rstrip("\\/"))
    jpath = os.path.join(vdir, name + ".json")
    if not os.path.isfile(jpath):
        # 目录名与 json 名不一致时（PCL 重命名过）取第一个 json
        cands = [f for f in os.listdir(vdir)
                 if f.lower().endswith(".json") and f.lower() != "emi.json"]
        if not cands:
            return None
        jpath = os.path.join(vdir, cands[0])
    d = _read_json(jpath)
    if not d or "libraries" not in d and "inheritsFrom" not in d:
        return None

    kind, lver = "", ""
    has_neoforge = False
    libs = d.get("libraries") or []
    for lib in libs:
        if str(lib.get("name", "")).startswith("net.neoforged"):
            has_neoforge = True
    # 先只认「主坐标」，再退到辅助坐标：NeoForge 的 libraries 里
    # net.neoforged.fancymodloader:loader:4.0.45 排在 net.neoforged:neoforge:21.1.264 前面，
    # 一趟扫下来会把加载器版本误记成 4.0.45。
    for primary_only in (True, False):
        for lib in libs:
            key, ver = _maven_parts(str(lib.get("name", "")))
            if primary_only and key not in LOADER_PRIMARY:
                continue
            if key in LOADER_KEYS:
                kind = LOADER_KEYS[key]
                lver = "" if key in LOADER_VER_SKIP else ver
                break
        if kind:
            break
    if kind and not lver:
        # 第一百零二轮：整合包实例壳靠 inheritsFrom 继承加载器主坐标，
        # 先从 .mcassist.json / 基底 id / fmlloader 把版本号找回来（见上）。
        lver = _pack_loader_ver(vdir, d, kind)
    if kind and not lver:
        # 加载器坐标没带主版本号时，从目录名兜底：neoforge-21.1.264 / 1.20.1-forge-47.4.18
        m = re.search(rf"{kind}[-_]?(\d[\w.+-]*)", name, re.I)
        if m:
            lver = m.group(1)
    if not kind:
        # 极少数整合包把加载器库裁掉了，只能看主类判断
        main = str(d.get("mainClass") or "")
        if "bootstraplauncher" in main or "modlauncher" in main:
            kind = "neoforge" if has_neoforge else "forge"
        elif "fabric" in main or "quilt" in main:
            kind = "fabric" if "fabric" in main else "quilt"

    # 优先 `clientVersion`（PCL2 合并版、整合包实例壳都靠它写「这是哪个原版」）；
    # 没有才退回 `inheritsFrom` —— 实例壳的 inheritsFrom 指向的是**加载器版本**
    # （如 1.20.1-forge-47.4.18），直接拿来当 mc 号会让启动器挑错 Java。
    mc = str(d.get("clientVersion") or "")
    if not mc:
        mc = str(d.get("inheritsFrom") or "")
    if not mc:
        if kind in ("forge", "neoforge") and "-" in lver:
            mc = lver.split("-", 1)[0]
        if not mc:
            m = _MC_RE.search(name) or _MC_RE.search(str(d.get("id") or ""))
            mc = m.group(0) if m else ""
    if not mc:
        # 年份命名（26.3 / 26.4-snapshot-5）走这里
        mc = mc_from_id(name) or mc_from_id(str(d.get("id") or ""))
    if not mc:
        idx = (d.get("assetIndex") or {}).get("id")
        if idx and str(idx) in ("5", "1.20"):
            mc = "1.20.1"

    idx = (d.get("assetIndex") or {}).get("id") or ""
    jar = os.path.join(vdir, name + ".jar")
    if not os.path.isfile(jar):
        cands = [f for f in os.listdir(vdir) if f.lower().endswith(".jar")]
        jar = os.path.join(vdir, cands[0]) if cands else ""
    return {"id": str(d.get("id") or name), "dir": vdir, "json": jpath,
            "mc": mc, "loader": kind, "loader_ver": lver,
            # 是不是「整合包实例目录」（带我们写的 mcassist 标记）：
            # 这种目录只是某个包的运行壳，绝不能当成可复用的运行版本搬去给别的包用
            "pack": bool(d.get("mcassist")),
            # type：release / snapshot / old_beta / old_alpha
            # 「非正版隔离」要按它判断（快照、愚人节版本一律没有正式版号）
            "type": str(d.get("type") or "").lower(),
            "assets": str(idx), "jar": jar if os.path.isfile(jar) else "",
            "source": vdir}


# ---------------------------------------------------------------- 扫描本机
def _drive_roots():
    roots = []
    for ch in "CDEFGH":
        p = ch + ":\\"
        if os.path.isdir(p):
            roots.append(p)
    return roots


def candidate_roots(extra=None):
    """本机可能存在的 .minecraft 根目录（带缓存）"""
    global _CACHE_ROOTS
    if _CACHE_ROOTS is not None and not extra:
        return list(_CACHE_ROOTS)
    roots = []
    for p in list(extra or []):
        if p:
            roots.append(os.path.realpath(p))
    appdata = os.path.expandvars(r"%APPDATA%\.minecraft")
    if os.path.isdir(appdata):
        roots.append(os.path.realpath(appdata))
    local_mc = os.path.expandvars(
        r"%LOCALAPPDATA%\Packages\Microsoft.4297127D64EC6_8wekyb3d8bbwe\LocalCache\Roaming\.minecraft")
    if os.path.isdir(local_mc):
        roots.append(os.path.realpath(local_mc))
    # 常见第三方启动器目录 + 各盘浅层扫描（PCL2/HMCL 常把 .minecraft 放在自己旁边）
    skip = {"windows", "program files", "program files (x86)", "programdata",
            "system volume information", "$recycle.bin", "users", "appdata",
            "node_modules", ".git", "perflogs", "recovery", "intel"}
    for base in _drive_roots():
        for i in range(1, 4):
            if i == 1:
                dirs = [base]
            else:
                dirs = []
            for d in dirs:
                try:
                    entries = os.listdir(d)
                except Exception:
                    continue
                for n in entries:
                    p = os.path.join(d, n)
                    try:
                        if not os.path.isdir(p):
                            continue
                    except Exception:
                        continue
                    low = n.lower()
                    if low in skip:
                        continue
                    if low in (".minecraft", "minecraft"):
                        roots.append(os.path.realpath(p))
                    elif i < 3:
                        try:
                            for n2 in os.listdir(p):
                                if n2.lower() in (".minecraft", "minecraft"):
                                    q = os.path.join(p, n2)
                                    if os.path.isdir(q):
                                        roots.append(os.path.realpath(q))
                        except Exception:
                            pass
    seen, out = set(), []
    for r in roots:
        k = os.path.normcase(r)
        if k not in seen and os.path.isdir(r):
            seen.add(k)
            out.append(r)
    if not extra:
        _CACHE_ROOTS = out
    return out


def scan_versions(roots=None):
    """扫描所有根目录下的 versions/*，返回描述列表"""
    out = []
    for root in (roots or candidate_roots()):
        vdir = os.path.join(root, "versions")
        if not os.path.isdir(vdir):
            continue
        try:
            names = os.listdir(vdir)
        except Exception:
            continue
        for n in names:
            info = describe_version(os.path.join(vdir, n))
            if not info:
                continue
            # 整合包实例也是 versions 里的一个目录，但它带着 mods/存档，是「一台实例」
            # 而不是可复用的运行版本 —— 拿它去当别的整合包的运行基础，模组会串台
            # （本软件造的壳会在 json 里留 mcassist 标记，别的启动器的实例靠 .mcassist.json）。
            if _read_json(info["json"]).get("mcassist"):
                continue
            if os.path.isfile(os.path.join(info["dir"], ".mcassist.json")):
                continue
            info["root"] = root
            info["assets_index"] = os.path.join(root, "assets", "indexes",
                                                info["assets"] + ".json") if info["assets"] else ""
            out.append(info)
    return out


def _score(v, mc, kind, lver):
    if mc and v["mc"] != mc:
        return -1
    s = 0
    if mc:
        s += 40
    if kind:
        if v["loader"] != kind:
            return -1
        s += 30
    elif v["loader"]:
        s -= 5           # 只要原版时，纯原版优先
    if lver and v["loader_ver"]:
        s += 25 if v["loader_ver"] == lver else -10
    if v["jar"]:
        s += 10
    if v["assets"] and os.path.isfile(v.get("assets_index") or ""):
        s += 10
    return s


def find_local(mc, kind="", lver="", roots=None):
    best, best_s = None, 0
    for v in scan_versions(roots):
        if v.get("pack"):
            continue                     # 别的包的实例壳：不能拿去当运行版本
        s = _score(v, mc, kind, lver)
        if s > best_s:
            best, best_s = v, s
    return best


def _same_loader_ver(a, b, mc=""):
    """'1.20.1-47.4.10' 与 '47.4.10' 算同一个加载器版本（forge 的坐标带原版前缀）"""
    def norm(v):
        v = str(v or "").strip().lower()
        if mc and v.startswith(str(mc).lower() + "-"):
            v = v[len(mc) + 1:]
        return v
    na, nb = norm(a), norm(b)
    return bool(na) and na == nb


def find_local_exact(mc, kind, lver, roots=None):
    """只认「原版号 + 加载器 + 加载器版本」全都对得上的本机版本。

    Forge/NeoForge 换一个小版本就不是同一套（fmlloader / 各处理器产物都不一样），
    拿别的版本顶上比没有更糟：游戏会在 ModLauncher 阶段直接崩。
    以前 `ensure_runtime` 会退到「随便找一个同加载器的版本」，结果把整合包
    绑到另一个整合包的实例上（现场就搬进来一个 `FarmingTales_ForgeⅡ`）。
    """
    for v in scan_versions(roots):
        if v.get("pack"):
            continue
        if (v.get("mc") != mc or v.get("loader") != kind):
            continue
        if not _same_loader_ver(v.get("loader_ver"), lver, mc):
            continue
        return v
    return None


# ---------------------------------------------------------------- 目录联接
def is_junction(path):
    if not path or not os.path.exists(path):
        return False
    try:
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & 0x400)
    except Exception:
        return False


def _link_dir(src, dst):
    """在 dst 建指向 src 的目录联接（mklink /J，无需管理员）"""
    if is_junction(dst) and os.path.normcase(os.path.realpath(dst)) == os.path.normcase(os.path.realpath(src)):
        return True
    if os.path.isdir(dst):
        try:
            if os.listdir(dst):
                return False       # 已有真实内容，别动
        except Exception:
            return False
        try:
            os.rmdir(dst)
        except Exception:
            return False
    try:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", dst, src],
                           capture_output=True, text=True, encoding="gbk",
                           errors="replace", timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        return r.returncode == 0 and os.path.isdir(dst)
    except Exception:
        return False


def link_shared(game_dir, src_root, notes=None):
    """把 libraries/assets 共享到 game_dir（已有的真实目录不动）。

    assets 不能整目录替换：官方启动器可能在启动失败时已经建好
    assets/indexes 空骨架，直接 rmdir 会被占用（启动器运行中）。所以逐个子目录链接，
    空骨架才替换，已有真实文件的一律保留。
    """
    ok_all = True
    src_lib = os.path.join(src_root, "libraries")
    dst_lib = os.path.join(game_dir, "libraries")
    if os.path.isdir(src_lib):
        if is_junction(dst_lib):
            pass
        elif os.path.isdir(dst_lib):
            try:
                if not os.listdir(dst_lib):
                    ok = _link_dir(src_lib, dst_lib)
                    if ok and notes is not None:
                        notes.append(f"已共享 libraries：{src_lib}")
            except Exception:
                ok = False
        else:
            ok = _link_dir(src_lib, dst_lib)
            if ok and notes is not None:
                notes.append(f"已共享 libraries：{src_lib}")
            ok_all = ok_all and ok

    src_a = os.path.join(src_root, "assets")
    dst_a = os.path.join(game_dir, "assets")
    if os.path.isdir(src_a):
        try:
            os.makedirs(dst_a, exist_ok=True)
        except Exception:
            return ok_all
        shared = []
        for name in os.listdir(src_a):
            s = os.path.join(src_a, name)
            if not os.path.isdir(s):
                continue
            d = os.path.join(dst_a, name)
            if is_junction(d):
                continue
            if os.path.isdir(d):
                try:
                    if os.listdir(d):
                        continue        # 已有真实内容（可能官启自己下过），保留
                    os.rmdir(d)
                except Exception:
                    continue
            if _link_dir(s, d):
                shared.append(name)
            else:
                ok_all = False
        if shared and notes is not None:
            notes.append(f"已共享 assets/{'、'.join(shared)}：{src_a}")
    return ok_all


# ---------------------------------------------------------------- 采用已有版本
def _safe_id(mc, kind, lver, fallback):
    """给「搬进来的版本」起目录名。

    第五十三轮修的关键：`kind` 有值但 `lver` 空时，**绝不能塌缩成裸原版号**。
    以前就是 `return mc` —— 于是「把一个 loader 版本搬进来」写成了
    `versions/1.20.1/1.20.1.json`，把真正的原版档案整份盖掉（现场：
    `mainClass` 变成 BootstrapLauncher、117 个库里有 forge 坐标），
    之后官方启动器和 PCL2 用 1.20.1 启动全部报
    `Invalid paths argument, contained no existing paths`。
    """
    if kind and lver:
        v = lver
        if mc and v.lower().startswith(mc.lower() + "-"):
            v = v[len(mc) + 1:]          # 1.20.1-47.4.18 -> 47.4.18
        return f"{mc}-{kind}-{v}" if mc else f"{kind}-{v}"
    if kind:
        # 有加载器、但版本号没认出来：拿源 id 拼，拼不出来就只留 `mc-加载器`
        tail = re.sub(r'[\\/:*?"<>|\[\]]+', "-", str(fallback or "")).strip("-")
        if tail and kind in tail.lower():
            return tail                  # 源 id 本来就是「1.20.1-forge-xx」的形态
        if tail and (not mc or os.path.normcase(tail) != os.path.normcase(mc)):
            return f"{mc}-{kind}-{tail}" if mc else f"{kind}-{tail}"
        return f"{mc}-{kind}" if mc else (tail or kind)
    if mc:
        return mc
    return re.sub(r'[\\/:*?"<>|\[\]]+', "-", fallback).strip("-") or "modpack-version"


def _free_vid(vroot, vid):
    """在 versions 下找一个不会被覆盖的目录名（绝不动已有版本的 json）"""
    p = os.path.join(vroot, vid)
    if not os.path.isdir(p):
        return p
    for i in range(2, 100):
        alt = os.path.join(vroot, f"{vid}-{i}")
        if not os.path.isdir(alt):
            return alt
    return p


def _copy_loader_jars(game_dir: str, src_root: str, kind: str, lver: str,
                      mc: str = "") -> int:
    """从别的启动器目录**只搬加载器那几份 jar**过来，返回搬了几份。

    为什么必须单独搬：NeoForge/Forge 的 `-universal.jar` / `-client.jar` 以及
    `client-…-srg.jar` **都不在版本 json 的 libraries 里** —— 它们是安装器的
    processors 顺手写进 `libraries/` 的副产品。所以「只搬版本目录 + 共享
    libraries（目标已有内容就不动）」这一套会把玩家带到一个看起来很完整、
    一开就秒退的版本上（第九十九轮那位玩家的机器就是这样）。
    """
    if not (src_root and kind and lver):
        return 0
    src_libs = os.path.join(src_root, "libraries")
    if not os.path.isdir(src_libs):
        return 0
    dst_libs = os.path.join(game_dir, "libraries")
    if kind == "neoforge":
        rel = ("net", "neoforged", "neoforge", lver)
    else:
        rel = ("net", "minecraftforge", "forge", f"{mc}-{lver}")
    n = 0
    sd, dd = os.path.join(src_libs, *rel), os.path.join(dst_libs, *rel)
    try:
        names = os.listdir(sd)
    except OSError:
        names = []
    for f in names:
        if not f.lower().endswith(".jar"):
            continue
        try:
            os.makedirs(dd, exist_ok=True)
            shutil.copy2(os.path.join(sd, f), os.path.join(dd, f))
            n += 1
        except OSError:
            continue
    # 原版那份 -srg.jar（安装器 processors 的产物，启动时 FML 要用）
    if mc:
        sb = os.path.join(src_libs, "net", "minecraft", "client")
        try:
            subs = os.listdir(sb)
        except OSError:
            subs = []
        for sub in subs:
            if not sub.startswith(str(mc) + "-"):
                continue
            sd2 = os.path.join(sb, sub)
            dd2 = os.path.join(dst_libs, "net", "minecraft", "client", sub)
            try:
                files = [f for f in os.listdir(sd2) if f.endswith("-srg.jar")]
            except OSError:
                continue
            for f in files:
                try:
                    os.makedirs(dd2, exist_ok=True)
                    shutil.copy2(os.path.join(sd2, f), os.path.join(dd2, f))
                    n += 1
                except OSError:
                    continue
    return n


def adopt(game_dir, info, notes=None, progress=None):
    """把 info 指向的版本搬进 game_dir/versions，并共享 libraries/assets。
    返回可直接写进档案的版本 id。"""
    notes = notes if notes is not None else []
    vid = _safe_id(info["mc"], info["loader"], info["loader_ver"], info["id"])
    vroot = os.path.join(game_dir, "versions")
    dst = os.path.join(vroot, vid)
    src = info["dir"]
    if os.path.normcase(os.path.realpath(src)) != os.path.normcase(os.path.realpath(dst)):
        d = _read_json(info["json"]) or {}
        d["id"] = vid
        tgt_json = os.path.join(dst, vid + ".json")
        same = False
        if os.path.isfile(tgt_json):
            # 目标目录已经有同名版本档案：内容一样就复用，不一样就**换名搬**。
            # 覆盖一个能启动的版本比多留一份副本糟糕得多（第五十三轮）。
            same = _read_json(tgt_json) == d
            if not same:
                free = _free_vid(vroot, vid)
                if free != dst:
                    vid = os.path.basename(free)
                    dst = free
                    tgt_json = os.path.join(dst, vid + ".json")
                    d["id"] = vid
        os.makedirs(dst, exist_ok=True)
        if progress and not same:
            progress(f"整理运行版本 {vid}")
        # 复制/生成版本 JSON（id 必须与目录同名，官方启动器才认）
        if not same:
            with open(tgt_json, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)


        # client jar：官方启动器按 <id>.jar 找
        if info["jar"]:
            jdst = os.path.join(dst, vid + ".jar")
            if not os.path.isfile(jdst) or os.path.getsize(jdst) != os.path.getsize(info["jar"]):
                shutil.copy2(info["jar"], jdst)
        # natives 目录（PCL2 解压好的本地库）
        for n in (info["id"] + "-natives", os.path.basename(src) + "-natives"):
            ns = os.path.join(src, n)
            if os.path.isdir(ns):
                nd = os.path.join(dst, vid + "-natives")
                if not os.path.isdir(nd):
                    shutil.copytree(ns, nd)
                break
        # info 不一定带 root（describe_version 不带，scan_versions 才补），
        # 这里必须用 get —— 直接下标会 KeyError，而且是在 json/jar 已经写完
        # 之后才炸，看起来像「导入到一半失败」。
        _src_root = info.get("root") or os.path.dirname(os.path.dirname(src))
        notes.append(f"已复用本机版本：{os.path.basename(src)}（来自 {_src_root}）")
    if info.get("root") and os.path.normcase(os.path.realpath(info["root"])) != \
            os.path.normcase(os.path.realpath(game_dir)):
        link_shared(game_dir, info["root"], notes)
    # 加载器那几份 jar 不会跟着版本目录过来（见 _copy_loader_jars），这里补搬一次。
    if str(info.get("loader") or "") in ("forge", "neoforge"):
        lkind = str(info["loader"])
        lver = str(info.get("loader_ver") or "")
        mc = str(info.get("mc") or "")
        if not loader_ready(game_dir, lkind, lver, mc)[0]:
            got = _copy_loader_jars(game_dir, str(info.get("root") or ""),
                                    lkind, lver, mc)
            ok_l, why_l = loader_ready(game_dir, lkind, lver, mc)
            if ok_l:
                notes.append(f"已补搬加载器文件 {got} 份")
            else:
                notes.append(f"复用来的加载器不完整（{why_l}），需要重装一次")
    return vid


# ---------------------------------------------------------------- 官方源补原版
def _get(url, timeout=25):
    """统一走下载源策略（设置 → 下载镜像源）：官方优先，连不上自动换镜像。"""
    from . import mirror
    return mirror.fetch(url, timeout=timeout)


def fetch_vanilla(game_dir, mc, progress=None):
    """从 Mojang 补原版：version json + client.jar + assetIndex。返回版本 id 或 ''"""
    try:
        man = _get(MANIFEST_URL).json()
        target = None
        for v in man.get("versions", []):
            if v.get("id") == mc:
                target = v
                break
        if not target:
            return ""
        if progress:
            progress(f"下载原版 {mc} 版本信息…")
        d = _get(target["url"]).json()
        vdir = os.path.join(game_dir, "versions", mc)
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, mc + ".json"), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        # client jar
        jar_url = ((d.get("downloads") or {}).get("client") or {}).get("url")
        jdst = os.path.join(vdir, mc + ".jar")
        jar_size = int(((d.get("downloads") or {}).get("client") or {}).get("size") or 0)
        if jar_url and (not os.path.isfile(jdst) or os.path.getsize(jdst) < 1024):
            if progress:
                progress(f"下载原版 {mc} 客户端…")
            from . import dl
            dl.download(jar_url, jdst, expected_size=jar_size)
        # assetIndex（这一步缺失就是 "Unable to prepare assets for download" 的直接原因）
        ai = d.get("assetIndex") or {}
        if ai.get("id") and ai.get("url"):
            idx_dir = os.path.join(game_dir, "assets", "indexes")
            os.makedirs(idx_dir, exist_ok=True)
            ip = os.path.join(idx_dir, str(ai["id"]) + ".json")
            if not os.path.isfile(ip):
                if progress:
                    progress("下载资源索引…")
                with open(ip, "wb") as f:
                    f.write(_get(ai["url"]).content)
            # 第五十九轮：索引只是清单，真正的贴图 / 音效 / 语言文件都在
            # assets/objects 里（几千个内容寻址的小文件），以前一个都不下 ——
            # 于是在没有官方启动器缓存的机器上，装完就是主菜单花屏、全程无声、
            # 语言列表空白。放在这里下（而不是等玩家点启动）是因为此刻他本来
            # 就在等下载；启动时 ensure_files 还会再兜一次。
            try:
                from . import assetsync
                res = assetsync.sync(game_dir, str(ai["id"]), progress=progress)
                if res.get("failed") and progress:
                    progress("部分资源没下全，启动时会再补一次")
            except Exception as e:                           # noqa: BLE001
                if progress:
                    progress(f"资源文件下载跳过：{e}")
        return mc
    except Exception:
        return ""


def fetch_fabric_profile(game_dir, mc, lver, progress=None):
    """Fabric：先补原版，再写 meta 提供的 loader 版本 JSON（继承原版）"""
    if not os.path.isfile(os.path.join(game_dir, "versions", mc, mc + ".json")):
        if progress:
            progress(f"补全原版 {mc}…")
        if not fetch_vanilla(game_dir, mc, progress):
            return ""
    try:
        if progress:
            progress("下载 Fabric 加载器配置…")
        d = _get(f"https://meta.fabricmc.net/v2/versions/loader/{mc}/{lver}/profile/json").json()
        vid = str(d.get("id") or f"fabric-loader-{lver}-{mc}")
        vdir = os.path.join(game_dir, "versions", vid)
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, vid + ".json"), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return vid
    except Exception:
        return ""


# ---------------------------------------------------------------- 整合包环境推断
def mc_plausible(s):
    """这个字符串像不像一个真实的 MC 版本号。

    存在的意义：`neoforge-21.1.172.jar` / `forge-47.4.0.jar` 这种**加载器**文件名里
    也带数字，`_MC_RE` 会从 "21.1.172" 里抠出 "1.1.172"，把 MC 版本记成 1.1.172。
    判据很简单：第三段超过 30 就不是 MC（MC 只有 1.7.10 / 1.20.1 / 1.21.11 这种）。
    """
    s = str(s or "")
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?$", s)
    if m:
        return m.group(3) is None or int(m.group(3)) <= 30
    return bool(re.match(r"^\d{2}w\d{2}[a-z]$", s))       # 25w42a 这类快照


def mc_from_id(vid):
    """官启原版目录名本身就是 MC 版本号时，直接拿来用。

    老命名 `1.21.1` 好认，`_MC_RE` 就够；但 2026 起改成按年份命名（`26.3`、
    `26.4-snapshot-5`），旧正则只认 `1.x`，这些版本会被记成「MC 未知」。
    这里要求**整串开头就是版本号**，避免把 `neoforge-21.1.264` 里的 21.1 当成 MC。
    """
    m = re.match(r"^(\d{1,2}\.\d{1,3}(?:\.\d{1,3})?)(?=$|[-_])", str(vid or ""))
    if not m:
        return ""
    v = m.group(1)
    major = int(v.split(".")[0])
    # 1.x 是旧命名；24~39 是年份命名（26.x）。Forge 47.x / NeoForge 21.x 都不在这个区间
    return v if (major == 1 or 24 <= major <= 39) else ""


def infer_instance_env(instance_dir):
    """从实例里的 mods 推断需要的 MC 版本与加载器（用于修复历史安装）"""
    mc_votes, kind_votes = {}, {}
    mods = os.path.join(instance_dir, "mods")
    names = []
    if os.path.isdir(mods):
        try:
            names = os.listdir(mods)
        except Exception:
            names = []
    for n in names:
        low = n.lower()
        # 计票而不是先到先得：整合包里常混有 "Fabric-API 兼容" 之类的文件名
        if "neoforge" in low or "neo-forge" in low:
            kind_votes["neoforge"] = kind_votes.get("neoforge", 0) + 1
        if "forge" in low:
            kind_votes["forge"] = kind_votes.get("forge", 0) + 1
        if "fabric" in low and "quilt" not in low:
            kind_votes["fabric"] = kind_votes.get("fabric", 0) + 1
        # 不是「跳过加载器 jar」而是「按合理性过滤」：真实整合包里连普通模组
        # 文件名都带 neoforge/fabric（jei-1.21.1-neoforge-…），按关键词跳会把票全跳光。
        for m in _MC_RE.findall(n):
            if mc_plausible(m):
                mc_votes[m] = mc_votes.get(m, 0) + 1
    order = {"neoforge": 3, "forge": 2, "fabric": 1}
    kind = max(kind_votes, key=lambda k: (kind_votes[k], order.get(k, 0))) if kind_votes else ""
    mc = max(mc_votes, key=mc_votes.get) if mc_votes else ""
    if not mc:
        for f in ("instance.cfg", "mmc-pack.json"):
            p = os.path.join(instance_dir, f)
            if os.path.isfile(p):
                try:
                    txt = open(p, encoding="utf-8", errors="replace").read()
                    for m in _MC_RE.finditer(txt):
                        if mc_plausible(m.group(0)):
                            mc = m.group(0)
                            break
                    if mc:
                        break
                except Exception:
                    pass
    return mc, kind


# ---------------------------------------------------------------- 加载器装全了吗
def _jar_ok(p: str) -> bool:
    """这真的是一份能用的 jar 吗（存在 + 有内容 + 像 zip）。

    为什么连文件头都查：下载中断 / 代理返回一段 HTML 时，文件照样「存在」。
    这种 jar 交给 FML，它只会说一句「not a valid mod file」然后当没装。
    """
    try:
        if not os.path.isfile(p) or os.path.getsize(p) < 1024:
            return False
        with open(p, "rb") as f:
            return f.read(2) == b"PK"
    except OSError:
        return False


def _legacy_mc(mc: str) -> bool:
    """1.12.2 及更早：Forge 只有一个 `<mc>-<ver>.jar`，没有 universal/client 之分"""
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", str(mc or ""))
    if not m:
        return False
    return (int(m.group(1)), int(m.group(2))) < (1, 13)


def loader_ready(game_dir: str, kind: str, lver: str,
                 mc: str = "") -> tuple[bool, str]:
    """Forge / NeoForge **真的装全了吗**。返回 (ok, 缺什么)。

    第九十九轮：这是「整合包装完打开就秒退」的判据。
    安装器（官方 installer）除了写 `versions/<id>/<id>.json`，还会在
    `libraries/` 里生成三份启动时离不开的东西：

        libraries/net/neoforged/neoforge/<lver>/neoforge-<lver>-universal.jar
        libraries/net/neoforged/neoforge/<lver>/neoforge-<lver>-client.jar
        libraries/net/minecraft/client/<mc>-<日期>/client-…-srg.jar

    安装器中途失败（断网 / 代理 / 玩家点了取消）时，**版本 json 留在原地、这三份
    没有**。FML 启动时靠这几份认出 `minecraft` 和 `neoforge` 两个 mod；认不出来
    就报 `Actual version: '[MISSING]'`，然后在开窗口那一瞬间抛
    `NoSuchElementException: No value present`（DisplayWindow.updateModuleReads），
    退出码 1、存活 10 秒 —— 玩家拿到的崩溃日志长这样，但根因和模组一点关系都没有。

    以前判断「装过没有」只看版本 json 在不在，所以这种半成品会被当成「已装好」
    一路沿用下去（见 `ensure_runtime` 第 1 步 / `loader.install_loader`）。
    """
    kind = str(kind or "").lower()
    lver = str(lver or "").strip()
    if kind not in ("forge", "neoforge"):
        return True, ""                       # fabric / quilt / 原版没有这一层
    gd = str(game_dir or "")
    if not gd:
        return True, ""
    libs = os.path.join(gd, "libraries")
    if kind == "neoforge":
        base = os.path.join(libs, "net", "neoforged", "neoforge", lver)
        pre = f"neoforge-{lver}"
    else:
        if not lver:
            return False, "没记下 Forge 版本号"
        # describe_version 有时给全名 `1.20.1-47.4.18`，有时只给 `47.4.18`
        full = lver if lver.startswith(str(mc) + "-") else f"{mc}-{lver}"
        base = os.path.join(libs, "net", "minecraftforge", "forge", full)
        pre = f"forge-{full}"
    if not os.path.isdir(base):
        return False, f"缺 libraries 里的加载器目录（{os.path.basename(base)}）"
    if kind == "forge" and _legacy_mc(mc):
        # 老版本只有一个 jar，名字也不带 universal/client
        jars = [n for n in os.listdir(base)
                if n.lower().endswith(".jar") and _jar_ok(os.path.join(base, n))]
        if jars:
            return True, ""
        return False, f"缺 {pre}.jar"
    miss = [n for n in (pre + "-universal.jar", pre + "-client.jar")
            if not _jar_ok(os.path.join(base, n))]
    if miss:
        return False, "缺 " + "、".join(miss)
    if mc and not _has_srg(libs, mc):
        return False, f"缺原版 {mc} 的 -srg.jar（安装器没跑完）"
    return True, ""


def _has_srg(libs: str, mc: str) -> bool:
    """`libraries/net/minecraft/client/<mc>-<日期>/client-…-srg.jar` 在不在"""
    base = os.path.join(libs, "net", "minecraft", "client")
    try:
        names = os.listdir(base)
    except OSError:
        return False
    for n in names:
        if not n.startswith(str(mc) + "-"):
            continue
        d = os.path.join(base, n)
        try:
            for f in os.listdir(d):
                if f.endswith("-srg.jar") and _jar_ok(os.path.join(d, f)):
                    return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------- 总入口
def ensure_runtime(game_dir, mc, kind="", lver="", progress=None, roots=None,
                   cancel_flag=None, verify_loader=False):
    """保证 game_dir 里有可启动的版本。返回 (version_id 或 '', notes[])。

    第九十八轮：`cancel_flag` 一路传到 `install_loader` —— Forge/NeoForge 的官方
    安装器要跑 1~3 分钟，以前这一段拿到的是 `task_id=None`，等于完全不可取消，
    玩家点完取消界面就一直停在「正在取消，请稍候…」。

    第一百零八轮 `verify_loader`：整合包导入传 True。此时第 1/2 步复用到的
    Forge/NeoForge 版本必须**真的装全**（`loader_ready`）才能直接返回；
    版本 json 在、jar 不全的半成品继续往下走第 3 步官方安装器重装。
    以前「沿用已有版本」一律直接 return，玩家上次断网留下的半成品会让
    之后每一次导入都跳过安装器 —— 这就是「始终缺模组加载器」。
    默认 False：第九十九轮特意不在这强卡，旧导入回归的 fixture 只有版本 json。
    """
    def _cancelled():
        return bool(cancel_flag and cancel_flag[0])

    def _reused_loader_ok():
        if not verify_loader or kind not in ("forge", "neoforge") or not lver:
            return True
        try:
            return bool(loader_ready(game_dir, kind, lver, mc)[0])
        except Exception:                                     # noqa: BLE001
            # 复核本身出错时不改旧行为：乱触发重装比沿用更糟。
            return True

    notes = []
    # 0) 先把「被加载器档案盖掉的原版 json」修回来（第五十三轮）
    #    上一版的 adopt() 会把 `versions/1.20.1` 写成一份 forge 档案，之后
    #    `_score` 会把整合包绑到这个坏版本上，装什么都不对。
    try:
        notes.extend(repair_polluted_vanilla(game_dir, progress))
    except Exception:                                     # noqa: BLE001
        pass
    # 1) 游戏目录里已有匹配版本
    #    这里**只挑版本 id**，不在这儿卡「加载器装全了没」：
    #    导入流程要靠它把档案写完，半装的加载器由 install_loader / adopt /
    #    开局前的 gamelaunch._fix_loader 补 —— 否则一次断网会让整包导入失败。
    for v in scan_versions([game_dir]):
        if _score(v, mc, kind, lver) > 0:
            if _reused_loader_ok():
                return v["id"], notes + [f"沿用已有版本 {v['id']}"]
            notes.append(f"沿用版本 {v['id']} 的加载器没装全，改走官方安装器重装")
            break
    # 2) 本机其它启动器复用
    if mc:
        if progress:
            progress("查找本机可复用的游戏版本…")
        if kind in ("forge", "neoforge") and lver:
            # 加载器版本必须完全一致：不同小版本会在 ModLauncher 阶段直接崩
            info = find_local_exact(mc, kind, lver, roots)
        else:
            info = find_local(mc, kind, lver, roots)
            if not info and kind:
                info = find_local(mc, kind, "", roots)
        if info:
            try:
                av = adopt(game_dir, info, notes, progress)
                if _reused_loader_ok():
                    return av, notes
                notes.append("复用来的加载器没装全，改走官方安装器重装")
            except Exception as e:
                notes.append(f"复用本机版本失败：{e}")
        # 3) 官方源补：原版直接下；Fabric 写加载器配置；Forge/NeoForge 跑官方安装器
        if not kind:
            vid = fetch_vanilla(game_dir, mc, progress)
            if vid:
                notes.append(f"已从官方源补全原版 {mc}")
                return vid, notes
        elif kind == "fabric" and lver:
            vid = fetch_fabric_profile(game_dir, mc, lver, progress)
            if vid:
                notes.append(f"已从官方源补全 Fabric {lver}（{mc}）")
                return vid, notes
        elif kind in ("forge", "neoforge") and lver:
            # 第五十三轮：以前这里**什么都不做** —— Forge 整合包只能靠「本机碰巧装过」
            # 才能启动，否则绑到一个不相干的版本上，游戏窗口一闪都不出。
            # Forge/NeoForge 必须跑官方安装器（它的 processors 才会生成
            # libraries/net/minecraft/client/*-srg.jar、forge-*-client.jar 这些
            # 启动时离不开的文件；光下版本 json 里列的库是不够的）。
            cn = LOADER_CN.get(kind, kind)
            if _cancelled():
                raise RuntimeError("已取消")
            if progress:
                progress(f"正在安装 {cn} {lver}（首次需要 1~3 分钟）…")
            try:
                from . import loader as _loader
                # 第一百零九轮：把 progress 一路传到官方安装器（下载安装器时
                # 报 MB、运行时报已跑秒数）。以前这里不传，导入详情只能看到一个
                # 焊在 93% 的进度条，1~3 分钟完全不知道卡在哪。
                ok, vid = _loader.install_loader(mc, kind, lver, None,
                                                 game_dir=game_dir,
                                                 cancel_flag=cancel_flag,
                                                 progress=progress)
            except Exception as e:                        # noqa: BLE001
                if "已取消" in str(e):
                    raise                                 # 取消不是失败，别吞掉
                ok, vid = False, ""
                notes.append(f"{cn} 安装出错：{e}")
            if ok and vid:
                notes.append(f"已安装 {cn} {lver}（Minecraft {mc}）")
                return vid, notes
            notes.append(f"{cn} {lver} 安装失败：{vid}")
    if kind and not lver:
        # 第一百零八轮：说清是「没记下版本号」，别让玩家以为是网络问题，
        # 反复重装多少遍都过不去（拿空版本号拼的 installer URL 必然 404）。
        notes.append(
            f"{LOADER_CN.get(kind, kind)} 的版本号没记下（整合包清单里没写），"
            "不能凭空猜一个去下载官方安装器")
    if kind:
        extra = ""
        if lver:
            extra = f" {lver}"
        notes.append(
            f"缺少 {LOADER_CN.get(kind, kind)}{extra}（{mc}）的运行版本。"
            f"请先在官方启动器里安装 Minecraft {mc} + {LOADER_CN.get(kind, kind)}，"
            f"或用 PCL2 装好该加载器后再点「修复档案」。")
    return "", notes


# ---------------------------------------------------------------- 原版 json 修复
_MC_ONLY = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
_LOADER_LIB_PREFIX = ("net.minecraftforge:", "net.neoforged", "net.fabricmc",
                      "org.quiltmc")


def repair_polluted_vanilla(game_dir, progress=None):
    """`versions/1.20.1/1.20.1.json` 被加载器档案盖掉时，换回 Mojang 的纯原版 json。

    判据（三个都满足才算坏）：目录名是纯原版号 + json 里没有 inheritsFrom +
    mainClass 不是原版的 `net.minecraft.client.main.Main`（或 libraries 里带加载器坐标）。
    **只改 json，客户端 jar / 存档 / 模组一个字节都不动**，原档先留一份
    `<id>.json.polluted.bak`。
    """
    notes = []
    vroot = os.path.join(game_dir, "versions")
    if not os.path.isdir(vroot):
        return notes
    bad = []
    for name in os.listdir(vroot):
        if not _MC_ONLY.match(name):
            continue
        jp = os.path.join(vroot, name, name + ".json")
        d = _read_json(jp)
        if not d or d.get("inheritsFrom"):
            continue
        main = str(d.get("mainClass") or "")
        libs = [str(x.get("name") or "") for x in (d.get("libraries") or [])]
        loader_lib = any(n.startswith(_LOADER_LIB_PREFIX) for n in libs)
        if "minecraft.client.main.Main" in main and not loader_lib:
            continue
        if not (loader_lib or main):
            continue
        bad.append((name, jp))
    if not bad:
        return notes
    if progress:
        progress("修复被加载器档案覆盖的原版版本…")
    man = None
    try:
        man = _get(MANIFEST_URL, timeout=25).json()
    except Exception:                                     # noqa: BLE001
        man = None
    if not man:
        for name, _jp in bad:
            notes.append(f"versions/{name} 的原版档案已被加载器覆盖，"
                         f"但连不上 Mojang，暂时没法修（进不去就重装该原版）")
        return notes
    urls = {str(v.get("id")): v.get("url") for v in man.get("versions") or []}
    for name, jp in bad:
        u = urls.get(name)
        if not u:
            continue
        try:
            d = _get(u, timeout=25).json()
            if not d:
                continue
            bak = jp + ".polluted.bak"
            if not os.path.isfile(bak):
                shutil.copy2(jp, bak)
            with open(jp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            notes.append(f"已把 versions/{name} 的原版档案修回来"
                         f"（坏档备份为 {name}.json.polluted.bak）")
        except Exception:                                 # noqa: BLE001
            notes.append(f"versions/{name} 的原版档案修复失败（网络？）")
    return notes


def pick_any_version(game_dir, need_loader="", need_lver=""):
    """档案兜底：挑一个已装好的版本 id（同目录优先带 jar 的）。

    第九十五轮加了 `need_loader` / `need_lver`，因为「随便挑一个装好的版本」
    正是整合包导入最坑的那种失败：要 Forge 47.4.10 的包，加载器没装上，
    这里挑到纯原版 `1.20.1`，档案照样写成功、官启照样能点开始游戏、游戏
    **也真的能进** —— 只是 329 个模组一个都不加载。玩家看到的是
    「导入成功了但整合包没生效」，比直接报错难查十倍。

    给了 `need_loader` 就只认装了这个加载器的版本；要的加载器一个都没有时
    返回空串，让上层明确报「加载器没装好」，而不是悄悄退回原版。
    `need_lver` 再收紧到加载器小版本（Forge/NeoForge 换了小版本 ModLauncher
    阶段会直接崩，宁可不绑）。
    """
    picks = []
    for v in scan_versions([game_dir]):
        if need_loader and v["loader"] != need_loader:
            continue
        if need_lver and v["loader"] and not _same_loader_ver(
                v["loader_ver"], need_lver, v["mc"]):
            continue
        picks.append(v)
    for v in picks:
        if v["jar"] or v["loader"]:
            return v["id"]
    return picks[0]["id"] if picks else ""
