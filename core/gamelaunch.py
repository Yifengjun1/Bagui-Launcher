# -*- coding: utf-8 -*-
"""独立启动：不经过官方启动器，直接用 java 把 Minecraft 拉起来。

官方启动器只认「档案 + 版本 json」，玩家想一键玩某个包时还得等它转圈、
选档案、点开始。这里按 Mojang 的启动规范自己拼命令行，做到「点一下直接进游戏」。

拼命令行的完整流程（都是可测的纯函数，真正 spawn 的只有 `launch()`）：
  1. `resolve_version()`  从 versions/<id>/<id>.json 沿 inheritsFrom 往上合
                           （整合包的壳 json 只有 id + mcassist，库与参数都在基底里）
  2. `libraries()`        过 rules（os.name / os.arch / features）、算 classpath、
                           分出要解压的 natives 包（`natives-` 分类器 / 老版 natives 字段）
  3. `arguments()`        过 rules + 代占位符（auth_* / classpath / library_directory…）
  4. `extract_natives()`  把 dll 抽到 <实例>/natives
  5. `launch()`           选 Java（按版本 json 的 javaVersion）、起进程、写日志

⚠ 三个实测坑：
  · jvm 参数里已经带 `-cp ${classpath}`，再追加一遍 `-cp` 会让 java 只认最后一个，
    Forge 的 module path 就全废了 —— 所以只在「没有 -cp / -classpath」时才补。
  · Forge/NeoForge 的 jvm 参数里有 `${library_directory}`、`${classpath_separator}`，
    不代换会直接报 ClassNotFound。
  · `assets/indexes/<index>.json` 缺失时游戏会崩在启动阶段（不是黑屏）。
    第 59 轮起由 `core/assetsync.py` 自己补齐（索引 + 全部 objects），
    **不再要求玩家先用官方启动器跑一次** —— 干净设备上那是个死循环。
  · natives 不是永远平铺的：1.21+ / 26.x 的 json 会声明
    `${natives_directory}/java`、`/lwjgl`、`/jna`、`/netty` 四个子目录，
    而且这些 dll 得能从 **classpath 里的 natives jar** 抽出来。少了任何一头
    就是 `Failed to locate library: lwjgl.dll`（窗口都不出现）。
"""
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import time
import zipfile

import requests

from . import crashlog, javamanager, ram
from .config import CONFIG

APP_NAME = "MCGuanQi"
APP_VER = "1.0.0"

# 可选特性一律关掉（游戏不需要 demo / 自定义分辨率 / quickPlay）
FEATURES = {
    "is_demo_user": False,
    "has_custom_resolution": False,
    "has_quick_plays_support": False,
    "is_quick_play_singleplayer": False,
    "is_quick_play_multiplayer": False,
    "is_quick_play_realms": False,
}


def _arch_name():
    """本机架构：x86_64 / x86 / arm64。

    ⚠ 绝不能用 PROCESSOR_ARCHITECTURE 判定：在 Git Bash、部分管理员会话、
    某些服务里它是 "x86" 甚至是空字符串，而解释器本身是 64 位 ——
    那样会把 `natives-windows-x86` 当成「本机要的」解压出来，
    64 位 JVM 加载 32 位 lwjgl.dll 直接 `UnsatisfiedLinkError`，
    表现就是「点了启动，游戏窗口根本不出现」。用解释器位数 + machine() 才靠谱。
    """
    mach = (platform.machine() or "").lower()
    if "arm" in mach or "aarch" in mach:
        return "arm64"
    return "x86_64" if struct.calcsize("P") * 8 == 64 else "x86"


_ARCH = _arch_name()
_ARCH_ALIASES = {
    "x86_64": {"x86_64", "amd64", "x64", "64"},
    "x86": {"x86", "i386", "i686", "32"},
    "arm64": {"arm64", "aarch64"},
}
_ARCH_TOKEN = {"x86_64": "64", "x86": "32", "arm64": "arm64"}   # ${arch} 要代换成这个

_RUN = {}          # 当前跑着的游戏进程 {"proc", "log", "name", "started"}


class LaunchError(RuntimeError):
    """能直接给玩家看的中文错误"""


# ---------------------------------------------------------------- 读 json
def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def version_json_path(game_dir, vid):
    from . import paths as _paths
    # 加载器版本（如 neoforge-21.1.248）可能已挪到全局 loaders/ 目录，
    # 优先在本游戏目录找，找不到再去 loaders/ 找（见 paths.version_json_lookup）。
    return _paths.version_json_lookup(game_dir, vid)


def resolve_version(game_dir, vid):
    """返回 (合并后的版本 dict, 继承链 [leaf, parent, …])。

    `inheritsFrom` 的合并规则按 Mojang 规范：
      · libraries 追加（子在前，重复坐标以后出现的为准）
      · arguments.game / arguments.jvm 追加 —— **但子版本自带整套参数时不叠加**
        （整合包常抄一份完整档案又写 inheritsFrom，叠加会出两个 --gameDir/-cp）
      · minecraftArguments / mainClass / assetIndex / javaVersion / type 子覆盖父
    """
    chain, cur, seen = [], str(vid or ""), set()
    while cur and cur not in seen and len(chain) < 8:
        seen.add(cur)
        d = _read_json(version_json_path(game_dir, cur))
        if not d:
            break
        chain.append((cur, d))
        cur = str(d.get("inheritsFrom") or "")

    if not chain:
        raise LaunchError(f"找不到版本 {vid} 的 json（versions/{vid}/{vid}.json）")

    data = {}
    for _name, d in reversed(chain):          # 从基底往下盖
        data = _merge(data, d)
    data.setdefault("id", chain[0][0])
    return data, [c[0] for c in chain]


# 子版本「自带整套参数」的判定标记（第四十七轮）
#
# 整合包的版本 json 有两种写法：
#   A) 规范的加载器档案 —— 只有自己的 jvm/库，启动参数靠 inheritsFrom 拿原版的；
#   B) 直接抄一份完整档案 —— 自带 `--gameDir …` 和 `-cp ${classpath}`，同时又写了
#      `inheritsFrom: 1.20.1`（很多 mrpack / 第三方整合包就是这样）。
#
# 按 Mojang 规范无脑「父参数 + 子参数」拼接，B 类会拿到两个 `--gameDir`、两个 `-cp`、
# 两个 `-p` → ModLauncher 的 joptsimple 直接抛
#   MultipleArgumentsForOptionException: Found multiple arguments for option gameDir
# 游戏窗口一闪都不出现（实机日志 logs/mcassist-launch.log 第 6 行就是这个）。
# 所以：子版本自己带了整套参数时，父版本那一份**整体丢弃**，不叠加。
# 判定用「必含项」而不是「含一个就算」—— 只写了 --gameDir 的半套参数不算数，
# 宁可按老规矩拼（少参数会崩，重复参数连门都进不去）。
_GAME_SELF_ARGS = ("--gameDir", "--username", "--assetsDir", "--assetIndex")
_JVM_SELF_ARGS = ("${classpath}",)


def _arg_text(a):
    """把 arguments 里的一项（字符串或 {rules,value}）摊成可比对的文本"""
    if isinstance(a, str):
        return a
    if isinstance(a, dict):
        v = a.get("value")
        if isinstance(v, list):
            return " ".join(str(x) for x in v)
        if v is not None:
            return str(v)
    return ""


def _args_self_sufficient(args, markers, need_all=False):
    """这一段参数是不是「自己就够用」（markers 必含项全在 / 有一个即可）"""
    if not args:
        return False
    blob = "\n".join(_arg_text(a) for a in args)
    hits = [m for m in markers if m in blob]
    return len(hits) == len(markers) if need_all else bool(hits)


def _join_args(base_args, child_args, markers, need_all=False):
    """父/子两段参数合并：子版本自带整套就只留子版本，否则子版本接在父版本后面"""
    b = list(base_args or [])
    c = list(child_args or [])
    if _args_self_sufficient(c, markers, need_all):
        return c
    return b + c


def _arg_segments(tokens):
    """把 ['--a','x','--b','y'] 切成 [('--a',['x']), ('--b',['y'])]（主类这种裸值当独立段）"""
    segs, cur = [], None
    for t in tokens:
        if isinstance(t, str) and t.startswith("-"):
            cur = [t, []]
            segs.append(cur)
        elif cur is None:
            segs.append([t, []])
        else:
            cur[1].append(t)
    return segs


def _merge_game_args(base_args, child_args):
    """启动参数（`--gameDir` 那一段）合并，专门防「同一个选项出现两次」。

    ModLauncher 的 joptsimple 一见到两个 `--gameDir` 就直接抛
    `MultipleArgumentsForOptionException`，游戏窗口连闪都不闪 —— 所以这一段的合并
    必须按「选项」去重，不能简单相加：
      · 子版本参数齐全（四个关键项都有）→ 整段用子版本的；
      · 子版本只有半套 → 父版本的照用，只把父版本没有的选项补进去；
      · 子版本没有 → 用父版本的。
    """
    b = list(base_args or [])
    c = list(child_args or [])
    if not c:
        return b
    if _args_self_sufficient(c, _GAME_SELF_ARGS, need_all=True):
        return c
    keys = {s[0] for s in _arg_segments(b)}
    out = list(b)
    for head, vals in _arg_segments(c):
        if head in keys:
            continue                 # 父版本已经给过这个选项，别重复
        out.append(head)
        out.extend(vals)
    return out


def _merge(base, child):
    out = dict(base)
    for k, v in (child or {}).items():
        if k == "libraries":
            out["libraries"] = list(base.get("libraries") or []) + list(v or [])
        elif k == "arguments":
            b = base.get("arguments") or {}
            c = v or {}
            out["arguments"] = {
                "game": _merge_game_args(b.get("game"), c.get("game")),
                "jvm": _join_args(b.get("jvm"), c.get("jvm"), _JVM_SELF_ARGS),
            }
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------- rules
def _rules_ok(rules, features=None):
    """Mojang 的 rules 判定：没有 rules = 允许；否则按 allow/disallow 顺序覆盖"""
    if not rules:
        return True
    feats = FEATURES if features is None else features
    allow = False
    for r in rules:
        if not isinstance(r, dict):
            continue
        os_ = r.get("os") or {}
        if os_.get("name") and os_["name"] != "windows":
            continue
        if os_.get("arch"):
            if str(os_["arch"]).lower() not in _ARCH_ALIASES.get(_ARCH, {_ARCH}):
                continue
        f = r.get("features") or {}
        if any(bool(feats.get(k)) is not bool(v) for k, v in f.items()):
            continue
        act = str(r.get("action") or "").lower()
        if act == "allow":
            allow = True
        elif act == "disallow":
            return False
    return allow


# ---------------------------------------------------------------- library
def _maven_rel(name):
    """'g:a:v[:cls]' -> 'g/a/v/a-v[-cls].jar'

    Forge 安装器会在坐标后面挂 `@zip` 这种扩展名（`g:a:v:cls@zip`），要剥掉，
    否则算出来的文件名是 `a-v-cls@zip.jar`，下载必然 404。
    """
    s = str(name or "").strip()
    if s.startswith("[") and s.endswith("]"):     # 极老的 Forge 用方括号包坐标
        s = s[1:-1]
    parts = s.split(":")
    if len(parts) < 3:
        return ""
    g, a, v = parts[0], parts[1], parts[2]
    cls = parts[3] if len(parts) > 3 else ""
    ext = "jar"
    if "@" in cls:
        cls, ext = cls.split("@", 1)
    elif "@" in v:
        v, ext = v.split("@", 1)
    fn = f"{a}-{v}" + (f"-{cls}" if cls else "") + f".{ext or 'jar'}"
    return "/".join([g.replace(".", "/"), a, v, fn])


def _classifier_arch_ok(cls):
    """分类器里的架构词是否匹配本机。

    ⚠ Mojang 的 json 里 `natives-windows`、`natives-windows-x86`、
    `natives-windows-arm64` 三条的 rules **完全一样**（只有 os.name=windows），
    架构只写在分类器名字里 —— 官方启动器自己按名字挑。不按名字过滤就会把
    32 位 / ARM 的 dll 一起解压出来，最后解压的那个覆盖先前的，
    64 位 JVM 加载 32 位 lwjgl.dll 直接 UnsatisfiedLinkError（窗口不出现）。
    """
    low = str(cls or "").lower()
    if not low:
        return True
    if "x86_64" in low or "amd64" in low:      # 先判 64 位，否则会被下面的 "x86" 误伤
        return _ARCH == "x86_64"
    toks = low.split("-")
    if "arm64" in toks or "aarch64" in toks:
        return _ARCH == "arm64"
    if "x86" in toks:
        return _ARCH == "x86"
    return True


# 库没有下载地址时按 group 前缀猜一个公开 maven（很多老 Forge/PCL2 的 json 里
# 只写了坐标，地址全靠启动器自己按惯例拼）
_MAVEN_BY_GROUP = (
    (("net.fabricmc", "org.quiltmc"), "https://maven.fabricmc.net/"),
    (("net.minecraftforge", "cpw.mods", "org.ow2.asm", "net.neoforged"),
     "https://maven.minecraftforge.net/"),
)
_DEFAULT_MAVEN = "https://libraries.minecraft.net/"


def _guess_maven(group):
    g = str(group or "").lower()
    for prefix, base in _MAVEN_BY_GROUP:
        if any(g.startswith(p) for p in prefix):
            return base
    return _DEFAULT_MAVEN


def lib_records(game_dir, data):
    """展开 libraries（过 rules、去重），返回
        [{"name": maven坐标, "path": 本地路径, "url": 下载地址, "size": 字节, "native": bool}]
    """
    libdir = os.path.join(game_dir, "libraries")
    order, recs, natives = [], {}, []
    for lib in (data.get("libraries") or []):
        if not isinstance(lib, dict):
            continue
        if not _rules_ok(lib.get("rules")):
            continue
        name = str(lib.get("name") or "")
        parts = name.split(":")
        if len(parts) < 3:
            continue
        cls = parts[3] if len(parts) > 3 else ""
        art = (lib.get("downloads") or {}).get("artifact") or {}

        # 老格式：natives 字段指明各系统的分类器（1.19 前）
        # ⚠ 这类包的下载地址在 downloads.classifiers[<分类器>] 里，不在 artifact 里；
        #   只读 artifact 会拿到空 url → 补不下来 → 1.12.2/1.7.10 这类版本永远起不来。
        nat = lib.get("natives") or {}
        if nat:
            cls_win = str(nat.get("windows") or "")
            if not cls_win:
                continue                      # 这个平台不需要，跳过
            cls_win = cls_win.replace("${arch}", _ARCH_TOKEN.get(_ARCH, "64"))
            if not _classifier_arch_ok(cls_win):
                continue
            cl = ((lib.get("downloads") or {}).get("classifiers") or {}).get(cls_win) or {}
            rel = cl.get("path") or _maven_rel(
                f"{parts[0]}:{parts[1]}:{parts[2]}:{cls_win}")
            if rel:
                natives.append({"name": name + ":" + cls_win,
                                "path": os.path.join(libdir, *rel.split("/")),
                                "url": str(cl.get("url") or ""),
                                "size": int(cl.get("size") or 0),
                                "native": True})
            continue

        rel = art.get("path") or _maven_rel(name)
        if not rel:
            continue
        url = str(art.get("url") or "")
        if not url:
            # Forge / Fabric / OptiFine 的安装器写的条目只有 `url`（maven 仓库根）+ name，
            # 没有 downloads 段。只读 downloads 会拿到空 url → 这些版本永远「补不下来」
            # → 独立启动直接被拦。用仓库根 + 相对路径拼出来。
            base = str(lib.get("url") or "")
            if base:
                url = base.rstrip("/") + "/" + rel
        if not url:
            # 连仓库根都没有（老 Forge 合并版常见）：按坐标惯例猜一个公开 maven
            url = _guess_maven(parts[0]).rstrip("/") + "/" + rel
        rec = {"name": name, "path": os.path.join(libdir, *rel.split("/")),
               "url": url, "size": int(art.get("size") or 0),
               "native": "natives-" in cls}
        if rec["native"]:
            if _classifier_arch_ok(cls):        # 别把别的架构的 dll 一起解出来
                natives.append(rec)
            continue
        key = f"{parts[0]}:{parts[1]}"   # 同坐标只留最后一个（加载器会覆盖原版库）
        old = recs.get(key)
        if old is None:
            order.append(key)
        elif not rec["url"]:
            # Forge 的 json 会把原版库（guava / commons-lang3…）重新列一遍但不给地址，
            # 直接覆盖就把 Mozilla 那份下载地址丢了 → 永远补不下来。地址沿用旧的。
            rec["url"] = old.get("url") or ""
            if not rec["size"]:
                rec["size"] = int(old.get("size") or 0)
        recs[key] = rec
    return [recs[k] for k in order] + natives


def libraries(game_dir, data):
    """算 classpath 与 natives 包，返回 (classpath 文件列表, natives jar 列表)"""
    recs = lib_records(game_dir, data)
    return ([r["path"] for r in recs if not r["native"]],
            [r["path"] for r in recs if r["native"]])



def client_jar(game_dir, chain):
    """链上第一个真正存在的 <id>.jar（原版 jar 通常在最底层）"""
    for vid in chain:
        p = os.path.join(game_dir, "versions", vid, vid + ".jar")
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    return ""


# 客户端 jar 全链都缺时的说明（第四十八轮）：玩家说的「整合包文件夹里少了个 jar」
# 通常就是这一份 —— 官启按 lastVersionId 去 versions/<id>/<id>.jar 找，找不到就不启动。
_CLIENT_JAR_HINT = (
    "「{name}」缺少客户端 jar（versions/{vid}/{vid}.jar），本机其它启动器目录和"
    "自动补下都没成功。到「游戏管理」里重新安装 / 修复这个版本即可"
    "（联网状态下会自动补下这份 jar）。")


def ensure_client_jar(game_dir, data, chain, tell=None):
    """继承链上一份客户端 jar 都没有时，尽量补一个；补不上返回空串。

    ① 本机其它启动器目录里有同版本的 → 直接复制（零下载，最常见）
    ② 版本 json 里带 downloads.client.url → 下最底层（原版）那一份
    """
    vids = list(reversed(list(chain or [])))      # 从最底层（原版）往上试
    try:
        from . import runtime
        here = os.path.normcase(os.path.abspath(game_dir))
        roots = [r for r in runtime.candidate_roots()
                 if os.path.normcase(os.path.abspath(r)) != here]
    except Exception:
        roots = []
    for vid in vids:
        for root in roots:
            src = os.path.join(root, "versions", vid, vid + ".jar")
            try:
                if not (os.path.isfile(src) and os.path.getsize(src) > 0):
                    continue
                dst = os.path.join(game_dir, "versions", vid, vid + ".jar")
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                if tell:
                    tell(f"复用本机客户端 jar：{vid}")
                return dst
            except Exception:
                continue
    url = str(((data.get("downloads") or {}).get("client") or {}).get("url") or "")
    if url and vids:
        vid = vids[-1]
        dst = os.path.join(game_dir, "versions", vid, vid + ".jar")
        try:
            if tell:
                tell("补下客户端 jar…")
            from . import dl
            dl.download(url, dst)
            if os.path.isfile(dst) and os.path.getsize(dst) > 0:
                return dst
        except Exception:
            pass
    return ""


_NATIVES_SUB_RE = re.compile(r"\$\{natives_directory\}[/\\]([\w.-]+)")


def natives_subdirs(data):
    """版本 json 声明了哪些 natives 子目录（通常 `java`/`jna`/`lwjgl`/`netty`）。

    判据是 jvm 参数里 `${natives_directory}/xxx` 这种写法。1.20.1 及更早的
    json 里一个都没有（natives 平铺），所以老版本返回空列表、走原来的平铺逻辑，
    行为不变。
    """
    subs = []
    for a in ((data.get("arguments") or {}).get("jvm") or []):
        if isinstance(a, str):
            vals = [a]
        elif isinstance(a, dict):
            v = a.get("value")
            if isinstance(v, list):
                vals = [str(x) for x in v]
            elif v is None:
                vals = []
            else:
                vals = [str(v)]
        else:
            continue
        for s in vals:
            for m in _NATIVES_SUB_RE.finditer(s):
                sub = m.group(1)
                if sub and sub not in subs:
                    subs.append(sub)
    return subs


def extract_natives(jars, dest, subs=None):
    """把 natives jar 里的动态库抽到 dest，返回抽出的文件数。

    `subs`（第 59 轮）：新版 MC 的 natives **不是平铺的**。版本 json 里明确写着

        -Djava.library.path=${natives_directory}/java
        -Djna.tmpdir=${natives_directory}/jna
        -Dorg.lwjgl.system.SharedLibraryExtractPath=${natives_directory}/lwjgl
        -Dio.netty.native.workdir=${natives_directory}/netty

    也就是 dll 必须落在 `java` / `jna` / `lwjgl` / `netty` 这些子目录里。只往
    根目录解压，`Library.loadSystem()` 去 `natives/java` 找 `lwjgl.dll` 就是空的，
    报 `UnsatisfiedLinkError: Failed to locate library: lwjgl.dll`，窗口都出不来。
    1.20.1 及更早是平铺的（`${natives_directory}` 后面没有子目录），所以这个
    坑只在换到新版本游戏时才炸 —— 老版本永远测不出来。

    这些 jar 加起来只有 7~8 MB，所以根目录和每个子目录都放一份：比按库名去猜
    「这个 dll 归哪个子目录」稳，多占的空间也可以忽略。
    """
    os.makedirs(dest, exist_ok=True)
    targets = [dest]
    for s in (subs or []):
        s = str(s or "").strip().strip("/\\")
        if not s or s in (".", ".."):
            continue
        targets.append(os.path.join(dest, s))
    n = 0
    for jp in jars:
        if not os.path.isfile(jp):
            continue
        try:
            with zipfile.ZipFile(jp) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    low = info.filename.lower()
                    if low.startswith("meta-inf/"):
                        continue
                    if not low.endswith((".dll", ".so", ".dylib", ".jnilib")):
                        continue
                    base = os.path.basename(info.filename)
                    if not base:
                        continue
                    one = None
                    try:
                        one = os.path.getsize(
                            os.path.join(dest, base)) == info.file_size
                    except OSError:
                        one = False
                    if not one:
                        with z.open(info) as src, open(os.path.join(dest, base), "wb") as out:
                            shutil.copyfileobj(src, out)
                    # 新版 MC 的子目录也要各放一份（见上面 subs 的说明）
                    for sub in targets:
                        if sub == dest:
                            continue
                        try:
                            os.makedirs(sub, exist_ok=True)
                            p = os.path.join(sub, base)
                            if os.path.isfile(p) and os.path.getsize(p) == info.file_size:
                                continue
                            with z.open(info) as src, open(p, "wb") as out:
                                shutil.copyfileobj(src, out)
                        except Exception:
                            continue
                    n += 1
        except Exception:
            continue
    return n


# ---------------------------------------------------------------- auth
def _offline_uuid(name):
    from .session import offline_session
    return offline_session(name or "Player")["uuid"]


def _xuid_from_token(token: str) -> str:
    """从 Minecraft access_token（JWT）的 payload 里取 xuid。

    第 118 轮：老 session 的 ``xuid`` 是空的（profile API 不返回它），
    但 token 本身是 JWT，payload 自带 ``xuid``。这样不用重新登录、不用
    再打一次 XSTS，所有现有账号都能立刻拿到真 XUID 供 `--xuid` 使用。
    解析失败返回空串（xuid 只是遥测字段，绝不能因为它让启动失败）。
    """
    try:
        import base64
        import json as _json
        seg = str(token or "").split(".")
        if len(seg) < 2:
            return ""
        pad = seg[1] + "=" * (-len(seg[1]) % 4)
        data = _json.loads(base64.urlsafe_b64decode(pad.encode("ascii")))
        return str(data.get("xuid") or data.get("xid") or "")
    except Exception:                                  # noqa: BLE001
        return ""


def auth_fields(session):
    """把本地会话翻成启动参数需要的几项。

    微软正版要 access_token；没有 token（离线/失效）就退到离线模式，
    离线也能进单人游戏与局域网，只是不能进正版服务器。
    """
    s = session or {}
    name = str(s.get("name") or "Player")
    uid = str(s.get("uuid") or "") or _offline_uuid(name)
    uid = uid.replace("-", "")
    token = str(s.get("mc_token") or "")
    mode = str(s.get("mode") or "")
    server = str(s.get("server") or "")
    # 第六十八轮：第三方（authlib-injector 外置登录）也算「在线」——
    # 它有真 token，能进这个站的验证服；userType 用 mojang（yggdrasil 体系的写法）。
    third = mode == "thirdparty" and bool(token) and bool(server)
    online = third or (bool(token) and mode == "microsoft")
    if mode == "microsoft" and online:
        utype = "msa"
    elif third:
        utype = "mojang"
    else:
        # 微软账户但令牌失效（第八十轮）：这时按离线处理更诚实 —— 继续用
        # `--userType msa` 配一个假 token，游戏只会一路 401，皮肤还是默认的，
        # 而界面上却写着「正版验证」，玩家根本对不上号。
        utype = "legacy"
    return {
        "name": name,
        "uuid": uid,
        "token": token if online else "0" * 32,
        "user_type": utype,
        "xuid": str(s.get("xuid") or "") or (_xuid_from_token(token) if online else ""),
        "online": online,
        "server": server,
        "thirdparty": third,
        "note": str(s.get("_auth_note") or ""),
        # 第 118 轮：--clientId 以前写死空。同步登录用的 Azure Client ID
        # （session 里有就用它，否则用内置公共 ID），与令牌配套。
        "client_id": str(s.get("client_id") or "") or _builtin_client_id(),
    }


def _builtin_client_id() -> str:
    try:
        from . import msa
        return msa.client_id()
    except Exception:                                  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 补文件
# 官启点「开始游戏」时会自己把缺的库和资源补上；我们独立启动没有这一步，
# 缺什么就得自己下。
#
# 第 59 轮修正了一处判断错误：以前这里写着「资源对象缺了只是没声音/材质，
# 所以只补索引 + 警告，不拦着玩家」。实际情况比那严重得多 —— assets/objects
# 里同时装着语言文件（*.json）和主菜单全景贴图，缺了会变成：
#   · 主菜单背景是品红/黑棋盘格（Mojang 的 missingno 纹理）
#   · 一个声音都没有
#   · **语言列表是空白的**，中文切不了
# 而这三件事在装过官方启动器的机器上永远复现不出来（那边 assets 早就下全了，
# paths.py 还会联接过来复用），所以一直被当成「个别玩家的环境问题」。
# 现在改成真下，见 core/assetsync.py。
MAX_REPAIR = 1500 << 20      # 一次补库超过这个体积就分段提示（不再拒绝）

# 「缺了就一定起不来」的库：加载器本体、LWJGL、ASM/Mixin 这类
_CORE_LIB_HINTS = ("forge", "fabric", "launchwrapper", "bootstraplauncher",
                   "lwjgl", "jinput", "minecraft:", "asm", "mixin", "authlib",
                   "text2speech")


def _is_core_lib(name):
    low = str(name or "").lower()
    return any(h in low for h in _CORE_LIB_HINTS)


def _reuse_libraries(game_dir, missing, tell=None, limit=600):
    """本机别的启动器（PCL2 / HMCL / Prism）里同路径的库直接复制过来。

    玩家的库往往早就在另一个 .minecraft 里下好了（本机 libraries 还是 junction 共享的），
    为几个 jar 重下几十 MB 不值当；复用不到才走网络。
    """
    if not missing:
        return 0
    try:
        from . import runtime
        here = os.path.normcase(os.path.abspath(game_dir))
        roots = [r for r in runtime.candidate_roots()
                 if os.path.normcase(os.path.abspath(r)) != here]
    except Exception:
        return 0
    if not roots:
        return 0
    libdir = os.path.join(game_dir, "libraries")
    got = 0
    for i, r in enumerate(missing[:limit]):
        try:
            rel = os.path.relpath(r["path"], libdir)
        except Exception:
            continue
        for root in roots:
            src = os.path.join(root, "libraries", rel)
            try:
                if not os.path.isfile(src) or os.path.getsize(src) == 0:
                    continue
                os.makedirs(os.path.dirname(r["path"]), exist_ok=True)
                shutil.copy2(src, r["path"])
                got += 1
                if tell:
                    tell(f"复用本机库 {i + 1}/{len(missing)}：{os.path.basename(r['path'])}")
            except Exception:
                continue
            break
    return got


def ensure_files(game_dir, data, progress=None):
    """补齐缺失的库 / natives / 资源索引，返回提示文字（没缺的返回空串）。"""
    from . import dl

    def tell(t):
        if progress:
            try:
                progress(t)
            except Exception:
                pass

    recs = lib_records(game_dir, data)
    missing = [r for r in recs if not os.path.isfile(r["path"])]
    notes = []
    if missing:
        # ① 先在本机其它启动器的 libraries 里找同路径的（不用下载）
        reused = _reuse_libraries(game_dir, missing, tell)
        if reused:
            notes.append(f"复用本机已有的 {reused} 个库文件")
            missing = [r for r in recs if not os.path.isfile(r["path"])]
    if missing:
        total = sum(r["size"] for r in missing)
        todo = [r for r in missing if r["url"]]
        # 干净设备第一次启动时，整套库都得现下（几百 MB）。以前这里超过
        # 1.5 GB 就直接 LaunchError「先用官方启动器补齐」，可那台机器上根本
        # 没有官方启动器 —— 等于把人堵死在门口。现在照下，只是把体积说清楚。
        if total > MAX_REPAIR:
            tell(f"需要补下 {len(missing)} 个库文件（约 {total >> 20} MB），会有点久…")
        for i, r in enumerate(todo):
            tell(f"补下库文件 {i + 1}/{len(todo)}：{os.path.basename(r['path'])}")
            try:
                dl.download(r["url"], r["path"], expected_size=r["size"])
            except Exception:
                pass
        left = [r for r in recs if not os.path.isfile(r["path"])]
        if left:
            names = "、".join(os.path.basename(r["path"]) for r in left[:3])
            # 加载器本体 / LWJGL 缺了必然起不来（Black screen 或 NoClassDefFoundError），
            # 直接拦下说清楚；其它零散条目（OptiFine 之类装了没装的残留）放行，
            # 免得因为一个可选库把整次启动挡住 —— 真崩了日志里也看得见。
            if any(_is_core_lib(r["name"]) for r in left) or len(left) > 6:
                raise LaunchError(
                    f"还有 {len(left)} 个库文件补不下来（{names}…）。"
                    f"检查一下网络再启动；一直不行就到「游戏管理」里修复这个版本。")
            notes.append(f"有 {len(left)} 个可选库缺失（{names}），缺了也能启动")
        if todo:
            notes.append(f"已补下 {len(todo)} 个库文件")

    # 资源（音效 / 语言 / 主菜单贴图全在 assets/objects 里）
    # 第 59 轮：从「只补索引 + 警告」改成真下。以前那套等于默认「玩家一定有
    # 官方启动器兜着」，而干净设备上就是花屏、无声、语言列表空白。
    try:
        from . import assetsync
        ok, note = assetsync.ensure(game_dir, data, progress=tell)
        if note:
            notes.append(note)
        if not ok:
            # 索引都补不下来时游戏会崩在启动阶段，这里拦下比让它闪退好
            raise LaunchError(f"{note}。检查网络后重试。")
    except LaunchError:
        raise
    except Exception as e:
        notes.append(f"资源补齐出错（{e}），画面/音效可能不全")
    return "；".join(notes)


# 原来的 _assets_mostly_missing()（抽查 24 个对象、大部分不在就提示一句
# 「建议先用官方启动器跑一次」）在第五十九轮删掉了 —— 现在由
# core/assetsync.missing() 给出完整清单并直接下完，不再只给个提示。


# ---------------------------------------------------------------- 参数
def _sub(text, repl):
    out = text
    for k, v in repl.items():
        out = out.replace("${" + k + "}", str(v))
    return out


def _flatten(args):
    """arguments 里既有字符串也有 {rules, value} —— 先过 rules 再摊平"""
    out = []
    for a in args or []:
        if isinstance(a, str):
            out.append(a)
            continue
        if not isinstance(a, dict):
            continue
        if not _rules_ok(a.get("rules")):
            continue
        v = a.get("value")
        if isinstance(v, list):
            out.extend([str(x) for x in v])
        elif v is not None:
            out.append(str(v))
    return out


# ---------------------------------------------------------------- 第 104 轮：按「选项」去重
# 第 48 轮的 `inheritsFrom` 重复 --gameDir / -cp 会让 Forge 的 argparse 抛
# MultipleArgumentsForOptionException。开放自定义 JVM / 游戏参数后，重复的
# -Xms / -Xmx / -cp 更容易出现 —— 自定义参数必须先过这一道，再拼进 argv。
# **只对「只允许出现一次」的选项去重**：--add-opens / --add-exports /
# --tweakClass 这类按规范可以重复，压成一个反而会让模组加载器少拿参数。
_CP_KEYS = ("-cp", "-classpath", "--class-path")
_VALUE_OPTS = set(_CP_KEYS) | {
    "-p", "--module-path", "--upgrade-module-path", "--patch-module",
    "--gameDir", "--username", "--version", "--versionType", "--assetIndex",
    "--assetsDir", "--uuid", "--accessToken", "--userType", "--userProperties",
    "--server", "--port", "--width", "--height", "--tweakClass", "--title",
}
_UNIQUE_FLAGS = {"--demo", "--fullscreen", "--disableMultiplayer",
                 "--disableChat", "--quickPlay", "--launchTarget"}
_MULTI_OPTS = {"--tweakClass", "--add-modules", "--add-opens",
               "--add-exports", "--patch-module"}


def _option_key(t):
    """返回这个 token 的「选项身份」；None = 不参与去重。"""
    s = str(t or "")
    if not s.startswith("-"):
        return None
    if s in _CP_KEYS:
        return "-cp"
    for p in ("-Xmx", "-Xms", "-Xmn", "-Xss"):
        if s.startswith(p):
            return p
    if s.startswith("-XX:"):
        body = s[4:].lstrip("+-")
        return "-XX:" + body.split("=", 1)[0]
    if s.startswith("-D"):
        return "-D:" + s[2:].split("=", 1)[0]
    if s.startswith("-javaagent:"):
        return "-javaagent"
    if s.startswith("-agentlib:"):
        return "-agentlib"
    if s.startswith("--"):
        base = s.split("=", 1)[0]
        if base in _MULTI_OPTS:
            return None                 # 规范允许多个，压成一个会丢参数
        return base if base in _UNIQUE_FLAGS or base in _VALUE_OPTS else None
    if s in ("-p", "--module-path"):
        return s
    return None


def dedup_jvm_args(tokens):
    """同一「选项」只保留**最后一次**出现，值跟着那一次一起保留。

    放在最后是有意的：默认参数 → 版本 json → 玩家自定义，越靠后越该生效。
    需要单独值 token 的选项（-cp / --gameDir / --username…）连值一起搬，
    绝不会把 classpath 或路径留在原地变成裸参数。
    """
    entries, i = [], 0
    seq = [str(t) for t in (tokens or [])]
    while i < len(seq):
        t = seq[i]
        n = 2 if (t in _VALUE_OPTS and i + 1 < len(seq)) else 1
        entries.append((_option_key(t), seq[i:i + n]))
        i += n
    last = {}
    for idx, (k, _v) in enumerate(entries):
        if k:
            last[k] = idx
    out = []
    for idx, (k, toks) in enumerate(entries):
        if k and last.get(k) != idx:
            continue
        out.extend(toks)
    return out


def build_command(game_dir, vid, instance_dir=None, java_exe="", memory_mb=0,
                  session=None, extra_jvm=None, game_cwd=None, item=None,
                  profile_properties=""):
    """拼出完整启动命令（不执行）。返回 dict：
        {argv, cwd, classpath, natives, java, version, main_class, online}
    干跑（dry run）与真正的 launch 都用它，方便测试与排查。

    `game_cwd`（第四十四轮）：版本隔离关掉时，工作目录要指向**游戏根目录**
    （共享存档 / 模组），但 natives 仍解在实例目录里 —— 两者分开传，
    免得 natives 被丢进游戏根目录污染玩家目录。
    `item`（第 104 轮）：当前游戏条目，用来取每实例自定义 JVM / 游戏参数、
    窗口设置；不传就读全局设置（默认值保持 1.7.3 的行为）。
    """
    game_dir = os.path.abspath(game_dir)
    data, chain = resolve_version(game_dir, vid)
    instance_dir = os.path.abspath(instance_dir or os.path.join(game_dir, "versions", vid))
    game_cwd = os.path.abspath(game_cwd or instance_dir)

    libs, native_jars = libraries(game_dir, data)
    cj = client_jar(game_dir, chain)
    if not cj:
        # 全链都没有客户端 jar：先试本机其它启动器 / 版本 json 自带的下载地址
        cj = ensure_client_jar(game_dir, data, chain)
    if not cj:
        raise LaunchError(_CLIENT_JAR_HINT.format(
            name=str(data.get("id") or vid), vid=str(vid)))
    libs.insert(0, cj)
    # 第 59 轮：natives jar **也要进 classpath**。
    # 新版 MC（1.21+ / 26.x）不再依赖「启动器把 dll 解到 java.library.path」，
    # 它自己的 NativeLibrariesBootstrap 是从 **classpath 里的 natives jar** 抽取
    # dll 的。以前这里只拼非 native 条目，于是 lwjgl 的 natives jar 压根没上
    # classpath → `Failed to locate library: lwjgl.dll` → 窗口不出现。
    classpath = os.pathsep.join(
        [p for p in (libs + native_jars) if os.path.isfile(p)])

    # 资源索引：缺了游戏会崩，提前说清楚
    idx = str((data.get("assetIndex") or {}).get("id") or data.get("assets") or "")
    assets_root = os.path.join(game_dir, "assets")
    if idx:
        ipath = os.path.join(assets_root, "indexes", idx + ".json")
        if not os.path.isfile(ipath):
            # 走到这里通常是 ensure_files 没补成（断网）。以前让人去找官方启动器，
            # 干净设备上根本没得找 —— 再试一次自己补，补不上才报错。
            try:
                from . import assetsync
                ok, note = assetsync.ensure(game_dir, data)
                if not ok:
                    raise LaunchError(note or f"缺少资源索引 {idx}.json")
            except LaunchError:
                raise
            except Exception as e:
                raise LaunchError(f"缺少资源索引 assets/indexes/{idx}.json（{e}）")

    natives_dir = os.path.join(instance_dir, "natives")
    info = auth_fields(session)
    # 第 104 轮 B5：窗口宽高 / 模式 / 标题不再写死 854x480。
    try:
        from . import launchprefs as _lp
        _win = _lp.window_settings(item or {})
        _opts = _lp.launcher_options(item or {})
    except Exception:                                  # noqa: BLE001
        _win = {"width": 854, "height": 480, "mode": "windowed", "title": ""}
        _opts = {}
    repl = {
        "auth_player_name": info["name"],
        "auth_uuid": info["uuid"],
        "auth_access_token": info["token"],
        "auth_session": f"token:{info['token']}:{info['uuid']}",
        "auth_xuid": info["xuid"],
        "clientid": info["client_id"],
        "user_type": info["user_type"],
        "user_properties": "{}",
        "version_name": str(data.get("id") or vid),
        "version_type": str(data.get("type") or "release"),
        "game_directory": game_cwd,
        # 第一百零五轮：照 PCL-CE `System.cs` L154-157 把「实例目录」和
        # 「隔离后的游戏根」分开暴露给自定义 JVM / 游戏参数。原来只有
        # game_directory 一个，玩家想指版本目录只能硬拼绝对路径，搬目录就失效。
        # 独立启动仍不读 launcher_profiles.json（第 96 轮铁律）。
        "instance_directory": os.path.abspath(instance_dir or game_cwd),
        "game_root_directory": os.path.abspath(game_dir or game_cwd),
        "assets_root": assets_root,
        "assets_index_name": idx or "legacy",
        "natives_directory": natives_dir,
        "classpath": classpath,
        "classpath_separator": os.pathsep,
        "library_directory": os.path.join(game_dir, "libraries"),
        "launcher_name": APP_NAME,
        "launcher_version": APP_VER,
        "resolution_width": str(_win.get("width") or 854),
        "resolution_height": str(_win.get("height") or 480),
    }

    # Java 提前选好：内存参数要按**实际用的那个 java.exe** 判断 32 位上限。
    if not java_exe:
        java_exe = pick_java(data, str(data.get("id") or vid))

    jvm = []
    log_cfg = ((data.get("logging") or {}).get("client") or {})
    log_arg = str(log_cfg.get("argument") or "")
    log_file = str(((log_cfg.get("file") or {}).get("id")) or "")
    for raw in _flatten((data.get("arguments") or {}).get("jvm")):
        if "${path}" in raw:
            # log4j 配置：本地没有就跳过这条，别拼出一个不存在的路径
            if not log_arg or not log_file:
                continue
            p = os.path.join(assets_root, "log_configs", log_file)
            if not os.path.isfile(p):
                continue
            jvm.append(raw.replace("${path}", p))
            continue
        jvm.append(_sub(raw, repl))

    # 第六十八轮：第三方登录（authlib-injector 外置登录）。
    # 不注入这一条，游戏只会拿「名字」当离线身份，皮肤/登录态全是我们这边的，
    # 面板服那边就是「验证失败」。jar 由 authlib.ensure_jar() 在 launch() 提前下好。
    if info.get("thirdparty"):
        from . import authlib as _ali
        jar = _ali.jar_path()
        if not os.path.isfile(jar):
            raise LaunchError(
                "第三方登录组件 authlib-injector.jar 还没下载好，"
                "请在「设置 → 账户」里重新登录一次第三方账号（或检查网络）")
        jvm.append(f"-javaagent:{jar}={info['server']}")

    if not any(a in ("-cp", "-classpath", "--class-path") for a in jvm):
        jvm += ["-cp", classpath]
    # 老版本（1.12.2 这类只有 minecraftArguments、没有 arguments.jvm）的 json 里
    # 根本不会给 java.library.path —— 官方启动器是启动器自己加的。少了这一条，
    # LWJGL 直接 `UnsatisfiedLinkError: no lwjgl64 in java.library.path` 秒退，
    # 表现就是「点了启动，游戏窗口不出现」。所以缺了就补一条。
    if not any(a.startswith("-Djava.library.path") for a in jvm):
        jvm.append(f"-Djava.library.path={natives_dir}")
    # C 块（第 104 轮）：堆参数统一由 ram.heap_args 生成 —— 32 位 Java 封顶
    # 1 GB、-Xmn=15% Xmx、LockMemory(Xms=Xmx)、独立可调初始堆。
    # auto_alloc 算法本身与 4 GB 下限一行没动（第 67 轮用户硬要求）。
    cap_note = ""
    if memory_mb:
        try:
            from . import launchprefs as _lp
            from . import ram as _ram
            _ro = _lp.ram_options(item or {})
            _heap, cap_note = _ram.heap_args(
                memory_mb, lock_memory=_ro["lock"], init_mb=_ro["init"],
                auto_min=_ro["auto_min"], xmn=_ro["xmn"],
                java_exe=java_exe if _ro["cap32"] else "")
            jvm += _heap
        except Exception as e:                         # noqa: BLE001
            crashlog.note("堆参数生成跳过：%s" % e)
            jvm.append(f"-Xmx{int(memory_mb)}M")
    # B2 默认优化参数串（可关，默认关 = 不硬加）+ B3 全局/每实例自定义 JVM
    try:
        from . import launchprefs as _lp
        jvm += _lp.default_jvm_opt_args()
        jvm += _lp.custom_jvm(item or {})
    except Exception as e:                             # noqa: BLE001
        crashlog.note("自定义 JVM 参数跳过：%s" % e)
    # 系统代理（第八十轮）：不传给 JVM，游戏查皮肤走 sessionserver / api.minecraftservices
    # 时就是直连 —— 国内直连超时 → 游戏里皮肤变回默认，日志只留一行
    # `Couldn't look up profile properties … Read timed out`。Java 不读系统代理，
    # 只能由启动器翻译成 -D 参数（详见 core/sysproxy.py）。
    try:
        from . import sysproxy
        jvm += sysproxy.jvm_args()
    except Exception as e:                             # noqa: BLE001
        crashlog.note("代理参数跳过：%s" % e)
    for a in (extra_jvm or []):
        jvm.append(str(a))
    # B6：IPv4 / IPv6 优先（真接到 JVM 上，不是只存个设置）
    _stack = str((_opts or {}).get("ip_stack") or "auto")
    if _stack == "ipv4":
        jvm.append("-Djava.net.preferIPv4Stack=true")
    elif _stack == "ipv6":
        jvm += ["-Djava.net.preferIPv6Addresses=true",
                "-Djava.net.preferIPv4Stack=false"]
    # B1：开放自定义参数的前置条件 —— 按「选项」去重，重复 -Xms/-cp/--gameDir
    # 不再触发 MultipleArgumentsForOptionException。
    jvm = dedup_jvm_args(jvm)

    game = _flatten((data.get("arguments") or {}).get("game"))
    if not game:
        legacy = str(data.get("minecraftArguments") or "")
        game = legacy.split() if legacy else []
    game = [_sub(a, repl) for a in game]
    # B5 / B6：全屏、自定义标题（走合法的 --versionType，不拼 MC 不认的
    # --title）、自动进服务器、全局/每实例自定义游戏参数。
    try:
        from . import launchprefs as _lp
        if _win.get("mode") == "fullscreen":
            game += ["--fullscreen"]
        if _win.get("title"):
            game += ["--versionType", str(_win["title"])]
        _sv = str((_opts or {}).get("server") or "").strip()
        if _sv:
            game += ["--server", _sv.split(":", 1)[0]]
            if ":" in _sv:
                try:
                    game += ["--port", str(int(_sv.rsplit(":", 1)[1]))]
                except ValueError:
                    pass
        game += _lp.custom_game(item or {})
    except Exception as e:                             # noqa: BLE001
        crashlog.note("自定义游戏参数跳过：%s" % e)
    # 第 121 轮：微软正版皮肤照官方方式直接喂给游戏 —— 游戏拿到现成的
    # --profileProperties 就不再直连被 403/429 限流的 sessionserver。
    # 只有 launch 判定「微软正版 + 1.20.2+ + 预取成功」时才传非空值；
    # dry run / 离线 / 老版本一律空串，参数完全不出现，保持原行为。
    if profile_properties:
        game += ["--profileProperties", str(profile_properties)]
    game = dedup_jvm_args(game)

    main_class = str(data.get("mainClass") or "")
    if not main_class:
        raise LaunchError("版本 json 里没有 mainClass，无法启动（文件可能损坏）")

    argv = [java_exe] + jvm
    # -cp/-classpath 必须在主类之前；jvm 里已经带了就不重复追加
    argv += [main_class] + game
    return {"argv": argv, "cwd": game_cwd, "instance": instance_dir,
            "classpath": classpath,
            "natives": native_jars, "java": java_exe,
            # 新版 MC 的 natives 子目录（java/lwjgl/jna/netty）；老版本为空
            "natives_subs": natives_subdirs(data),
            "version": str(data.get("id") or vid), "main_class": main_class,
            "online": info["online"], "auth_note": info.get("note", ""),
            "mc": str(data.get("inheritsFrom") or vid),
            "cap_note": cap_note, "window": _win, "options": _opts,
            "chain": chain, "data": data}


def pick_java(data, mc=""):
    """按版本 json 的 javaVersion.majorVersion 找 JDK，找不到再按 MC 版本猜。

    顺序：本软件装的 Java（精确主版本）→ 系统里≥该版本的 java.exe → Java 8 兜底。
    注意 `javamanager.find_installed()` 返回的是 **JAVA_HOME 目录**，不是 exe。
    """
    major = int(((data or {}).get("javaVersion") or {}).get("majorVersion") or 0)
    if not major:
        major = guess_java_major(mc or str((data or {}).get("id") or ""))
    if major:
        home = javamanager.find_installed(major)
        exe = os.path.join(home, "bin", "java.exe") if home else ""
        if exe and os.path.isfile(exe):
            return exe
        from . import loader
        try:
            exe = loader.find_java(major)
        except Exception:
            exe = ""
        if exe:
            return exe
    raise LaunchError(
        f"没有找到 Java {major or '?'}。到主页的「Java 下载」里装一个 "
        f"Java {major or 17}，或在设置里配置好 JAVA_HOME 后再试。")


def guess_java_major(mc):
    """MC 版本 → 需要的 Java 主版本（1.18~1.20.4 要 17，1.20.5+ / 1.21+ 要 21）"""
    v = str(mc or "")
    m = v.replace("_", ".").split("-")[0]
    parts = [p for p in m.split(".") if p.isdigit()]
    if not parts:
        return 17
    major = int(parts[0])
    minor = int(parts[1]) if len(parts) > 1 else 0
    patch = int(parts[2]) if len(parts) > 2 else 0
    if major >= 24:                      # 年份命名（26.3 …）都是新版本
        return 21
    if major > 1 or minor >= 21:
        return 21
    if minor == 20 and patch >= 5:
        return 21
    if minor >= 18:
        return 17
    if minor == 17:
        return 16
    return 8


# ---------------------------------------------------------------- 目标
def standalone_enabled():
    return bool(CONFIG.get("standalone_launch"))


def launch_mode():
    """当前启动方式：official / standalone（开关关掉时永远 official）"""
    if not standalone_enabled():
        return "official"
    return "standalone" if CONFIG.get("launch_mode") == "standalone" else "official"


def set_launch_mode(mode):
    CONFIG.set("launch_mode", "standalone" if mode == "standalone" else "official")


def launch_target():
    """已选的独立启动目标（失效则返回 {}) """
    t = CONFIG.get("launch_target")
    return t if isinstance(t, dict) and t.get("version_id") else {}


def set_launch_target(kind, version_id, path, name, memory_mb=0, game_dir=""):
    """记下独立启动目标。

    `game_dir` 是那个版本所属的 .minecraft（可以是别的启动器的根目录）：
    「导入已有文件夹」直接指向原目录时，库/资源/版本 json 都在那边，
    零拷贝也能启动 —— 这时候不能拿我们自己的游戏目录去解析版本。
    """
    CONFIG.set("launch_target", {"kind": kind, "version_id": str(version_id),
                                 "path": str(path), "name": str(name),
                                 "memory_mb": int(memory_mb or 0),
                                 "game_dir": str(game_dir or "")})


def target_game_dir(t=None):
    """目标版本所属的游戏目录（目标自带优先，否则用本机官启目录）"""
    t = t if t is not None else launch_target()
    gd = str((t or {}).get("game_dir") or "")
    return gd if gd and os.path.isdir(gd) else _game_dir()


def target_ready(game_dir=None):
    """目标是否真的能启动（版本 json 还在吗）"""
    t = launch_target()
    if not t:
        return False, ""
    gd = game_dir or target_game_dir(t)
    vid = target_version_id(t, gd)
    if not os.path.isfile(version_json_path(gd, vid)):
        # 报错要说清「缺什么」：加载器没装上是玩家唯一能动手的那件事
        meta = _instance_meta(t.get("path") or "")
        cn = LOADER_NAMES.get(str(meta.get("loader") or ""), "")
        lver = str(meta.get("loader_ver") or "")
        if cn:
            return False, (f"「{t.get('name') or vid}」还缺 {cn} {lver} 的运行版本 —— "
                           f"装好它就能独立启动（不用官方启动器）")
        return False, (f"「{t.get('name') or vid}」的运行版本不见了，"
                       f"请重新在游戏管理里选一次")
    return True, ""


def _instance_meta(path):
    """实例目录里的 `.mcassist.json`（没有就返回 {}）"""
    p = os.path.join(str(path or ""), ".mcassist.json")
    if not str(path or "") or not os.path.isfile(p):
        return {}
    d = _read_json(p)
    return d if isinstance(d, dict) else {}


def target_version_id(t=None, game_dir=None):
    """目标真正该拿去解析的版本 id。

    `t["version_id"]` 记的是**实例名**（PCL2 的版本隔离语义：实例目录就是
    `versions/<实例名>`）。官启语义要求 `versions/<实例名>/<实例名>.json` 存在，
    但那个壳在两种情况下没有：加载器当时没装上、或实例是别的启动器/老版本留下的。
    这时退到 `.mcassist.json` 里记的 `base_version`（真正装好的加载器版本，
    如 `1.20.1-forge-47.4.10`）—— 独立启动照样能把游戏拉起来，
    mods 也照旧读实例目录，一个字节都不复制（第九十六轮）。
    """
    t = t if t is not None else launch_target()
    if not t:
        return ""
    gd = game_dir or target_game_dir(t)
    inst = str(t.get("path") or "")
    want = str(t.get("version_id") or "")
    meta = _instance_meta(inst)
    cands = [want]
    for k in ("version_id", "base_version"):
        v = str(meta.get(k) or "")
        if v and v not in cands:
            cands.append(v)
    for v in cands:
        if v and os.path.isfile(version_json_path(gd, v)):
            return v
    return want


def target_instance_dir(t=None) -> str:
    """已选游戏的**实例目录**（没选 / 目录没了 → 空串）。

    第五十七轮：装模组 / 数据包 / 光影 / 材质包时要「按所选游戏分配」——
    玩家在「选择要启动的游戏」里点了哪个，内容就落进那个游戏的文件夹
    （`<实例>/mods` 等），而不是一律丢进官方启动器的根目录。

    只有实例（整合包 / 导入的目录）才有「自己的文件夹」；纯版本（原版、
    加载器版本）内容本来就该在游戏根目录，返回空串让调用方退回老行为。
    """
    t = t if t is not None else launch_target()
    if not t:
        return ""
    inst = str(t.get("path") or "")
    if inst and os.path.isdir(inst):
        return inst
    # 老配置 / 导入登记可能只存了 version_id：按标准位置补一次
    vid = str(t.get("version_id") or "")
    if vid:
        cand = os.path.join(target_game_dir(t), "versions", vid)
        if os.path.isdir(cand):
            return cand
    return ""


def target_label(t=None) -> str:
    """已选游戏的显示名（给「装到哪个游戏」这类提示用）"""
    t = t if t is not None else launch_target()
    if not t:
        return ""
    return str(t.get("name") or t.get("version_id") or "")


def _version_dir_of(t):
    """目标版本真正的 versions/<id> 目录（实例目录里优先找一层）"""
    vid = str((t or {}).get("version_id") or "")
    gd = target_game_dir(t)
    inst = target_instance_dir(t)
    cands = []
    if inst and vid:
        cands.append(os.path.join(inst, "versions", vid))
    if gd and vid:
        cands.append(os.path.join(gd, "versions", vid))
    if inst:
        cands.append(inst)
    for c in cands:
        try:
            if c and os.path.isdir(c) and any(
                    f.lower().endswith(".json") for f in os.listdir(c)):
                return c
        except OSError:
            continue
    return cands[0] if cands else ""


def _mc_number(mc):
    """「1.21.1」→ (1, 21, 1)；年份版（26.3 / 26.4-snapshot-5）→ (26, 3)；读不出返回 ()"""
    s = str(mc or "").strip()
    if not s:
        return ()
    out = []
    for part in s.replace("_", ".").replace("-", ".").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            break
        out.append(int(digits))
        if len(out) >= 3:
            break
    return tuple(out)


def datapack_supported(mc):
    """这个 MC 版本有没有「数据包」这个玩法（1.13 才有 datapacks）。

    返回 True / False；版本号读不出来时返回 True —— 判不了就别拦，
    宁可让玩家装错一次（数据包装错只是不生效），也不要莫名其妙禁掉一整页。
    """
    num = _mc_number(mc)
    if not num:
        return True
    if num[0] >= 13:                 # 年份命名（26.x）走这条：肯定支持
        return True
    return num >= (1, 13)


# 加载器 → 中文（下载页/详情页的提示里用）
LOADER_NAMES = {"forge": "Forge", "neoforge": "NeoForge", "fabric": "Fabric",
                "quilt": "Quilt", "liteloader": "LiteLoader", "rift": "Rift"}


def target_caps(t=None):
    """所选游戏能装什么（第六十一轮）。

    原版（没有 Forge/NeoForge/Fabric/Quilt）跑不了模组也跑不了光影 ——
    这两类装进去只会让游戏在启动时报「缺依赖」或者干脆无视；
    材质包原版就支持；数据包从 1.13 起才有，更老的版本不能下。

    返回 {"label","mc","loader","vanilla","ok":{kind: bool},"why":{kind: str}}
    kind ∈ mod / datapack / shader / resourcepack（modpack 永远可以）
    没选游戏时 `vanilla` 为 False、全部允许 —— 那时候内容装到官方目录，
    跟「所选游戏是不是原版」无关，不该在这里拦。
    """
    prof = target_profile(t)
    kinds = ("mod", "datapack", "shader", "resourcepack")
    ok = {k: True for k in kinds}
    why = {k: "" for k in kinds}
    if not prof:
        return {"label": "", "mc": "", "loader": "", "vanilla": False,
                "ok": ok, "why": why}
    loader = str(prof.get("loader") or "").lower()
    mc = str(prof.get("mc") or "")
    if not mc:
        # 版本号都读不出来（json 被删 / 导入的目录格式怪）→ 判不了是不是原版。
        # 这时候**不能拦**：一拦就是整页按钮全灰，玩家连"下到官方目录"都做不了。
        return {"label": prof.get("label") or "", "mc": "", "loader": loader,
                "vanilla": False, "optifine": False, "ok": ok, "why": why}
    vanilla = not loader
    # 「OptiFine / Iris 独立版」也是原版壳子里塞一个 jar，describe_version 认不出加载器，
    # 但它们**能加载光影** —— 名字里带了就不该把光影也禁掉。
    tag = (str(prof.get("version_id") or "") + " "
           + str(prof.get("label") or "")).lower()
    optifine_like = ("optifine" in tag) or ("iris" in tag)
    if vanilla:
        ok["mod"] = False
        why["mod"] = "原版没有装加载器（Forge / NeoForge / Fabric），模组装进去用不了"
        if not optifine_like:
            ok["shader"] = False
            why["shader"] = "原版没有装加载器 —— 光影要靠 Iris / OptiFine 才能加载"
    if not datapack_supported(mc):
        ok["datapack"] = False
        why["datapack"] = "MC %s 还没有「数据包」这个玩法（1.13 起才有）" % mc
    return {"label": prof.get("label") or "", "mc": mc, "loader": loader,
            "vanilla": vanilla, "optifine": optifine_like,
            "ok": ok, "why": why}


def caps_note(kind, caps):
    """给「不可下载」的按钮配一句话；能用返回空串"""
    caps = caps or {}
    got = (caps.get("ok") or {})
    if got.get(kind, True):
        return ""
    return str((caps.get("why") or {}).get(kind) or "当前所选游戏装不了这个")


def target_profile(t=None):
    """已选游戏「是什么版本」—— 下载内容前判断版本匹配用（第六十轮）。

    返回 {"label", "instance", "version_id", "mc", "loader", "loader_ver"}；
    没选游戏 / 版本 json 读不到时，mc 与 loader 留空（调用方据此显示「未知」）。
    """
    t = t if t is not None else launch_target()
    if not t:
        return {}
    from . import runtime
    vdir = _version_dir_of(t)
    desc = {}
    try:
        desc = runtime.describe_version(vdir) or {}
    except Exception:                                  # noqa: BLE001
        desc = {}
    return {"label": target_label(t),
            "instance": target_instance_dir(t),
            "version_id": str(t.get("version_id") or ""),
            "mc": str(desc.get("mc") or ""),
            "loader": str(desc.get("loader") or ""),
            "loader_ver": str(desc.get("loader_ver") or "")}


def install_targets(limit=10):
    """下载内容时可以「装到哪儿」的候选（第六十轮：版本不匹配时可手动改）。

    第一条永远是「当前所选游戏」（有的话），最后一条是「官方启动器目录」。
    返回 [{"key", "label", "note", "instance", "version_id", "mc", "loader",
           "loader_ver", "current"}]
    """
    out = []
    seen = set()

    def add(item, note="", current=False):
        key = os.path.normcase(item.get("instance") or "") or "official"
        if key in seen:
            return
        seen.add(key)
        out.append({"key": key, "label": item.get("label") or "未命名",
                    "note": note, "instance": item.get("instance") or "",
                    "version_id": item.get("version_id") or "",
                    "mc": item.get("mc") or "", "loader": item.get("loader") or "",
                    "loader_ver": item.get("loader_ver") or "",
                    "current": bool(current)})

    cur = target_profile()
    if cur:
        add(cur, "当前所选 · " + _ver_text(cur), True)
    try:
        from . import installer
        for it in installer.list_game_instances() or []:
            if len(out) >= max(1, limit - 1):
                break
            add({"label": it.get("name") or os.path.basename(it.get("path") or ""),
                 "instance": it.get("path") or "",
                 "version_id": it.get("version_id") or "",
                 "mc": it.get("mc") or "", "loader": it.get("loader") or "",
                 "loader_ver": it.get("loader_ver") or ""},
                "已安装的实例 · " + _ver_text(it))
    except Exception:                                  # noqa: BLE001
        pass
    if "official" not in seen:
        add({"label": "官方启动器目录", "instance": ""},
            "不分游戏，装进游戏根目录下的内容文件夹", not cur)
    return out


def _ver_text(d):
    """"1.21.1 · NeoForge 21.1.264" 这样的一句话（缺的项自动省略）"""
    mc = str((d or {}).get("mc") or "")
    ld = str((d or {}).get("loader") or "")
    lv = str((d or {}).get("loader_ver") or "")
    cn = {"forge": "Forge", "neoforge": "NeoForge", "fabric": "Fabric",
          "quilt": "Quilt", "liteloader": "LiteLoader"}.get(ld, ld)
    bits = []
    if mc:
        bits.append("MC " + mc)
    if cn:
        bits.append(cn + (" " + lv if lv else ""))
    return " · ".join(bits) or "版本未知"


def _game_dir():
    from . import installer
    return installer.detect_game_dir()


def is_running():
    p = _RUN.get("proc")
    return bool(p) and p.poll() is None


def pid() -> int:
    """独立启动的游戏进程 pid（没在跑返回 0）。

    悬浮球上那条加载进度就靠它找游戏窗口（core/winwatch.py）。
    """
    p = _RUN.get("proc")
    try:
        return int(p.pid) if (p is not None and p.poll() is None) else 0
    except Exception:
        return 0


def running_name():
    return _RUN.get("name") or ""


def kill_running(wait: float = 6.0) -> bool:
    """结束独立启动的游戏进程（第六十八轮）。

    为什么要我们动手结束它：加载器缺前置时会自己弹一屏「加载失败」，进程就那么
    挂着等玩家点退出。那一屏是**英文/一长串原文**，玩家看完还是不知道该干嘛；
    而我们已经把原因、缺什么、能不能自动补全塞进启动器自己的弹窗里了。
    所以发现「卡在加载失败」就直接关掉它 —— 报错内容我们已经读进弹窗，
    加载器那屏不必再留着（用户明确要求：自带的报错提示不用再显示）。

    返回 True 表示进程原本在跑、已请求结束。
    """
    p = _RUN.get("proc")
    try:
        if p is None or p.poll() is not None:
            return False
    except Exception:                                  # noqa: BLE001
        return False
    try:
        p.terminate()                  # Windows 上是 TerminateProcess
    except Exception:                                  # noqa: BLE001
        pass
    t0 = time.time()
    while time.time() - t0 < max(0.5, float(wait or 0)):
        try:
            if p.poll() is not None:
                return True
        except Exception:                              # noqa: BLE001
            return True
        time.sleep(0.12)
    try:
        p.kill()
    except Exception:                                  # noqa: BLE001
        pass
    return True


def exit_report():
    """游戏退出后给玩家的说明。

    启动后几十秒内就退出的，八成是崩了（缺库 / natives 架构 / 参数错），
    这时把日志里最像错误的那行带上 —— 否则玩家只看到「已退出」，无从下手。
    """
    name = _RUN.get("name") or ""
    started = float(_RUN.get("started") or 0)
    lived = (time.time() - started) if started else 0
    if name and lived < 40:
        err = _error_line(_RUN.get("log") or "")
        if err:
            return f"「{name}」启动失败（{err}）"
    return f"「{name}」已退出" if name else "游戏已退出"


def log_path():
    return _RUN.get("log") or ""


def run_cwd() -> str:
    """当前（或上一次）这次启动的**游戏工作目录** —— 游戏的 logs/ 就在它下面。

    取 `cmd["cwd"]`：那是真正传给 Popen 的目录，不是「设置里的游戏目录」
    （两者在整合包实例上完全不是一回事，差一个就永远找不到 latest.log）。
    """
    cmd = _RUN.get("cmd") or {}
    for k in ("cwd", "game_directory", "instance"):
        v = str(cmd.get(k) or "")
        if v and os.path.isdir(v):
            return v
    return _game_dir()


def live_missing() -> list:
    """游戏**还在跑**时缺哪些前置（第五十八轮）。

    卡在「加载失败」界面时进程是活的，`exit_info()` 永远等不到退出，
    主界面靠这个每 3 秒问一次。返回 `[{id, by, range, actual}]`，没问题就是空表。
    """
    from . import crashdiag
    cwd = run_cwd()
    started = float(_RUN.get("started") or 0)
    if not cwd:
        return []
    try:
        return crashdiag.live_missing(cwd, started)
    except Exception:                                  # noqa: BLE001
        return []


def crash_report(cwd: str = "", since: float = 0) -> str:
    """最新那份 crash-reports/crash-*.txt（比 since 旧的不要）。

    Forge/NeoForge/Fabric 崩了都会往 `crash-reports` 里写一份带 Description /
    Caused by 的报告，比 stdout 日志干净得多 —— 弹窗里「崩溃原因」优先读它。
    """
    d = os.path.join(str(cwd or ""), "crash-reports")
    try:
        names = [n for n in os.listdir(d) if n.lower().endswith(".txt")]
    except OSError:
        return ""
    best, best_t = "", 0.0
    for n in names:
        p = os.path.join(d, n)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if since and mt < since - 5:
            continue
        if mt >= best_t:
            best, best_t = p, mt
    return best


def exit_info() -> dict:
    """游戏退出后的全部现场（第五十五轮崩溃弹窗用）。

    `crashed` 的判定宁宽勿窄：非 0 退出码，或者活了不到 40 秒 —— 玩家点「退出
    游戏」是 0，正常关窗口也基本是 0；剩下那些就是崩了，值得把原因摊开给他看。
    """
    p = _RUN.get("proc")
    code = None
    try:
        code = None if p is None else p.returncode
    except Exception:
        code = None
    started = float(_RUN.get("started") or 0)
    lived = (time.time() - started) if started else 0.0
    cmd = _RUN.get("cmd") or {}
    cwd = str(cmd.get("cwd") or "")
    # 第五十八轮：卡在「缺前置」界面再点叉关掉的情况，退出码是 0、存活也不短，
    # 只看退出码会把这次失败判成「玩家自己关的游戏」。日志里有证据就算崩。
    dep_missing = []
    try:
        from . import crashdiag
        if cwd:
            dep_missing = crashdiag.live_missing(cwd, started)
    except Exception:                              # noqa: BLE001
        dep_missing = []
    crashed = ((code not in (0, None)) or (bool(started) and lived < 40)
               or bool(dep_missing))
    return {
        "name": _RUN.get("name") or cmd.get("version") or "",
        "code": code,
        "lived": lived,
        "log": _RUN.get("log") or "",
        "cwd": cwd,
        "instance": str(cmd.get("instance") or ""),
        "game_dir": _game_dir() if not cwd else cwd,
        "mc": str(cmd.get("mc") or ""),
        "version": str(cmd.get("version") or ""),
        "java": str(cmd.get("java") or ""),
        "memory_mb": int(_RUN.get("memory_mb") or 0),
        "crashed": bool(crashed),
        "dep_missing": dep_missing,
        "report": crash_report(cwd, started),
    }


# ---------------------------------------------------------------- 启动
_TOKEN_CACHE = {}          # access_token -> (是否有效, 探测时间)


def _mc_token_alive(token) -> bool | None:
    """微软的 Minecraft access_token 还有效吗？

    `True` 有效（200）· `False` 明确失效（401）· `None` 说不准（断网 / 超时）

    为什么要探这一下：token 只有 24 小时，以前只在「压根没有 token」时才续期，
    过期的那种照样塞给游戏。游戏拿它去 `api.minecraftservices.com` 领权益，
    拿到 401 → 日志里 `Failed to fetch user properties`，皮肤 / 披风也跟着查不到，
    进世界就是默认皮肤（第八十轮玩家报的「正版号却显示离线皮肤」）。
    三条路都要试（系统代理 / 环境变量 / 直连），只用 requests 的默认行为会被
    「Clash 只写注册表」坑一次，结果是「说不准」，等于白探。
    """
    if not str(token or "").strip():
        return False
    now = time.time()
    hit = _TOKEN_CACHE.get(token)
    # 第 119 轮：5 分钟太短，连开两个游戏就会重复探、白耗 Mojang 的限流额度
    # （限流是按出口 IP 算的，跟用哪个 token 无关）。token 有 24 小时有效期，
    # 15 分钟内探一次足够，且下面对 401/200 都写缓存，不确定的才留到下次。
    if hit and now - hit[1] < 900:                     # 15 分钟内不重复探同一个令牌
        return hit[0]
    from . import bbsmc, msa
    verdict = None
    throttled = False
    for _label, px in bbsmc._proxy_candidates():
        try:
            r = requests.get(msa.MC_PROFILE_URL,
                             headers={"Authorization": "Bearer " + token,
                                      "User-Agent": msa.UA},
                             proxies=px, timeout=(3.5, 5))
        except Exception:                              # noqa: BLE001
            continue
        if r.status_code == 200:
            verdict = True
            break
        if r.status_code == 401:
            verdict = False
            break
        if r.status_code == 429:
            # 429 = 限流，**不是令牌失效**。token 明明还剩 22 小时，若返回
            # None，`_session_with_token` 会误判成「说不准」去续期 —— 那又是
            # 一次 Mojang 请求，把限流雪上加霜。这里按「令牌有效」处理，
            # 继续换候选（直连可能没被那个代理出口连累），绝不触发续期。
            throttled = True
            verdict = True
            continue
        # 其它 5xx / 异常状态：换候选再试，最后按「说不准」处理
    if verdict is not None:                            # 说不准的别缓存，下次再探
        _TOKEN_CACHE[token] = (verdict, now)
    if throttled:
        crashlog.note("Mojang 令牌探活遇到 429 限流：按令牌仍有效处理，未触发续期")
    return verdict


# 第 119 轮：游戏内的皮肤查询是 authlib 一次性的，撞 429 就放弃、直接回退
# 默认皮肤，**它不会重试**。游戏 jar 里的 authlib 我们改不了，但可以在启动
# 进程之前，用带退避重试的请求先把 profile 探通：一是提前把限流窗口耗过去，
# 二是 200 的结果会进 `_TOKEN_CACHE`，游戏启动时那一下探活直接命中缓存、
# 不再多发一次 Mojang 请求 —— 两处一起把限流额度省下来。
_PROFILE_CACHE = {}        # uuid -> (textures 是否可用, 探测时间)


def _prefetch_skin_profile(session: dict, progress=None) -> bool:
    """启动进程前预取皮肤 profile；429 时退避重试。失败不阻断启动。

    用 sessionserver 那个带 uuid 的端点（游戏查皮肤走的同一个）：
    正常返回 200 就把可用标记缓存 10 分钟，游戏紧接着查时属于同一窗口，
    大幅降低撞限流的概率。只用直连候选里最可能通的一条，不铺开三条 ——
    预取本身就是为了少发请求，铺开等于自己制造限流。
    """
    s = session or {}
    uid = str(s.get("uuid") or "").replace("-", "")
    token = str(s.get("mc_token") or "")
    if not uid or not token or str(s.get("mode") or "") != "microsoft":
        return False
    now = time.time()
    hit = _PROFILE_CACHE.get(uid)
    if hit and now - hit[1] < 600:
        return hit[0]
    from . import bbsmc, msa
    url = ("https://sessionserver.mojang.com/session/minecraft/profile/%s"
           "?unsigned=false" % uid)
    ok, saw_429 = False, False
    for attempt in range(3):                           # 最多 3 次，退避 0/1.5/3 秒
        for _label, px in bbsmc._proxy_candidates():
            try:
                r = requests.get(url, headers={"User-Agent": msa.UA},
                                 proxies=px, timeout=(3.5, 5))
            except Exception:                          # noqa: BLE001
                continue
            if r.status_code == 200:
                ok = True
                break
            if r.status_code == 429:
                saw_429 = True
                continue                               # 换候选，别在这里就放弃
            # 401/其它：这个 token 查 profile 不该 401，交回正常逻辑处理
            break
        if ok:
            break
        if attempt < 2 and saw_429:
            if progress:
                progress("皮肤查询被限流，稍后重试…")
            time.sleep(1.5 * attempt + 1.5)
    if ok:
        _PROFILE_CACHE[uid] = (True, now)
        # 同步喂给令牌探活缓存：预取已证明 token 活着，游戏启动那次探活
        # 直接命中，不再多发一次 api.minecraftservices.com 请求。
        _TOKEN_CACHE[token] = (True, now)
    else:
        crashlog.note("启动前预取皮肤 profile 未完成（限流或端点不可达），"
                      "不阻断启动；本次游戏内皮肤可能回退默认")
    return ok


def _session_with_token():
    """拿到带 Minecraft access_token 的会话（过期就悄悄续一次）。

    续不上不算错：独立启动会退到离线模式，单人/局域网照样能玩，
    只是进不了正版验证的服务器 —— 这一点会在提示里写明。
    """
    s = dict(CONFIG.session() or {})
    if str(s.get("mode") or "") != "microsoft":
        return s
    token = str(s.get("mc_token") or "")
    alive = _mc_token_alive(token) if token else False
    if alive is True:
        return s
    ref = str(s.get("msa_refresh") or "")
    if ref:
        try:
            from . import msa
            # 二元组调用（兼容旧测试桩）；第 118 轮 xuid/client_id 由模块级
            # _last_refresh_extra 带出，老 session 的空 XUID 借此补回。
            tok, newr = msa.refresh_mc_token(ref)
            extra = getattr(msa, "_last_refresh_extra", {}) or {}
            s = dict(s, mc_token=tok, msa_refresh=newr)
            if extra.get("xuid") and not str(s.get("xuid") or ""):
                s["xuid"] = extra["xuid"]            # 老 session 的空 XUID 补上
            if extra.get("client_id"):
                s["client_id"] = extra["client_id"]
            CONFIG.set("session", s)
            return s
        except Exception as e:                         # noqa: BLE001
            crashlog.note("令牌续期失败：%s" % e)
    if alive is False:
        # 明确失效又续不上：别再把死 token 塞给游戏。按离线启动说清楚，
        # 玩家至少知道为什么皮肤是默认的、进不去正版服。
        s = dict(s, mc_token="", _auth_note="登录已过期（续期失败），本次按离线启动；"
                                           "皮肤可能显示为默认，进不去正版服务器。"
                                           "到「设置 → 账户」重新登录一次即可")
        saved = dict(s)
        saved.pop("_auth_note", None)                  # 提示文案不进配置文件
        CONFIG.set("session", saved)
    return s


def _fix_loader(game_dir, chain, progress=None) -> str:
    """开局前确认加载器装全了；没装全就当场补一次。返回错误说明（空 = 没问题）。

    第九十九轮（玩家实测崩溃日志）：整合包装完打开游戏，10 秒退出码 1，日志里
    `Missing or unsupported mandatory dependencies` 把 `minecraft` / `neoforge`
    都标成 `[MISSING]`，最后死在
    `DisplayWindow.updateModuleReads → Optional.orElseThrow` 的
    `NoSuchElementException: No value present`。看着像模组冲突，其实是
    NeoForge 安装器没跑完 —— `libraries/` 里缺那几份安装器生成的 jar
    （判据见 runtime.loader_ready）。

    这里**不静默放过**：能补就补，补不上就拦住并说清楚，别让玩家对着
    「退出码 1」猜是哪个模组干的。

    第一百零二轮：以前在版本链**第一个** forge 节点就判、判不过就立刻
    `return` 错误。可 PCL2 / PCLCE 导入的整合包第一个节点是「实例壳」，
    它靠 `inheritsFrom` 继承 Forge、自己没记版本号，于是空版本 →「没装全」
    → 空版本补装必然失败 → 一直拦着不让开局，哪怕 `libraries` 里 jar 全齐、
    链上后面那个 `1.20.1-forge-47.4.10` 明明是好的。现在遍历完整条链、
    按 (kind, lver, mc) 去重，实例壳补不出版本号就跳过，只检查真正承载
    加载器的那一份。
    """
    from . import runtime as _rt
    checked, problems = set(), []
    for vid in (chain or []):
        vdir = os.path.join(game_dir, "versions", str(vid))
        try:
            info = _rt.describe_version(vdir) or {}
        except Exception:                                 # noqa: BLE001
            info = {}
        kind = str(info.get("loader") or "").lower()
        if kind not in ("forge", "neoforge"):
            continue
        lver = str(info.get("loader_ver") or "")
        mc = str(info.get("mc") or "")
        # 第一百零二轮：整合包实例壳本身不承载加载器，Forge 主坐标交给
        # inheritsFrom 继承。describe_version 已从 .mcassist.json / 基底 id /
        # fmlloader 补过一次；真补不出来的（外来包、版本信息残缺）就别拿空
        # 版本号判「没装全」，更别拿空版本号去补装 —— 空版本拼出来的
        # installer URL 必然 404，玩家重装多少次都过不去。链上后面那个
        # `<mc>-forge-<ver>` 才是真正要检查的那一份。
        if not lver:
            continue
        sig = (kind, lver, mc)
        if sig in checked:
            continue
        checked.add(sig)
        ok, why = _rt.loader_ready(game_dir, kind, lver, mc)
        if ok:
            continue                      # 别再「第一个 OK 就返回」：链上可能还有别的加载器
        cn = _rt.LOADER_CN.get(kind, kind)
        if progress:
            progress(f"{cn} {lver} 上次没装全，正在补装（要 1~3 分钟）…")
        try:
            from . import loader as _ld
            _ld.install_loader(mc, kind, lver, None, game_dir=game_dir)
        except Exception as e:                            # noqa: BLE001
            crashlog.note("补装加载器出错：%s" % e)
        ok2, why2 = _rt.loader_ready(game_dir, kind, lver, mc)
        if not ok2:
            problems.append(f"{cn} {lver}：{why2 or why}")
    if problems:
        # 不再写「请联网后」：这红字最常见的成因是版本信息没认出来，
        # 跟网络一点关系都没有，玩家反复重装只会白忙。
        return ("这个游戏的加载器没装全（" + "；".join(problems) + "），"
                "直接开会在 10 秒内崩掉。已经按版本号自动补装了一次仍没成功 ——"
                "到「设置 → 启动项」重新安装对应版本的加载器，"
                "或者删掉这个整合包重新导入一次。")
    return ""


def launch(game_dir=None, progress=None):
    """独立启动已选的目标，返回 (ok, 中文说明)。阻塞，请在后台线程调用。"""
    t = launch_target()
    if not t:
        return False, "还没选游戏：先到「游戏管理」里点一下要启动的那个游戏"
    # 目标可能指向别的启动器的目录（零拷贝添加进来的），优先用它的游戏目录
    gd = game_dir or target_game_dir(t)
    ok, why = target_ready(gd)
    if not ok:
        return False, why
    # vid 可能不是 t["version_id"]：实例壳没生成时退到 base_version（见 target_version_id）
    vid = target_version_id(t, gd)
    inst = t.get("path") or os.path.join(gd, "versions", vid)
    if not os.path.isdir(inst):
        inst = os.path.join(gd, "versions", vid)

    try:
        if progress:
            progress("整理启动参数…")
        data, _chain = resolve_version(gd, vid)
        # 加载器装全了吗：半成品会在开窗口那一瞬间崩（见 _fix_loader）
        _bad = _fix_loader(gd, _chain, progress)
        if _bad:
            return False, _bad
        if progress:
            progress("检查库与资源…")
        notes = ensure_files(gd, data, progress)      # 缺库/缺索引就地补

        # ---- 启动偏好（第四十四轮）：版本隔离 / 内存 / Java 都由 launchprefs 判 ----
        from . import launchprefs, runtime as _rt
        try:
            desc = _rt.describe_version(inst) or {}
        except Exception:
            desc = {}
        item = dict(desc, path=inst, version_id=vid, name=t.get("name") or vid)
        if not item.get("id"):
            item["id"] = vid
        if not item.get("type"):
            item["type"] = str(data.get("type") or "")
        iso = launchprefs.should_isolate(item)
        # 内存按**实际生效**的目录数模组：隔离时模组在实例目录里，
        # 关着时才在游戏根目录。传错一个层级，自动配置就永远只数到 0 个模组。
        mem, mem_why = launchprefs.memory_for(item, inst if iso else gd)
        java_exe = launchprefs.java_for(item)
        sess = _session_with_token()
        if str((sess or {}).get("mode") or "") == "thirdparty":
            from . import authlib as _ali
            try:
                _ali.ensure_jar(progress)
            except Exception as _e:
                return False, f"第三方登录组件下载失败：{_e}"
        # 第 121 轮：照官方方式注入皮肤。用官方同款 api 域名拿 profile、拼成
        # --profileProperties 喂给游戏，游戏就不必直连被限流的 sessionserver。
        # 只对微软正版 + 1.20.2+ 启用；失败/离线/老版本一律空串退回原行为。
        _skin_props = ""
        try:
            from . import skinprops
            _mc_ver = str((data.get("inheritsFrom") or data.get("id") or vid))
            if (str((sess or {}).get("mode") or "") == "microsoft"
                    and skinprops.needs_inject(_mc_ver)):
                _skin_props = skinprops.fetch_properties(sess)
                if _skin_props and progress:
                    progress("皮肤数据已就绪")
        except Exception as _e:                        # noqa: BLE001
            crashlog.note("皮肤 profileProperties 注入跳过：%s" % _e)
            _skin_props = ""
        cmd = build_command(gd, vid, instance_dir=inst,
                            java_exe=java_exe, memory_mb=mem,
                            session=sess, item=item,
                            game_cwd=(inst if iso else gd),
                            profile_properties=_skin_props)
        # B6：javaw / java 切换。javaw 没有控制台窗口，但日志仍由我们的
        # stdout 重定向写到 mcassist-launch.log，不影响崩溃诊断。
        try:
            if launchprefs.launcher_options(item).get("use_javaw"):
                _jw = os.path.join(os.path.dirname(cmd["java"]), "javaw.exe")
                if os.path.isfile(_jw):
                    cmd["java"] = _jw
                    cmd["argv"][0] = _jw
        except Exception:                              # noqa: BLE001
            pass
        if progress:
            progress("释放本地库…")
        extract_natives(cmd["natives"],
                        os.path.join(cmd.get("instance") or cmd["cwd"], "natives"),
                        subs=cmd.get("natives_subs"))
        # 实例目录还没有自己的 `<id>.jar` 时，把链上那份硬链接过来（同卷 0 成本）：
        # 官启按 lastVersionId 去 versions/<id>/<id>.jar 找客户端 jar，缺了就不启动。
        try:
            from . import installer as _inst
            _inst.mirror_base_jar(gd, vid)
        except Exception:
            pass
    except LaunchError as e:
        return False, str(e)
    except Exception as e:
        return False, f"准备启动失败：{e}"

    logs = os.path.join(cmd["cwd"], "logs")
    try:
        os.makedirs(logs, exist_ok=True)
    except Exception:
        logs = cmd["cwd"]
    logf = os.path.join(logs, "mcassist-launch.log")
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # B4（第 104 轮）：预启动命令，全局 + 每实例可覆盖，可等待完成。
    # 玩家自己写的命令可能挂住，所以**绝不用 subprocess.run(timeout=1800)**
    # （第 98 轮铁律），改 Popen + 0.2s 轮询 + 到点 kill，总时限 120 秒。
    pre_note = ""
    try:
        pre_cmd, pre_wait = launchprefs.pre_launch(item)
    except Exception:                                  # noqa: BLE001
        pre_cmd, pre_wait = "", True
    if pre_cmd:
        try:
            _p = subprocess.Popen(pre_cmd, shell=True, cwd=cmd["cwd"],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.STDOUT, creationflags=flags)
            if pre_wait:
                _deadline = time.monotonic() + 120.0
                while _p.poll() is None and time.monotonic() < _deadline:
                    time.sleep(0.2)
                if _p.poll() is None:
                    _p.kill()
                    pre_note = "；预启动命令超时（120 秒）已终止"
                else:
                    pre_note = f"；预启动命令退出码 {_p.returncode}"
            else:
                pre_note = "；预启动命令已后台运行"
        except Exception as e:                         # noqa: BLE001
            pre_note = f"；预启动命令没跑起来：{str(e)[:50]}"
    # 开局前把输入法切成英文（设置里可关）：必须在 Popen 之前，
    # 新进程才会拿到 en-US 的默认输入语言（见 core/ime.py）
    try:
        from . import ime
        ime.launch_switch()
    except Exception:
        pass
    # 自动中文（第五十五轮）：游戏启动瞬间就把 options.txt 读进内存，所以必须
    # 在 Popen 之前把 lang 写成 zh_cn。只动 lang 那一行，键位/音量一个字节不碰。
    try:
        from . import langfix
        langfix.apply(cmd["cwd"])
    except Exception:
        pass
    # 第 119 轮：启动前预取皮肤 profile，429 退避重试。游戏内查皮肤是一次性
    # 的（撞限流就直接回退默认），这里先把它探通、并把 token 探活一起喂缓存。
    # 第 121 轮：若已注入 profileProperties（游戏皮肤数据现成，不再查 sessionserver），
    # 就别再请求那个被限流的端点了 —— 纯属浪费限流额度。只有注入失败时才退回
    # 这条老兜底路。失败都不阻断启动，断网照样能进单人/局域网。
    if not _skin_props:
        try:
            _prefetch_skin_profile(sess, progress=progress)
        except Exception as e:                         # noqa: BLE001
            crashlog.note("皮肤 profile 预取跳过：%s" % e)
    try:
        with open(logf, "w", encoding="utf-8", errors="replace") as out:
            out.write(" ".join(cmd["argv"]) + "\n\n")
            out.flush()
            proc = subprocess.Popen(cmd["argv"], cwd=cmd["cwd"], stdout=out,
                                    stderr=subprocess.STDOUT, creationflags=flags)
    except Exception as e:
        _ime_back()
        return False, f"启动进程失败：{e}"

    _RUN.update({"proc": proc, "log": logf, "name": t.get("name") or vid,
                 "started": time.time(), "cmd": cmd,
                 "iso": iso, "memory_mb": mem, "priority": launchprefs.priority(),
                 # B6：启动器可见性只记账，交给主线程在轮询时处理 —— 后台线程
                 # 绝不碰 QWidget（第 98 轮线程铁律）。
                 "visibility": str((cmd.get("options") or {}).get("visibility")
                                   or "keep")})
    _apply_priority(proc.pid, launchprefs.priority())
    # 秒退一般是参数/Java/资源问题：等一下再判，把日志尾巴给玩家看
    time.sleep(1.6)
    if proc.poll() is not None:
        tail = _tail(logf)
        _ime_back()          # 没起来就别把玩家扔在英文输入法里
        return False, (f"游戏启动后立刻退出（退出码 {proc.returncode}）。"
                       f"{('日志：' + tail) if tail else '详细日志见 ' + logf}")
    mode = "正版验证" if cmd["online"] else "离线"
    # Java 只说人看得懂的：.../jdk-17.0.12/bin/java.exe -> jdk-17.0.12
    jhome = os.path.basename(os.path.dirname(os.path.dirname(cmd["java"])))
    jlabel = jhome or os.path.basename(cmd["java"])
    tail = f"；{notes}" if notes else ""
    bits = f"{mode} · Java {jlabel} · {ram.fmt(mem)} 内存"
    if iso:
        bits += " · 独立目录"
    note = str(cmd.get("auth_note") or "")
    if note:
        bits += f"。{note}"
    cap_note = str(cmd.get("cap_note") or "")
    if cap_note:
        bits += f"。{cap_note}"
    bits += pre_note
    return True, f"已启动「{_RUN['name']}」（{bits}）{tail}"


def _apply_priority(pid: int, level: str) -> bool:
    """把游戏进程的 CPU 优先级调成「高 / 中 / 低」。

    Windows 侧只有这一个是「玩家真能感觉到」的旋钮：
      · 高（HIGH_PRIORITY_CLASS）：游戏帧数稳，但浏览器/下载可能卡；
      · 中（NORMAL）：默认，系统自己调度；
      · 低（BELOW_NORMAL）：让别的程序先跑，游戏掉帧。
    `IDLE_PRIORITY_CLASS` 不用 —— 那会让游戏在后台被压得几乎跑不动（连加载都慢）。
    """
    cls = {"high": 0x00000080, "normal": 0x00000020, "low": 0x00004000}.get(
        str(level or "").lower())
    if not cls or not pid or os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.SetPriorityClass.restype = wintypes.BOOL
        k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        PROCESS_SET_INFORMATION = 0x0200
        h = k32.OpenProcess(PROCESS_SET_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            return bool(k32.SetPriorityClass(h, cls))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def _ime_back():
    """开局失败时把输入法切回去（正常退出由主窗口的退出监测负责）"""
    try:
        from . import ime
        ime.restore()
    except Exception:
        pass


def _lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [ln.strip() for ln in f.readlines() if ln.strip()]
    except Exception:
        return []


_ERROR_KEYS = ("exception", "error", "caused by", "failed", "unsatisfiedlink",
               "could not", "unable to")


def _error_line(path, limit=200):
    """日志里最像错误的那一行（没有就返回空串）"""
    for ln in reversed(_lines(path)):
        low = ln.lower()
        if any(k in low for k in _ERROR_KEYS):
            return ln[:limit]
    return ""


def _tail(path, limit=260):
    """日志尾巴里挑最像错误的那几行"""
    lines = _lines(path)
    for ln in reversed(lines):
        low = ln.lower()
        if any(k in low for k in _ERROR_KEYS):
            return ln[:180]
    return lines[-1][:180] if lines else ""


def dry_run(game_dir=None, vid=None, java_exe="python"):
    """只拼命令不启动（排查用）"""
    gd = game_dir or _game_dir()
    t = launch_target()
    vid = vid or t.get("version_id") or ""
    inst = t.get("path") or os.path.join(gd, "versions", vid)
    return build_command(gd, vid, instance_dir=inst, java_exe=java_exe,
                         session=CONFIG.session())
