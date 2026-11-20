# -*- coding: utf-8 -*-
"""模组加载器下载（纯加载器，不含任何模组）。

支持 Fabric / Quilt / Forge / NeoForge：
- Fabric / Quilt：先补原版（json+client.jar+assetIndex），再写官方 meta 提供的
  加载器版本 JSON（继承原版）；
- Forge / NeoForge：官方 installer 需要 Java 才能真正装好（要跑它的 processors），
  这里下载 installer 后用本机 Java 执行 `--installClient`，把它装进游戏目录。

下载源策略（大陆网络必备）：
`maven.neoforged.net` 在国内经常被解析到错误 IP / TCP 直接被掐，官方 installer 内部
又是按 install_profile.json 里写死的 URL 去抓库的（实测 `--mirror` 只影响它自己的
mirror_list，不改库地址）。所以这里做两层兜底：
1) 取版本列表 / 下载 installer 时，官方失败自动降级到 BMCLAPI 镜像；
2) 需要走镜像时，把 installer 包里的 JSON 地址整体改成镜像地址，再跑副本，
   这样它内部下载的所有库也走镜像。
"""
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile

import requests

from . import runtime
from .config import app_data_dir
from .tasks import TASKS

UA = "MC-GuanQi-Fuzhu-Gongju/1.0.0"
KINDS = [("fabric", "Fabric"), ("quilt", "Quilt"), ("forge", "Forge"),
         ("neoforge", "NeoForge")]
KIND_CN = dict(KINDS)

# ---- 下载源 -----------------------------------------------------------------
# 镜像/官方策略统一由 app/core/mirror.py 管理（设置 → 下载镜像源）。
from . import mirror as _mirror                     # noqa: E402

# BMCLAPI 是国内完整可用的 Maven 代理：同时覆盖 maven.neoforged.net、
# maven.minecraftforge.net 与 libraries.minecraft.net 的路径。
BMCLAPI = _mirror.BMCLAPI
MIRROR_MAVEN = _mirror.BMCLAPI_MAVEN

FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
NEO_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
NEO_API = ("https://maven.neoforged.net/api/maven/versions/releases/"
           "net/neoforged/neoforge")
QUILT_META = "https://meta.quiltmc.org/v3/versions"
FABRIC_META = "https://meta.fabricmc.net/v2/versions"

# 官方前缀 -> 镜像前缀（旧接口，保留给外部调用；实际映射见 mirror.SOURCES）
MIRROR_MAP = tuple(
    (pre, s["mirror"]) for s in _mirror.SOURCES for pre in s["official"]
)

_NET_ERRORS = _mirror._NET_ERRORS

_dead = _mirror._DEAD                               # 共享「连不上」域名集合
_last = _mirror._LAST


def mirror_mode():
    """auto（官方优先、失败换镜像）/ always（直接镜像）/ off（只用官方）"""
    m = _mirror.default_mode()
    return {"official": "off", "always": "always"}.get(m, "auto")


def set_mirror_mode(mode):
    m = {"off": "official", "official": "official",
         "always": "always"}.get(str(mode or ""), "auto")
    _mirror.set_default_mode(m)
    _neo_cache.clear()
    _loader_cache.clear()
    _loader_error.clear()
    return mirror_mode()


def mirror_of(url):
    """官方地址 -> 镜像地址（没有对应镜像返回 ''）"""
    return _mirror.mirror_of(url)


def last_used_mirror():
    return bool(_last.get("mirror"))


def dead_hosts():
    return sorted(_dead)


def _get(url, timeout=20, stream=False):
    return requests.get(url, headers={"User-Agent": UA}, timeout=timeout, stream=stream)


def fetch(url, timeout=20, stream=False):
    """带镜像降级的 GET。官方优先；本会话内官方失败过就直接走镜像。

    HTTP 错误（4xx/5xx）只降级不记“死”，因为镜像也可能返回同样的状态码。
    各域名的具体策略（官方/自动/镜像）在「设置 → 下载镜像源」里逐项可调；
    这里的 mirror_mode() 只作为「默认策略」参与，逐项设置优先。
    """
    m = {"off": "official", "always": "always"}.get(mirror_mode(), "auto")
    return _mirror.fetch(url, timeout=timeout, stream=stream,
                         default=m, getter=_get)


def friendly_error(e, kind=""):
    """把 requests 的原始异常翻译成一句用户能看懂的话"""
    cn = KIND_CN.get(kind, kind)
    host = ""
    m = re.search(r"host='([^']+)'", str(e))
    if m:
        host = m.group(1)
    if isinstance(e, requests.exceptions.Timeout):
        what = f"连接 {host or '官方源'} 超时"
    elif isinstance(e, (requests.exceptions.ConnectionError,
                        requests.exceptions.SSLError,
                        requests.exceptions.ProxyError)):
        what = f"连不上 {host or '官方源'}"
    elif isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
        what = f"{host or '源'} 返回 HTTP {e.response.status_code}"
    else:
        what = f"{type(e).__name__}: {str(e)[:60]}"
    return f"{cn} 官方仓库与国内镜像都不可用（{what}）"


# ---------------------------------------------------------------- 版本列表
_mc_cache = []
_mc_catalog = []
_neo_cache = []
_loader_cache = {}
_loader_error = {}

# 版本分类（第七十二轮）：正式 / 快照 / 远古 / 愚人
CATS = (("release", "正式"), ("snapshot", "快照"),
        ("ancient", "远古"), ("fun", "愚人"))
CAT_CN = dict(CATS)


def loader_error(kind, mc):
    """上一次获取版本列表的错误（网络不可达等），供界面提示用"""
    return _loader_error.get((kind, mc), "")


def clear_cache():
    _mc_catalog.clear()
    _mc_cache.clear()
    _neo_cache.clear()
    _loader_cache.clear()
    _loader_error.clear()
    # Java 侧的缓存也一起清：刚装完 Java 要能立刻看到
    _BEST_JAVA.clear()
    _CAND_CACHE.update({"t": 0.0, "list": []})
    _MAJOR_CACHE.clear()
    _DEEP_CACHE.update({"list": [], "ready": False, "busy": False})


# 愚人节版本的 id 不按 type 区分（Mojang 清单里它们全是 snapshot），只能点名。
# 名单按年份列全，漏一个就会跑到「快照」里去。
_APRIL_IDS = {
    "2.0", "15w14a", "1.rv-pre1", "3d shareware v1.34", "20w14infinite",
    "22w13oneblockatatime", "23w13a_or_b", "24w14potato", "25w14craftmine",
}
# 兜底：名字里带这些词的几乎都是愚人节（infinite/oneblock/potato/craftmine…）
_APRIL_RE = re.compile(r"or_b|oneblock|potato|craftmine|infinite|shareware"
                       r"|rv-pre|^15w14a$|^2\.0$", re.I)


def cat_of(vid: str, vtype: str = "") -> str:
    """一个版本属于哪一类：release / snapshot / ancient / fun"""
    v = str(vid or "")
    t = str(vtype or "").lower()
    if t in ("old_beta", "old_alpha"):
        return "ancient"
    if v.lower() in _APRIL_IDS or _APRIL_RE.search(v):
        return "fun"
    if t == "snapshot":
        return "snapshot"
    if t == "release":
        return "release"
    # 清单里没写 type 时靠名字猜：带 pre/rc/周号 的一律算快照
    return "snapshot" if re.search(r"(?:^|[-_ ])(?:pre|rc)\d*|^\d{2}w\d{2}[a-z]$",
                                   v, re.I) else "release"


def mc_catalog():
    """全部版本（含快照 / 远古 / 愚人），新 -> 旧。

    返回 [{"id": "1.21.1", "cat": "release", "type": "release"}, ...]。
    第七十二轮：以前只取 release 且截前 60 个，玩家想装快照 / 老版本根本没入口。
    """
    global _mc_catalog, _mc_cache
    if _mc_catalog:
        return _mc_catalog
    try:
        d = _get(runtime.MANIFEST_URL, timeout=15).json()
    except Exception:                                    # noqa: BLE001
        return []
    out = []
    for v in d.get("versions", []):
        vid = str(v.get("id") or "").strip()
        if not vid:
            continue
        t = str(v.get("type") or "").lower()
        out.append({"id": vid, "type": t, "cat": cat_of(vid, t)})
    if out:
        _mc_catalog = out
        _mc_cache = [x["id"] for x in out if x["cat"] == "release"]
    return _mc_catalog


def catalog_by_cat(cat):
    """某一类的版本 id 列表（新 -> 旧）"""
    cat = str(cat or "release")
    return [x["id"] for x in mc_catalog() if x["cat"] == cat]


def mc_versions(limit=0):
    """Mojang 正式版列表（新 -> 旧）；limit=0 表示不截断"""
    global _mc_cache
    if not _mc_cache:
        mc_catalog()
    if not _mc_cache:
        try:
            d = _get(runtime.MANIFEST_URL, timeout=12).json()
            _mc_cache = [v["id"] for v in d.get("versions", [])
                         if v.get("type") == "release"]
        except Exception:                                # noqa: BLE001
            _mc_cache = []
    return _mc_cache[:limit] if limit else list(_mc_cache)


def _neo_all_versions():
    """NeoForge 全部版本号。官方 API -> 官方 metadata -> 镜像 metadata。"""
    global _neo_cache
    if _neo_cache:
        return _neo_cache
    err = None
    try:
        # 官方 API 只给 5 秒：被墙时这段就是纯等待，别让用户干等
        vs = _get(NEO_API, timeout=5).json().get("versions") or []
        vs = [str(v) for v in vs if v]
        if vs:
            _last.update(host="maven.neoforged.net", mirror=False)
            _neo_cache = vs
            return vs
    except Exception as e:                            # noqa: BLE001
        err = e
        if isinstance(e, _NET_ERRORS):
            _dead.add("maven.neoforged.net")
    try:
        r = fetch(NEO_MAVEN + "/maven-metadata.xml", timeout=10)
        vs = re.findall(r"<version>([^<]+)</version>", r.text)
        vs = [v.strip() for v in vs if v.strip()]
        if vs:
            _neo_cache = vs
            return vs
    except Exception as e:                            # noqa: BLE001
        err = err or e
    raise err if err is not None else RuntimeError("NeoForge 版本列表为空")


def suggest_mc(kind, mc_list, current=""):
    """当前 MC 版本没有该加载器的包时，返回**离 current 最近**的可用 MC 版本。"""
    if kind != "neoforge" or not mc_list:
        return ""
    try:
        vs = _neo_all_versions()
    except Exception:                                 # noqa: BLE001
        return ""
    have = set()
    for v in vs:
        pre = _neo_prefix_from_version(v)
        if pre:
            have.add(pre)
    cur = _ver_tuple(current) if current else None
    best, best_d = "", 0
    for mc in mc_list:                 # mc_list 是 新 -> 旧
        if _neo_prefix(mc) not in have:
            continue
        d = 0 if cur is None else _ver_dist(_ver_tuple(mc), cur)
        if not best or d < best_d:     # 距离相同时保留更靠前（更新）的那个
            best, best_d = mc, d
    return best


def _ver_tuple(mc):
    out = []
    for part in str(mc).split("."):
        m = re.match(r"\d+", part)
        out.append(int(m.group(0)) if m else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def _ver_dist(a, b):
    return (abs(a[0] - b[0]) * 10000 + abs(a[1] - b[1]) * 100 + abs(a[2] - b[2]))


def _neo_prefix_from_version(v):
    """21.1.264 -> 21.1.；20.4.237 -> 20.4."""
    parts = str(v).split(".")
    if len(parts) < 2:
        return ""
    return f"{parts[0]}.{parts[1]}."


def _neo_prefix(mc):
    """1.21.1 -> 21.1.（NeoForge 的版本号规则）；非 1.x 的新版本号原样用"""
    parts = (mc or "").split(".")
    if len(parts) < 2:
        return ""
    if parts[0] != "1":                       # 26.1 这类新版本号
        return mc + "."
    return f"{parts[1]}.{parts[2] if len(parts) > 2 else '0'}."


def loader_versions(kind, mc):
    """某加载器在指定 MC 版本下可用的版本号（新 -> 旧）"""
    key = (kind, mc)
    if key in _loader_cache:
        return _loader_cache[key]
    out = []
    newest_first = True          # fabric/quilt 的 meta 本身就是新->旧
    try:
        if kind in ("fabric", "quilt"):
            base = FABRIC_META if kind == "fabric" else QUILT_META
            d = fetch(f"{base}/loader/{mc}", timeout=8).json()
            out = [str(x.get("loader", {}).get("version") or x.get("version") or "")
                   for x in d] if isinstance(d, list) else []
        elif kind == "forge":
            newest_first = False     # maven-metadata 是旧->新
            r = fetch(f"{FORGE_MAVEN}/maven-metadata.xml", timeout=10)
            for v in re.findall(r"<version>([^<]+)</version>", r.text):
                if v.startswith(mc + "-"):
                    out.append(v[len(mc) + 1:])
        elif kind == "neoforge":
            newest_first = False
            prefix = _neo_prefix(mc)
            for v in _neo_all_versions():
                if prefix and v.startswith(prefix):
                    out.append(v)
    except Exception as e:                                # noqa: BLE001
        out = out or []
        if not out:
            _loader_error[key] = friendly_error(e, kind)
    out = [x for x in out if x]
    if out and not newest_first:
        out.reverse()          # 统一成新在前
    _loader_cache[key] = out
    return out


# ---------------------------------------------------------------- Java
# ---- Java 探测 ---------------------------------------------------------------
# 第五十三轮：这里原来是「每个候选跑一次 java -version」。本机装了 JDK 8/17/21/24
# 四套 + 官启自带 runtime 的话，一次 find_java 就是 5~8 个进程 = **3 秒**，
# 而且「Java 下载」和「设置」两边都会在点开的瞬间调它 → 界面明显卡一下。
#
# 现在两层提速：
#   1) 候选列表（glob，含递归扫官启 runtime）缓存 60 秒 —— 单这一步就 240ms；
#   2) 主版本优先读 JDK 自带的 `release` 纯文本（毫秒级），读不到才退回 java -version。
_BEST_JAVA = {}                     # min_major -> (exe, 时间戳)
_BEST_TTL = 30.0
_CAND_CACHE = {"t": 0.0, "list": []}
_CAND_TTL = 60.0
_MAJOR_CACHE = {}                   # normcase(exe) -> (major, mtime)
_DEEP_CACHE = {"list": [], "ready": False, "busy": False}


def _java_candidates():
    """本机所有候选 java.exe（只做便宜的 glob）。

    最贵的两处是「递归扫官启自带 runtime」（实测 240ms，还是冷盘）：放到后台线程
    预热，这里先用现有结果 —— 真的一个都没找到时 `find_java` 会等它一下。
    """
    now = time.time()
    if _CAND_CACHE["list"] and now - _CAND_CACHE["t"] < _CAND_TTL:
        return list(_CAND_CACHE["list"]) + list(_DEEP_CACHE["list"])
    cands = []
    jh = os.environ.get("JAVA_HOME")
    if jh:
        cands.append(os.path.join(jh, "bin", "java.exe"))
    # 第一百零八轮：以前只扫 64 位 Program Files。本机（以及不少玩家的机器）
    # JDK 全装在 `C:\Program Files (x86)\Java\` → find_java() 一个都找不到 →
    # install_loader() 直接返回「没有找到可用的 Java」，Forge/NeoForge 安装器
    # 根本不跑，整合包导入后自然始终缺加载器。javamanager 的 _JAVA_GLOBS 早就
    # 认这个目录，这里补成同一套口径。
    pf32 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    for pat in (r"C:\Program Files\Java\jdk-*\bin\java.exe",
                r"C:\Program Files\Java\jre*\bin\java.exe",
                r"C:\Program Files\Eclipse Adoptium\*\bin\java.exe",
                r"C:\Program Files\Microsoft\jdk-*\bin\java.exe",
                r"C:\Program Files\Zulu\*\bin\java.exe",
                r"C:\Users\*\AppData\Local\Programs\Eclipse Adoptium\*\bin\java.exe",
                os.path.join(pf32, "Java", "jdk-*", "bin", "java.exe"),
                os.path.join(pf32, "Java", "jre*", "bin", "java.exe"),
                os.path.join(pf32, "Eclipse Adoptium", "*", "bin", "java.exe"),
                os.path.join(pf32, "Microsoft", "jdk-*", "bin", "java.exe"),
                os.path.join(pf32, "Zulu", "*", "bin", "java.exe")):
        cands.extend(glob.glob(pat))
    _CAND_CACHE.update({"t": now, "list": cands})
    _warm_deep_java()
    return list(cands) + list(_DEEP_CACHE["list"])


def _warm_deep_java():
    """后台扫「官方启动器自带 runtime」那两条递归通配（240ms，不能占主线程）"""
    if _DEEP_CACHE["ready"] or _DEEP_CACHE["busy"]:
        return
    _DEEP_CACHE["busy"] = True

    def work():
        out = []
        try:
            for pat in (
                os.path.expandvars(
                    r"%LOCALAPPDATA%\Packages\Microsoft.4297127D64EC6*\LocalCache"
                    r"\Local\runtime\**\bin\java.exe"),
                os.path.expandvars(r"%APPDATA%\.minecraft\runtime\**\bin\java.exe"),
            ):
                out.extend(glob.glob(pat, recursive=True))
        except Exception:                                 # noqa: BLE001
            out = []
        _DEEP_CACHE.update({"list": out, "ready": True, "busy": False})

    threading.Thread(target=work, daemon=True, name="java-deep-scan").start()


def _wait_deep_java(seconds=2.5):
    """一个都没找到时才等后台扫描收尾（否则官启自带的 Java 看不见）"""
    end = time.time() + seconds
    while _DEEP_CACHE["busy"] and time.time() < end:
        time.sleep(0.02)


def _major_from_release(java_exe):
    """读 JDK 的 `release` 文件拿主版本：`JAVA_VERSION="17.0.12"`。

    JDK 9+ 必有；很多 JRE 8 也有。读不到返回 0。
    """
    home = os.path.dirname(os.path.dirname(str(java_exe or "")))
    if not home:
        return 0
    try:
        with open(os.path.join(home, "release"), "r",
                  encoding="utf-8", errors="replace") as f:
            for _ in range(40):
                line = f.readline()
                if not line:
                    break
                if line.startswith("JAVA_VERSION="):
                    v = line.split("=", 1)[1].strip().strip('"')
                    m = re.match(r"(\d+)(?:\.(\d+))?", v)
                    if not m:
                        return 0
                    major = int(m.group(1))
                    if major == 1 and m.group(2):
                        major = int(m.group(2))
                    return major
    except OSError:
        pass
    return 0


def _version_from_release(java_exe):
    """同上的完整版本号字符串（给界面显示用，不启进程）"""
    home = os.path.dirname(os.path.dirname(str(java_exe or "")))
    if not home:
        return ""
    try:
        with open(os.path.join(home, "release"), "r",
                  encoding="utf-8", errors="replace") as f:
            for _ in range(40):
                line = f.readline()
                if not line:
                    break
                if line.startswith("JAVA_VERSION="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def find_java(min_major=17):
    """找一个满足最低主版本的 java.exe。优先选**最接近**的版本（Forge 1.20.1 用 24 会拒跑）。"""
    now = time.time()
    hit = _BEST_JAVA.get(int(min_major))
    if hit and now - hit[1] < _BEST_TTL:
        return hit[0]
    best, best_major = _scan_java(min_major)
    if not best and not _DEEP_CACHE["ready"]:
        # 官启自带的 runtime 还在后台扫：等它一下再试，别急着报「没有 Java」
        _wait_deep_java()
        best, best_major = _scan_java(min_major)
    _BEST_JAVA[int(min_major)] = (best, now)
    return best


def _scan_java(min_major):
    best, best_major = "", -1
    for p in _java_candidates():
        if not os.path.isfile(p):
            continue
        major = _java_major(p)
        if major < min_major:
            continue
        # 越小越接近目标（17 优先于 21/24），同版本取先出现的
        if best_major < 0 or major < best_major:
            best, best_major = p, major
    return best, best_major


def _java_major(java_exe):
    key = os.path.normcase(str(java_exe or ""))
    try:
        mtime = os.path.getmtime(java_exe)
    except OSError:
        mtime = 0.0
    hit = _MAJOR_CACHE.get(key)
    if hit and hit[1] == mtime:
        return hit[0]
    major = _major_from_release(java_exe)
    if not major:
        major = _java_major_slow(java_exe)
    _MAJOR_CACHE[key] = (major, mtime)
    return major


def _java_major_slow(java_exe):
    """最后的兜底：真跑一次 java -version（每个 100~300ms，尽量别走到）"""
    try:
        r = subprocess.run([java_exe, "-version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        txt = (r.stderr or "") + (r.stdout or "")
        m = re.search(r'version "(\d+)(?:\.(\d+))?', txt)
        if not m:
            return 0
        major = int(m.group(1))
        if major == 1 and m.group(2):
            major = int(m.group(2))
        return major
    except Exception:
        return 0


# ---------------------------------------------------------------- 安装
def install_vanilla_only(mc, task_id, game_dir=None):
    """只下原版游戏，一个加载器都不装。返回 (ok, 版本 id/说明)。

    给「下载游戏」里加载器选「无」用：独立启动是自己拼命令行的，裸原版照样能跑；
    官方启动器那边也认这个版本目录，只是不带加载器而已。
    """
    if not game_dir:
        from .installer import detect_game_dir
        game_dir = detect_game_dir()
    TASKS.set_phase(task_id, f"准备 Minecraft {mc}")
    vid = runtime.fetch_vanilla(game_dir, mc,
                                progress=lambda s: TASKS.set_phase(task_id, s))
    if not vid:
        return False, f"无法下载原版 {mc}（检查网络后重试）"
    TASKS.set_phase(task_id, "整理版本文件")
    return True, vid


def ensure_launcher_profile_file(game_dir):
    """Forge / NeoForge 官方安装器**要求**目标目录里已经有 `launcher_profiles.json`。

    没有就直接退出（返回码 1），原文：
        There is no minecraft launcher profile in "...", you need to run the
        launcher first! There was an error during installation
    这就是「下载的整合包装了一半、Forge 从来没装成」的直接原因 —— 全新建的
    游戏目录在写档案之前是没有这个文件的，安装器连跑都不跑，processors 不执行，
    `libraries/net/minecraft/client/*-srg.jar` 与 `forge-*-client.jar` 自然也
    不会生成，启动时 ModLauncher 抛
    `Invalid paths argument, contained no existing paths`（窗口一闪都不出）。

    这里只补一个**最小合法骨架**（和官方启动器全新安装时的样子一致），
    文件已存在就一个字节都不动。
    """
    p = os.path.join(game_dir, "launcher_profiles.json")
    if os.path.isfile(p):
        return False
    try:
        os.makedirs(game_dir, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"profiles": {},
                       "settings": {"crashReports": "true"},
                       "version": 3}, f, ensure_ascii=False, indent=2)
        return True
    except Exception:                                     # noqa: BLE001
        return False


def install_loader(mc, kind, lver, task_id, game_dir=None, cancel_flag=None,
                   progress=None, keep_visible=False):
    """安装纯加载器。返回 (ok, 说明/版本 id)。工作线程调用。

    第一百零九轮：新增 `progress(text)`。以前这段只有 `TASKS.set_phase(task_id, …)`，
    而整合包导入传的是 `task_id=None` —— 玩家看到的进度条就**焊在 93% 一动不动**，
    1~3 分钟的官方安装器期间完全不知道在干什么。现在每个阶段除了写任务中心，
    还会回调给调用方（导入详情 / 补全进度条都能显示「正在下安装器 3.2/12.5 MB」）。

    第九十八轮：新增 `cancel_flag`，并把 Forge/NeoForge 安装器从
    `subprocess.run(timeout=1800)` 改成 **Popen + 0.2 秒轮询**。
    以前这 1~3 分钟（实测 Forge 47.4.10 要 210 秒）中间**一个取消检查点都没有**：
    玩家在「下载得差不多」时点取消，下载收尾后照样会进这一步，然后窗口就一直
    停在「正在取消，请稍候…」—— 这就是那条反馈里「有时候直接卡在正在取消」。
    现在是取消就 kill 子进程，一秒钟内真的停下来。

    第一百一十二轮：新增 `keep_visible`。主页「下载游戏」装的加载器版本
    （原版 + NeoForge / Forge / Fabric / Quilt）是玩家要**直接开玩的独立游戏**，
    不是给整合包当父版本的加载器 —— 不能搬进 loaders/、更不能从游戏列表消失。
    keep_visible=True 时不搬 loaders/、写 standalone 标记让它显示；若同名版本
    以前装给过整合包（躺在全局 loaders/ 里），还要搬回 versions/。
    整合包导入 / 补全路径不传它（默认 False），照旧搬走 + 藏列表。
    """

    def _finalize(vid):
        if not vid:
            return
        if keep_visible:
            ensure_visible_version_dir(game_dir, vid)
            mark_standalone_version(game_dir, vid, mc, kind, lver)
        else:
            relocate_loader_version(game_dir, vid)

    def _phase(text):
        try:
            TASKS.set_phase(task_id, text)
        except Exception:                                 # noqa: BLE001
            pass
        if progress:
            try:
                progress(str(text))
            except Exception:                             # noqa: BLE001
                pass

    def _cancelled():
        if cancel_flag and cancel_flag[0]:
            return True
        return bool(task_id and TASKS.is_cancelled(task_id))

    if not game_dir:
        from .installer import detect_game_dir
        game_dir = detect_game_dir()
    _phase(f"准备 Minecraft {mc}")
    if _cancelled():
        raise RuntimeError("已取消")
    if not runtime.fetch_vanilla(game_dir, mc, progress=_phase):
        return False, f"无法下载原版 {mc}（检查网络后重试）"
    if _cancelled():
        raise RuntimeError("已取消")

    if kind == "fabric":
        _phase("写入 Fabric 加载器")
        vid = runtime.fetch_fabric_profile(game_dir, mc, lver, progress=_phase)
        if not vid:
            return False, f"Fabric {lver} 信息获取失败"
        _finalize(vid)
        return True, vid
    if kind == "quilt":
        _phase("写入 Quilt 加载器")
        try:
            d = fetch(f"{QUILT_META}/loader/{mc}/{lver}/profile/json", timeout=15).json()
            vid = str(d.get("id") or f"quilt-loader-{lver}-{mc}")
            vdir = os.path.join(game_dir, "versions", vid)
            os.makedirs(vdir, exist_ok=True)
            with open(os.path.join(vdir, vid + ".json"), "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            _finalize(vid)
            return True, vid
        except Exception as e:                            # noqa: BLE001
            return False, f"Quilt {lver} 安装失败：{e}"

    # ---- Forge / NeoForge：跑官方 installer
    # 先查是否已经装过：installer 会覆盖同名版本目录，已有版本一律不再重装。
    # 第九十九轮：判据从「版本 json 在不在」改成「**加载器装全了没有**」。
    # 只看 json 的话，上次安装器跑到一半失败（断网 / 代理 / 取消）留下的半成品
    # 会被当成「已装好」直接返回 —— 之后每次启动都在开窗口那一瞬间抛
    # NoSuchElementException 秒退，玩家以为整合包坏了。
    exist = _find_installed_id(game_dir, mc, kind, lver)
    if exist:
        ok_l, why_l = runtime.loader_ready(game_dir, kind, lver, mc)
        if ok_l:
            # 第一百零九轮+：装好就归位 —— 整合包装的搬去全局 loaders/；
            # 主页下载的（keep_visible）反而要从 loaders/ 搬回来并标记显示
            _finalize(exist)
            return True, exist
        _phase(f"加载器不完整（{why_l}），正在重装")

    if kind == "forge":
        url = f"{FORGE_MAVEN}/{mc}-{lver}/forge-{mc}-{lver}-installer.jar"
        fname = f"forge-{mc}-{lver}-installer.jar"
    else:
        url = f"{NEO_MAVEN}/{lver}/neoforge-{lver}-installer.jar"
        fname = f"neoforge-{lver}-installer.jar"

    java = find_java(17 if _need_java17(mc) else 8)
    if not java:
        return False, ("没有找到可用的 Java（Forge/NeoForge 安装器需要 Java 运行）。"
                       "请先安装 JDK 17 或在官方启动器里装一次 1.20.1")

    dest_dir = os.path.join(app_data_dir(), "installers")
    os.makedirs(dest_dir, exist_ok=True)
    installer_path = os.path.join(dest_dir, fname)
    if not os.path.isfile(installer_path):
        _phase("下载加载器安装器…")
        try:
            r = fetch(url, timeout=120, stream=True)
        except Exception as e:                            # noqa: BLE001
            return False, f"安装器下载失败：{friendly_error(e, kind)}"
        total = int(r.headers.get("content-length") or 0)
        done = 0
        _next = 0
        with open(installer_path, "wb") as f:
            for chunk in r.iter_content(65536):
                if _cancelled():
                    raise RuntimeError("已取消")
                f.write(chunk)
                done += len(chunk)
                TASKS.set_bytes(task_id, done, total)
                # 第一百零九轮：每 512 KB 报一次文本，导入详情里能看到
                # 「下载安装器 3.2 / 12.5 MB」在动，而不是卡在 93% 干等。
                if progress and done >= _next:
                    _next = done + (512 << 10)
                    mb = done / 1048576.0
                    if total > 0:
                        _phase(f"下载安装器 {mb:.1f} / {total / 1048576.0:.1f} MB")
                    else:
                        _phase(f"下载安装器 {mb:.1f} MB")

    # 官方仓库连不上（或用户选了“始终镜像”）时，把包内库地址改到镜像再跑。
    # 第九十六轮：**镜像补丁只对「没签名」的安装器有效**。NeoForge 的 installer
    # 没有签名，改完照样能跑；Forge 的 installer 带 FORGE.SF / FORGE.RSA，改一个
    # 条目就验签失败，直接抛「A problem installing was detected」并在 3 秒内退出
    # —— 而同条件跑原版安装器是 210 秒成功（本轮实测对照）。
    # patch_installer 现在自己会拒绝签名包；这里再把两条路都准备好，谁通用谁，
    # 绝不把同一个坏包连跑两遍（第 95 轮的「重试」就是这么白重试的）。
    plain_jar = installer_path
    mirror_jar = ""
    try:
        m = patch_installer(installer_path)
        if m and os.path.normcase(os.path.abspath(m)) != \
                os.path.normcase(os.path.abspath(plain_jar)):
            mirror_jar = m
    except Exception:                                     # noqa: BLE001
        mirror_jar = ""
    if mirror_jar and (mirror_mode() == "always"
                       or (_dead & {"maven.neoforged.net",
                                    "maven.minecraftforge.net"})):
        _phase("切换国内镜像源")

    # 两条路都备好，谁通用谁。签名包拿不到镜像副本（patch_installer 会拒绝），
    # 那就同一个包再跑一遍 —— 抓库时一次 DNS/TLS 抖动是常态，第 95 轮实测重跑
    # 就好；但绝不再出现「拿坏掉的镜像副本当重试」那种假重试。
    order = [plain_jar]
    if mirror_jar:
        if mirror_mode() == "always":
            order = [mirror_jar, plain_jar]       # 玩家明确要求镜像，尊重它
        else:
            order = [plain_jar, mirror_jar]       # 官方失败时的兜底
    else:
        order = [plain_jar, plain_jar]

    _phase(f"运行 {KIND_CN.get(kind, kind)} 安装器（需要 1-3 分钟）")
    TASKS.replace_total(task_id, 0)

    def _run_installer(jar):
        """跑一次官方安装器（**可取消**）。

        `subprocess.run` 会把整段 1~3 分钟焊死在这里；换成 Popen 之后每 0.2 秒
        回来看一眼取消标志，取消就 kill —— 玩家点了取消，界面立刻能收场。
        输出仍要收全（失败时要拿尾巴给人看），所以另开一条线程读管道，
        免得管道写满把子进程堵住。
        """
        p = subprocess.Popen(
            [java, "-jar", jar, "--installClient", game_dir],
            cwd=dest_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="gbk", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW)
        chunks = []

        def _drain():
            try:
                for line in p.stdout:
                    chunks.append(line)
            except Exception:                             # noqa: BLE001
                pass

        th = threading.Thread(target=_drain, daemon=True)
        th.start()
        deadline = time.time() + 1800
        t0 = time.time()
        tick = 0
        rc = -1
        while True:
            try:
                rc = p.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pass
            # 第一百零九轮：安装器本身不吐进度，至少让玩家看到「已经跑了多久」，
            # 而不是一个停在 93% 的进度条。每 5 秒报一次。
            el = int(time.time() - t0)
            if el >= tick + 5:
                tick = el - (el % 5)
                _phase(f"{KIND_CN.get(kind, kind)} 安装器运行中…已 {el} 秒"
                       f"（通常 1-3 分钟）")
            if _cancelled():
                for die in (p.kill, p.terminate):
                    try:
                        die()
                        break
                    except Exception:                     # noqa: BLE001
                        continue
                try:
                    p.wait(timeout=5)
                except Exception:                         # noqa: BLE001
                    pass
                raise RuntimeError("已取消")
            if time.time() > deadline:
                try:
                    p.kill()
                except Exception:                         # noqa: BLE001
                    pass
                raise RuntimeError("安装器跑了 30 分钟还没结束，已中止")
        th.join(1.0)
        return rc, "".join(chunks)

    rc, out, tried = -1, "", 0
    for i, run_jar in enumerate(order):
        if _cancelled():
            raise RuntimeError("已取消")
        if i:
            TASKS.set_phase(task_id, "换一种下载方式重试安装…")
        ensure_launcher_profile_file(game_dir)   # 每个安装器都要求这个骨架
        tried += 1
        try:
            rc, out = _run_installer(run_jar)
        except Exception as e:                            # noqa: BLE001
            # 取消必须往上抛，否则会被当成「安装器返回错误」再跑一遍
            if "已取消" in str(e) or "已中止" in str(e):
                raise
            rc, out = -1, str(e)
        if rc == 0:
            break
    if rc != 0:
        if re.search(r"timed out|SocketTimeoutException|UnknownHost|SSLHandshake",
                     out, re.I):
            return False, ("安装器连不上下载源（官方与国内镜像都试过了）。"
                           "请检查网络/代理后重试，或改用 Fabric。")
        tail = re.sub(r"\s+", " ", out.strip())[-180:]
        _kinds = len({os.path.normcase(os.path.abspath(j)) for j in order[:tried]})
        more = (f"，已换 {_kinds} 种方式" if _kinds > 1
                else (f"，已试 {tried} 次" if tried > 1 else ""))
        return False, f"安装器返回错误（{rc}{more}）：{tail}"

    vid = _find_installed_id(game_dir, mc, kind, lver)
    if not vid:
        return False, ("安装器已执行，但没有在 versions 下找到对应版本。"
                       "请检查官方启动器里的版本列表")
    # 第一百零九轮+：装好就归位（整合包装的搬 loaders/ 藏起来，主页下载的留在 versions/）
    _finalize(vid)
    return True, vid


_SIGN_ENTRY = re.compile(r"^META-INF/[^/]+\.(SF|RSA|DSA|EC)$", re.I)


def patch_installer(src_path):
    """把安装器包内 JSON 里的官方 Maven 地址换成镜像地址，返回副本路径。

    官方 installer 是按 install_profile.json / version.json 里写死的 URL 抓库的，
    `--mirror` 参数不影响这一步，所以只能改包内地址。

    第九十六轮（实测教训）：**带签名的安装器绝对不能改**。Forge 的 installer 里
    有 `META-INF/FORGE.SF` + `FORGE.RSA`，改掉 `install_profile.json` 或
    `version.json` 的任何字节都会让验签失败，installer 会在 3 秒内抛
    「A problem installing was detected, install cannot continue」然后退出，
    连一个库都不下 —— 而同条件跑**未打补丁**的原版安装器，210 秒正常装完。
    NeoForge 的 installer 没签名，所以它的镜像副本一直是好的（历史日志可查）。
    以前这里不看签名，于是「官方 maven 被标死 → 永远走镜像 → 永远装不上」，
    玩家反复重装整合包都停在「文件不齐全」。
    """
    dst_path = src_path[:-4] + "-mirror.jar"
    try:
        with zipfile.ZipFile(src_path) as z:
            signed = any(_SIGN_ENTRY.match(n) for n in z.namelist())
    except Exception:                                     # noqa: BLE001
        return src_path
    if signed:
        # 顺手清掉以前生成的坏副本，免得下次又被当成可用镜像
        try:
            if os.path.isfile(dst_path):
                os.remove(dst_path)
        except OSError:
            pass
        return src_path
    if os.path.isfile(dst_path) and \
            os.path.getmtime(dst_path) >= os.path.getmtime(src_path):
        return dst_path
    repl = [("https://maven.neoforged.net/releases", MIRROR_MAVEN),
            ("https://maven.minecraftforge.net", MIRROR_MAVEN)]
    tmp = dst_path + ".part"
    changed = 0
    with zipfile.ZipFile(src_path) as zin, \
            zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            name = item.filename
            if name.count("/") <= 1 and name.endswith(".json"):
                try:
                    txt = data.decode("utf-8")
                except UnicodeDecodeError:
                    txt = ""
                if txt.lstrip().startswith("{"):
                    new = txt
                    for a, b in repl:
                        new = new.replace(a, b)
                    if new != txt:
                        data = new.encode("utf-8")
                        changed += 1
            zout.writestr(item, data)
    if not changed:
        os.remove(tmp)
        return src_path
    os.replace(tmp, dst_path)
    return dst_path


def _need_java17(mc):
    m = re.match(r"1\.(\d+)(?:\.(\d+))?", mc or "")
    if not m:
        return True
    major = int(m.group(1))
    return major >= 17


def _find_installed_id(game_dir, mc, kind, lver):
    """安装器生成的目录名不完全可控，装完后扫一遍 versions 找匹配项。

    第一百零九轮+：加载器版本可能已挪到全局 loaders/ 目录（见 paths.loaders_dir），
    所以这里两个地方都扫 —— `.minecraft/versions` 与 `loaders/`，谁先命中用谁。
    """
    from . import paths as _paths
    roots = [os.path.join(game_dir, "versions"), _paths.loaders_dir()]
    best = ""
    for vroot in roots:
        if not os.path.isdir(vroot):
            continue
        for name in os.listdir(vroot):
            jp = os.path.join(vroot, name, name + ".json")
            if not os.path.isfile(jp):
                continue
            low = name.lower()
            if kind == "forge" and "forge" in low and lver in name:
                best = name
            elif kind == "neoforge" and "neoforge" in low and lver in name:
                best = name
    return best


def relocate_loader_version(game_dir, vid):
    """把刚装好的加载器版本目录从 `.minecraft/versions/<vid>` 搬到全局 `loaders/<vid>`。

    只搬版本 json 目录；`libraries/` 里的那些 jar 一个都不动（classpath 仍指向游戏目录的
    libraries）。这样加载器版本不再出现在 `versions/` 里，游戏管理 / 启动选择就看不到它，
    而整合包实例靠 `inheritsFrom` 仍能通过 `paths.version_json_lookup` 找到它。
    版本目录名（如 neoforge-21.1.248）保持不变。
    """
    if not vid:
        return
    from . import paths as _paths
    src = os.path.join(game_dir, "versions", vid)
    if not os.path.isdir(src):
        return
    # 这个目录其实是个整合包实例（带 .mcassist.json）就不搬，免得把游戏搬飞
    if os.path.isfile(os.path.join(src, ".mcassist.json")):
        return
    dst = os.path.join(_paths.loaders_dir(), vid)
    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
        return
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.isdir(dst):
            os.replace(src, dst)
    except OSError:
        pass


def ensure_visible_version_dir(game_dir, vid):
    """keep_visible 的反向搬运：版本目录若躺在全局 `loaders/` 里，搬回
    `.minecraft/versions/<vid>`（第一百一十二轮）。

    场景：玩家先给整合包装了某加载器版本（被搬进 loaders/），之后又从主页
    「下载游戏」下载**同一个版本**当独立游戏玩 —— `install_loader` 命中
    「已装」快路径，但版本在 loaders/ 里列表看不见。这里把它搬回 versions/，
    配合 standalone 标记让它重新出现在游戏列表。目录名不变，只换位置。
    """
    if not vid:
        return
    from . import paths as _paths
    src = os.path.join(_paths.loaders_dir(), vid)
    if not os.path.isdir(src):
        return
    dst = os.path.join(game_dir, "versions", vid)
    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
        return
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.isdir(dst):
            os.replace(src, dst)
    except OSError:
        pass


def mark_standalone_version(game_dir, vid, mc="", kind="", lver=""):
    """给「玩家从主页主动下载、要当游戏玩」的加载器版本目录写 standalone 标记。

    `.mcassist.json` 里带 `standalone_version: true`（第一百一十二轮）：
    · `installer.list_game_versions` 见到它就**不隐藏** —— 它不是给整合包当
      父版本的加载器，是玩家要直接开玩的游戏；
    · `installer.list_game_instances` 见到它**不当整合包实例**（避免两边重复列）；
    · `relocate_loader_version` / `migrate_existing_loaders` 本来就跳过带
      `.mcassist.json` 的目录，不会再把它搬进 loaders/。
    """
    if not vid:
        return
    d = os.path.join(game_dir, "versions", vid)
    if not os.path.isdir(d):
        return
    mk = os.path.join(d, ".mcassist.json")
    meta = {}
    if os.path.isfile(mk):
        try:
            with open(mk, "r", encoding="utf-8") as f:
                meta = json.load(f) or {}
        except Exception:                                 # noqa: BLE001
            meta = {}
    meta.update({
        "standalone_version": True,
        "version_id": str(meta.get("version_id") or vid),
        "mc": str(meta.get("mc") or mc or ""),
        "loader": str(meta.get("loader") or kind or ""),
        "loader_ver": str(meta.get("loader_ver") or lver or ""),
    })
    try:
        with open(mk, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def migrate_existing_loaders(game_dir=None):
    """第一百零九轮+：把还留在 `.minecraft/versions` 里的加载器版本目录
    （forge / neoforge / fabric / quilt，如 `neoforge-21.1.248`）一次性挪到全局
    `loaders/`，让它们从「游戏管理 / 选择要启动的游戏」里彻底消失。

    启动时跑一次即可（幂等）：已经在 loaders/ 的不会重复搬；整合包实例（带
    `.mcassist.json`）一律不碰；只搬有加载器主坐标、且不是「胖壳实例」的版本目录。
    atomic `os.replace`，异常全吞，绝不拦着软件启动，也不动 `libraries/` 里的 jar。
    """
    if game_dir is None:
        from .installer import detect_game_dir
        game_dir = detect_game_dir()
    if not game_dir:
        return
    vroot = os.path.join(game_dir, "versions")
    if not os.path.isdir(vroot):
        return
    try:
        names = os.listdir(vroot)
    except OSError:
        return
    for n in names:
        src = os.path.join(vroot, n)
        if not os.path.isdir(src):
            continue
        # 整合包实例（带 .mcassist.json）不是加载器，别搬飞
        if os.path.isfile(os.path.join(src, ".mcassist.json")):
            continue
        try:
            info = runtime.describe_version(src)
        except Exception:                                 # noqa: BLE001
            continue
        if not info:
            continue
        if str(info.get("loader") or "") not in \
                ("forge", "neoforge", "fabric", "quilt"):
            continue
        if info.get("pack"):
            continue
        relocate_loader_version(game_dir, n)
