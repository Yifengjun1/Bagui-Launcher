# -*- coding: utf-8 -*-
"""崩溃诊断（第五十五轮）：把一坨英文堆栈翻成「人话原因」，并负责补前置。

对应需求：参照 PCLCE 的崩溃弹窗，列出简洁明了的崩溃原因（某个前置模组缺失 /
某个模组导致崩溃），前置缺了能一键补齐，日志能一键复制成 .txt 交给外置 AI。

三层证据，按可信度从高到低：
  ① `crash-reports/crash-*.txt` —— 加载器自己写的报告，有 Description / Caused by；
  ② 启动日志（gamelaunch 的 mcassist-launch.log，含 stdout/stderr）——
     Forge/NeoForge 的「Missing or unsupported mandatory dependencies」只在这里；
  ③ 退出码 —— 0xC0000005 之类连日志都不一定留。

所有正则都按「多版本加载器实测日志」写，别凭空猜格式：
  · Forge 1.20.5+ / NeoForge：`Mod ID: 'x', Requested by: 'y', Expected range: '[1.0,)', Actual version: '[MISSING]'`
  · Forge 1.12~1.19：`Missing Mods:` 后面跟 `   x : [1.0,)`
  · Fabric/Quilt：`- Mod 'A' (a) requires any version of mod 'b', which is missing!`
"""
import os
import re
import time

READ_LIMIT = 600 * 1024          # 日志只读尾部：崩溃原因几乎总在最后几十 KB


# ================================================================ 读文件
def _read_tail(path: str, limit: int = READ_LIMIT) -> str:
    try:
        n = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if n > limit:
                f.seek(n - limit)
                f.read(64)               # 丢掉被截断的半行
            return f.read()
    except OSError:
        return ""


def _text_of(info: dict) -> str:
    """日志 + 崩溃报告合成一份文本（报告在前：它的信息密度更高）"""
    parts = []
    rep = str(info.get("report") or "")
    if rep and os.path.isfile(rep):
        parts.append(_read_tail(rep, 200 * 1024))
    log = str(info.get("log") or "")
    if log and os.path.isfile(log):
        parts.append(_read_tail(log))
    # 兜底：有些版本把控制台写进 <game>/logs/latest.log，我们的 log 反而空
    gd = str(info.get("cwd") or "")
    for sub in ("logs/latest.log", "logs/debug.log"):
        p = os.path.join(gd, sub.replace("/", os.sep))
        if os.path.isfile(p):
            parts.append(_read_tail(p))
            break
    return "\n".join(parts)


# ================================================================ 加载期失败
# 第五十八轮：**游戏窗口已经开了、但卡在错误界面进不去**这种情况，进程还活着，
# 玩家最后点叉关掉时退出码是 0、存活时间也够长 —— 原来判定为「正常退出」，
# 于是启动器一声不响。真正的证据只有一个：日志里那几行「缺前置」。
_LIVE_LOG = "logs/latest.log"
_RE_DEP_FAIL = re.compile(
    r"Missing or unsupported mandatory dependencies"
    r"|Mod loading has failed"
    r"|Failed to (?:load|initialize) mods"
    r"|LoadingFailedException"
    r"|ModResolutionException"
    r"|Incompatible mod set"
    r"|Incompatible mods found"
    r"|A potential solution has been determined"
    r"|Missing Mods:"
    # Fabric/Quilt 0.19+：`Mod 'X' (x) 1.0 requires any version of fabric-api, which is missing!`
    # 关键差异：要装的那个模组 id 是**裸 id**，不再用 `mod '...'` 包一层 ——
    # 老正则只认带引号的写法，新版一条都命中不了。
    r"|requires\s+[^\n]{1,80}?\s+of\s+(?:mod\s+['\u2018\u2019]?)?[a-z0-9_.\-]{2,64}"
    r"\s*,?\s*(?:which\s+is\s+missing|but\s+only\s+the\s+wrong\s+version"
    r"|which\s+can'?t\s+be\s+loaded|which\s+is\s+disabled)"
    # ↓ 第六十二轮（玩家实测报的那个）：Fabric 走 `FormattedException.ofLocalized`，
    #   报错文本会**按系统语言翻译**。中文 Windows 上整段都是中文，英文正则全员失配，
    #   于是加载器自己都弹出「缺少前置」界面了，我们这边还写着「没认出具体原因」。
    #   下面这几条对应 Messages_zh_CN.properties 的 resolution.depends.* / solutionHeader。
    r"|需要\s*[^\n，,]{1,60}\s*的\s*[^\n，,]{1,40}\s*[，,]\s*"
    r"但(?:没有安装它|已经安装了的版本|由于其他限制条件无法载入|它在此环境中被停用)"
    r"|确定了一种可能的解决方法"
    r"|有不兼容的模组"
    r"|缺少(?:前置|依赖)",
    re.IGNORECASE)


def live_log(cwd: str, since: float = 0) -> str:
    """游戏**还在跑**时的权威日志：`<game>/logs/latest.log` 的尾部。

    `since` 是本次启动的时间戳 —— 比它更旧的日志是上一次的，绝不能拿来当这次的
    证据（否则上一回崩过一次，之后每次启动都会被判成缺前置）。
    """
    p = os.path.join(str(cwd or ""), _LIVE_LOG.replace("/", os.sep))
    try:
        if since and os.path.getmtime(p) < since - 5:
            return ""
    except OSError:
        return ""
    return _read_tail(p, 200 * 1024)


def dep_failure(text: str) -> bool:
    """这份文本里有没有「加载器因为缺东西直接放弃加载」的痕迹"""
    return bool(text) and bool(_RE_DEP_FAIL.search(text))


def live_missing(cwd: str, since: float = 0) -> list:
    """游戏还在跑 → 从 latest.log 里挖出缺哪些前置。没有就返回空表。

    给主界面每 3 秒轮询用：玩家卡在那张「加载失败」界面上等的时候，
    启动器就已经能把缺什么列出来（不必等他点叉关掉）。
    """
    t = live_log(cwd, since)
    if not dep_failure(t):
        return []
    return find_missing(t)


# ================================================================ 加载器没装全
# 第九十九轮（玩家实测的崩溃日志）：NeoForge 安装器跑到一半失败时，
# `versions/<id>/<id>.json` 留在原地、`libraries/` 里那几份安装器生成的 jar 没有。
# 之后每次启动都是：ModSorter 把 `minecraft` / `neoforge` 自己标成 [MISSING]
# → DisplayWindow.updateModuleReads 找不到 minecraft 模块 → 抛
# `NoSuchElementException: No value present` → 退出码 1、存活 10 秒。
#
# 这条必须先于「缺前置模组」判断：那张表里会列出 `Mod ID: 'minecraft'`，
# 按缺前置报就会把玩家引去补模组（怎么补都补不好，因为压根不是模组的事）。
_RE_SELF_MISSING = re.compile(
    r"Mod ID:\s*'(minecraft|neoforge|forge|fml)',\s*Requested by:\s*'[^']+'"
    r"[^\n]{0,140}?Actual version:\s*'\[MISSING\]'", re.IGNORECASE)
_RE_MODULE_GONE = re.compile(
    r"updateModuleReads|NoSuchElementException:\s*No value present", re.IGNORECASE)


def loader_broken(text: str) -> bool:
    """这份文本能不能说明「加载器只装了一半」"""
    if not text:
        return False
    if _RE_SELF_MISSING.search(text):
        return True
    # 单看 `No value present` 太泛（哪都可能出现），必须同时有 [MISSING]
    return bool(_RE_MODULE_GONE.search(text)) and "[MISSING]" in text


# ================================================================ 前置缺失
_RE_FORGE_DEP = re.compile(
    r"Mod ID:\s*'([^']+)',\s*Requested by:\s*'([^']+)'"
    r"(?:,\s*Expected range:\s*'([^']*)')?(?:,\s*Actual version:\s*'([^']*)')?")
_RE_FORGE_DEP_SHORT = re.compile(
    r"^\s*([a-z0-9_\-]+)\s*\(requested by\s+([a-z0-9_\-]+)\)\s*$",
    re.MULTILINE | re.IGNORECASE)
_RE_FABRIC_DEP = re.compile(
    r"requires\s+(?:version\s+[^ ]+\s+or\s+later\s+of\s+|any\s+version\s+of\s+)?"
    r"mod\s+'([^']+)'(?:\s*\(([^)]+)\))?[^.\n]*"
    r"(?:which\s+is\s+missing|is\s+missing)", re.IGNORECASE)
_RE_FABRIC_DEP2 = re.compile(
    r"Mod\s+'([^']+)'\s*\(([^)]+)\)\s*requires\s+.*?mod\s+'([^']+)'", re.IGNORECASE)
_RE_OLD_MISSING = re.compile(r"Missing Mods:\s*\n((?:[ \t]*[a-z0-9_\-]+\s*:.*\n?)+)",
                             re.IGNORECASE)

# ---- Fabric/Quilt 新版排版 + 中文（第六十二轮）--------------------------------
# 老的两条正则只认 `requires ... mod 'xxx', which is missing` 这种英文写法，实测
# Fabric Loader 0.20.1 两个地方都变了：
#   ① 依赖 id 变裸的（没有 `mod '...'` 包装）：
#        Mod 'EntityCulling' (entityculling) 1.11.3 requires any version of fabric-api, which is missing!
#   ② 报错文本本地化（`FormattedException.ofLocalized`）：
#        模组 'EntityCulling'（entityculling）1.11.3 需要 fabric-api 的 任意版本，但没有安装它！
#   于是加载器自己弹出了「缺少前置」界面，我们却只显示「没认出具体原因」。
# 这里按「整段文本扫 + 往前找是谁在要」的方式补，不再依赖分行（多行 message 里分隔符不一定）。
_RE_FAB_LINE_ZH = re.compile(
    r"需要\s*(?P<dep>[a-z0-9_.\-]{2,64})\s*的\s*(?P<rng>[^，,！!。\n]{1,60}?)\s*[，,]\s*"
    r"(?:但没有安装它"
    r"|但已经安装了的版本\s*(?P<actual>[^，,！!。\n]{1,60}?)\s*不对"
    r"|但由于其他限制条件无法载入"
    r"|但它在此环境中被停用)")
_RE_FAB_LINE_EN = re.compile(
    r"requires\s+(?P<rng>[^\n]{1,60}?)\s+of\s+(?:mod\s+['\u2018\u2019])?"
    r"(?P<dep>[a-z0-9_.\-]{2,64})['\u2018\u2019]?\s*,?\s*"
    r"(?:which\s+is\s+missing"
    r"|but\s+only\s+the\s+wrong\s+version[^:\n]{0,20}:\s*(?P<actual>[^!\n]{1,80})"
    r"|which\s+can'?t\s+be\s+loaded"
    r"|which\s+is\s+disabled)", re.IGNORECASE)
# `'名字'（id）` / `'name' (id)` —— 名字里允许带单引号（`Xaero's Minimap`），所以名字
# 部分不能排除引号，只能靠「非贪婪 + 引号紧跟括号」来定界，否则会在 `Xaero` 那个
# 撇号上截断，`name` 只剩 `s Minimap`。
_RE_BY_REF = re.compile(
    r"['\u2018\u2019](?P<name>[^\n]{1,80}?)['\u2018\u2019]\s*"
    r"[（(]\s*(?P<id>[a-z0-9_.\-]{2,64})\s*[)）]")


def _norm_range(s: str) -> str:
    """版本范围说法归一到界面上的中文（`any version` / `任意版本` → `任意版本`）"""
    s = str(s or "").strip().strip("，, ")
    return "任意版本" if s.lower() in ("any version", "任意版本", "") else s


def _by_before(text: str, pos: int, span: int = 400) -> str:
    """缺前置那句话**同一行**往前找最近的 `'名字'（id）` —— 谁在要这个前置。

    只在本行内找：跨行会抓到上一条无关的模组引用（`- 将 模组 'A'…` 那一行）。
    """
    start = max(0, pos - span)
    nl = text.rfind("\n", start, pos)
    if nl >= 0:
        start = nl + 1
    best = ""
    for m in _RE_BY_REF.finditer(text, start, pos):
        best = m.group("id")
    return best


def _dep_name(modid: str) -> str:
    """modid → 好看一点的名字（geckolib → geckolib，保持不变，UI 里再配中文说明）"""
    return str(modid or "").strip()


def find_missing(text: str) -> list:
    """从日志里抠出「缺了哪个前置」。返回 [{id, by, range, actual}]，去重保序。"""
    out, seen = [], {}

    def add(modid, by="", rng="", actual=""):
        mid = str(modid or "").strip()
        if not mid:
            return
        # 加载器自己、原版、Java 不是「玩家能补的前置」
        if mid.lower() in ("minecraft", "java", "forge", "neoforge", "fabricloader",
                           "fabric-loader", "quilt_loader", "quiltloader",
                           "fabric", "quilt_loader"):
            return
        key = mid.lower()
        if key in seen:
            # 同一个前置被多个模组需要（截图里 6 个模组都要 fabric-api）：
            # 合并成一条，把「谁要的」攒起来 —— 玩家删模组时用得上。
            it = seen[key]
            by = str(by or "").strip()
            if by and by not in it["by"].split("、"):
                names = it["by"].split("、") if it["by"] else []
                if len(names) < 6:
                    it["by"] = "、".join(names + [by])
            if rng and (not it["range"] or
                        (it["range"] == "任意版本" and rng != "任意版本")):
                # 6 个模组各要一个版本范围时，留**最具体**的那个
                # （`任意版本` 没有信息量，`0.43.1 及以上版本` 才是玩家要知道的）
                it["range"] = rng
            if actual and not it["actual"]:
                it["actual"] = actual
            return
        item = {"id": mid, "by": str(by or ""), "range": str(rng or ""),
                "actual": str(actual or "")}
        seen[key] = item
        out.append(item)

    for m in _RE_FORGE_DEP.finditer(text):
        actual = (m.group(4) or "").strip()
        # 「Actual version: '4.4.4'」说明不缺，只是版本不在范围内 —— 也算问题，
        # 但补的时候要装的是**它**，所以照收。
        add(m.group(1), m.group(2), m.group(3) or "", actual)
    for m in _RE_FORGE_DEP_SHORT.finditer(text):
        add(m.group(1), m.group(2))
    for m in _RE_OLD_MISSING.finditer(text):
        for line in m.group(1).splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            mid, _, rng = line.partition(":")
            add(mid.strip(), "", rng.strip())
    for m in _RE_FABRIC_DEP.finditer(text):
        add(m.group(1), "", "", "")
    for m in _RE_FABRIC_DEP2.finditer(text):
        add(m.group(3), m.group(1), "", "")
    # Fabric/Quilt 新版 + 中文：中英各扫一遍（两份正则不会互相命中，重复由 add 去重）
    for rx in (_RE_FAB_LINE_ZH, _RE_FAB_LINE_EN):
        for m in rx.finditer(text):
            gd = m.groupdict()
            add(gd.get("dep"), _by_before(text, m.start()),
                _norm_range(gd.get("rng")), (gd.get("actual") or "").strip())
    return out


# ================================================================ 已知崩溃特征
# (正则, kind, 标题, 建议)
_PATTERNS = [
    (r"OutOfMemoryError|Java heap space|GC overhead limit exceeded",
     "oom", "内存不够（Java 堆溢出）",
     "到「设置 → 运行内存」把分配调大，或者改成「自动」；32 位 Java 建议换成 64 位。"),
    (r"UnsupportedClassVersionError|compiled by a more recent version of the Java Runtime",
     "java", "Java 版本太低",
     "这个游戏版本要更高的 Java。到「设置 → 启动项」把 Java 换成对应的 JDK（1.20.5+ 一般要 21）。"),
    (r"Pixel format not accelerated|Failed to create window|GLFW error|"
     r"WGL: Failed to create|OpenGL.*(?:not supported|failed)|"
     r"EXCEPTION_ACCESS_VIOLATION.*(nvoglv|atio6axx|ig\\d+icd)",
     "gpu", "显卡驱动 / OpenGL 不兼容",
     "更新显卡驱动；笔记本双显卡的话，把 java.exe 设为「高性能显卡」运行。老显卡可加 JVM 参数 -Dorg.lwjgl.opengl.Window.undecorated=false。"),
    (r"Mixin apply failed|MixinApplyError|MixinTransformerError|"
     r"InvalidMixinException|mixin\.throwables",
     "mixin", "某个模组的注入（Mixin）失败",
     "通常是模组版本和游戏/加载器对不上。看下面「可疑模组」，把那个模组换成对应版本。"),
    (r"Duplicate mods|Found a duplicate mod|duplicate modid",
     "dup", "同一个模组装了两份",
     "去 mods 文件夹里把重复的那份（常见是 XXX.jar 和 XXX (1).jar）删掉一个。"),
    (r"java\.util\.zip\.ZipException|invalid LOC header|zip END header not found|"
     r"Invalid or corrupted jarfile",
     "zip", "有 jar 文件下载不完整（损坏）",
     "用「游戏目录 → 补全整合包文件」重新校验一遍；或者把最近装的模组删掉重下。"),
    (r"java\.lang\.VerifyError|ClassFormatError|Unsupported class file major version",
     "verify", "模组文件损坏或版本不匹配",
     "重新下载那个模组；确认它和游戏版本、加载器都一致。"),
    (r"NoSuchMethodError|NoSuchFieldError|AbstractMethodError|"
     r"IncompatibleClassChangeError|NoClassDefFoundError|ClassNotFoundException",
     "bcmatch", "模组与游戏版本不匹配（缺类 / 缺方法）",
     "常见于「给 1.20.1 装的模组装进了 1.21」。看下面「可疑模组」，换成对应游戏版本的版本。"),
    (r"ExceptionInInitializerError",
     "init", "某个模组初始化时出错",
     "多半是配置/前置问题。看下面「可疑模组」和 Caused by 里的第一行。"),
    (r"java\.lang\.StackOverflowError",
     "stack", "死循环 / 递归爆栈",
     "常见于两个模组互相调用。看「可疑模组」，逐个禁用排查。"),
]
_PATTERN_CACHE = [(re.compile(p, re.IGNORECASE), k, t, h) for p, k, t, h in _PATTERNS]

# 不可能是「模组类」的包名（这些是原版 / 加载器 / 平台自己的栈帧）
_NOT_MOD_PKG = ("java.", "javax.", "jdk.", "sun.", "net.minecraft.", "com.mojang.",
                "org.spongepowered.", "org.lwjgl.", "io.netty.", "com.google.",
                "org.apache.", "it.unimi.", "org.objectweb.", "cpw.mods.",
                "net.neoforged.", "net.minecraftforge.", "org.slf4j.", "oshi.",
                "com.electronwill.", "net.fabricmc.", "org.quiltmc.", "kotlin.",
                "kotlinx.", "org.jetbrains.", "com.sun.")

_RE_FRAME = re.compile(r"^\s*at\s+([a-zA-Z_$][\w$]*(?:\.[\w$]+)+)\(", re.MULTILINE)
_RE_CAUSED = re.compile(r"^Caused by:\s*(.+)$", re.MULTILINE)
_RE_DESC = re.compile(r"^Description:\s*(.+)$", re.MULTILINE)


def _mods_dir(info: dict) -> str:
    for cand in (info.get("cwd"), info.get("instance"), info.get("game_dir")):
        if cand and os.path.isdir(os.path.join(str(cand), "mods")):
            return os.path.join(str(cand), "mods")
    return ""


def _jar_names(mods_dir: str) -> list:
    try:
        return [n for n in os.listdir(mods_dir) if n.lower().endswith(".jar")]
    except OSError:
        return []


def suspect_mods(text: str, mods_dir: str = "", limit: int = 6) -> list:
    """从堆栈里的包名猜「是哪个模组」—— 拿包前缀去和 mods 里的 jar 名对。

    为什么不一上来读模组元数据：jar 里的 mods.toml 要解压才看得到，装了几百个
    模组时太慢。jar 文件名（geckolib-forge-1.20.1-4.4.4.jar）本身就带 modid，
    拿包名最前两段（com/foo）去比已经够准，比不了就明说「不确定」。
    """
    jars = _jar_names(mods_dir)
    if not jars:
        return []
    low_jars = [(n, n.lower()) for n in jars]
    hits, seen = [], set()
    for m in _RE_FRAME.finditer(text):
        cls = m.group(1)
        pkg = cls.rsplit(".", 1)[0].lower()
        if pkg.startswith(_NOT_MOD_PKG):
            continue
        segs = pkg.split(".")
        for take in (2, 3):
            key = ".".join(segs[:take])
            if not key or key in seen:
                continue
            tok = segs[min(take, len(segs)) - 1]
            if len(tok) < 3:
                continue
            for name, lname in low_jars:
                # jar 文件名里带这个包名段就算命中（geckolib / create / curios…）
                if tok in lname.replace("_", "-"):
                    if name not in [h["jar"] for h in hits]:
                        hits.append({"jar": name, "pkg": key})
                        seen.add(key)
                    break
            if len(hits) >= limit:
                return hits
        if len(hits) >= limit:
            break
    return hits


def _code_hint(code) -> tuple:
    """退出码 → (标题, 建议)。Windows 上这些码都是「进程被系统干掉」。"""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "", ""
    if c == 0:
        return "", ""
    # 无符号形态：-1073741819 == 0xC0000005
    u = c & 0xFFFFFFFF
    table = {
        0xC0000005: ("游戏原生崩溃（内存访问冲突 0xC0000005）",
                     "多半是显卡驱动或某个模组的原生代码。先更新显卡驱动；"
                     "还崩就逐个禁用最近装的模组。"),
        0xC000001D: ("游戏原生崩溃（非法指令 0xC000001D）",
                     "CPU 不支持这个 Java 的指令集：换一个更保守的 JDK（比如 17 或 21 的 Zulu）。"),
        0xC0000409: ("游戏原生崩溃（栈溢出 / 快速失败 0xC0000409）",
                     "常见于模组递归调用。看「可疑模组」逐个排查。"),
        0xC0000135: ("缺少运行库（0xC0000135）",
                     "安装 Microsoft Visual C++ 运行库（x64）。"),
        0x40010004: ("游戏被调试器 / 系统结束（0x40010004）",
                     "如果有杀毒软件拦截了 java.exe，把它加白名单。"),
    }
    return table.get(u, ("", ""))


def _clip(s: str, n: int) -> str:
    s = str(s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


# ================================================================ 加载器原文
def loader_excerpt(text: str, limit: int = 1600, span: int = 16) -> str:
    """把加载器自己那屏报错（日志里那一段）原样摘出来，内置进我们的弹窗。

    第六十八轮（用户要求）：加载器自带的报错界面不再显示（进程由我们关掉），
    但「它到底说了什么」必须还看得到 —— 否则玩家只看到我们归纳的一行标题，
    想核对是哪个 jar、要求什么版本范围，就得自己去翻 latest.log。

    定位：第一条命中 `_RE_DEP_FAIL` 的行（加载失败标志），往前留 3 行、往后取
    `span` 行；一条都没命中就退回「尾部第一行像错误的」。原文照抄，不做任何美化。
    """
    raw = str(text or "")
    if not raw.strip():
        return ""
    lines = raw.splitlines()
    idx = -1
    for i, ln in enumerate(lines):
        if _RE_DEP_FAIL.search(ln):
            idx = i
            break
    if idx < 0:
        # 没有明确标志：从尾部往前找第一行像错误的
        for i in range(len(lines) - 1, max(-1, len(lines) - 120), -1):
            low = lines[i].lower()
            if any(k in low for k in ("error", "exception", "caused by",
                                      "failed", "错误", "异常", "失败")):
                idx = i
                break
    if idx < 0:
        return ""
    start = max(0, idx - 3)
    chunk = [ln.rstrip() for ln in lines[start:idx + span]]
    # 前后空行去掉，别在卡片里留一大段白
    while chunk and not chunk[0].strip():
        chunk.pop(0)
    while chunk and not chunk[-1].strip():
        chunk.pop()
    out = "\n".join(chunk)
    return _clip(out, limit)


def memory_shortage(info: dict, text: str = "") -> dict | None:
    """第一百二十六轮：判定「是不是内存分配不足导致的崩溃」。

    用户反馈：内存不够崩了却提示「多半是某个模组的问题」，误导玩家去删模组。
    判据（满足任一即认为内存不足，优先级高于模组）：
      ① 日志里有 Java OOM / GC overhead 字样；
      ② 退出码是 0xC0000005 / 0xC000001D 这类「进程被系统干掉」，且本次分配的
         内存偏小（< 推荐值的一半）——这是 -Xmx 太小、堆溢出后被 Windows 杀掉的
         典型形态，日志里往往没有 Java OOM 那行字；
      ③ 存活极短（< 25 秒）且分配内存 < 3GB —— 进了世界就 GC 死。
    返回 reason 字典；不是内存问题返回 None。"""
    from . import ram as _ram
    mb = int(info.get("memory_mb") or 0)
    # 推荐值按这个游戏实际模组数算（现场字典没带 mods，就从游戏目录数）
    mods = int(info.get("mods") or 0)
    if not mods:
        gd = str(info.get("cwd") or info.get("game_dir") or "")
        if gd:
            try:
                mods = _ram.count_mods(os.path.join(gd, "mods"))
            except Exception:
                mods = 0
    try:
        rec, _ = _ram.auto_alloc(mods)
    except Exception:
        rec = 4096
    oom_hit = bool(re.search(r"OutOfMemoryError|Java heap space|GC overhead limit exceeded",
                             text or "", re.I))
    try:
        u = int(info.get("code")) & 0xFFFFFFFF
    except (TypeError, ValueError):
        u = 0
    killed = u in (0xC0000005, 0xC000001D, 0xC0000409)
    lived = info.get("lived")
    try:
        lived = float(lived)
    except (TypeError, ValueError):
        lived = None
    short_lived = lived is not None and lived < 25
    # 分配值明显低于「这个包该给的量」就算偏低（85% 阈值：大包给 4G 而推荐 6G+ 必判低）
    low = bool(mb) and mb < max(2560, int(rec * 0.85))
    if not (oom_hit or (killed and low) or (short_lived and low)):
        return None
    detail_bits = []
    if mb:
        detail_bits.append(f"本次给游戏分配了 {mb} MB")
        if rec:
            detail_bits.append(f"按当前模组数建议 {rec} MB")
    if u:
        detail_bits.append(f"退出码 0x{u:08X}（进程被系统结束）")
    if oom_hit:
        detail_bits.append("日志里出现 Java 堆溢出 / GC 超限")
    return {
        "kind": "oom",
        "title": "内存分配不足（不是模组的问题）",
        "detail": "；".join(detail_bits) or "堆内存不足以支撑当前整合包。",
        "mods": [], "fix": "",
        "hint": "到「设置 → 运行内存」把分配调大，或改成「自动配置」——"
                "新版按模组数用 PCL2 四阶段算法计算，大整合包会比旧版多给。"
                "若已是自动仍不够，再回来精简模组；顺序不要反。",
    }


def analyze(info: dict) -> dict:
    """主入口：返回 {summary, reasons:[...], missing:[...], suspects:[...], text}"""
    text = _text_of(info)
    reasons = []

    # ---- ⓪-b 内存不足（第 126 轮）：排在模组/缺前置之前 ----
    # 用户反馈：内存不够崩了却提示「多半是某个模组的问题」。内存是更常见的
    # 真因且能一键调，提到最前面；但加载器没装全仍是最硬的判据，留给它。
    mem_reason = memory_shortage(info, text)
    if mem_reason:
        reasons.append(mem_reason)

    # ---- ⓪ 加载器只装了一半（必须排在「缺前置」前面，理由见上面那段注释） ----
    broken_loader = loader_broken(text)
    if broken_loader:
        reasons.append({
            "kind": "loader",
            "title": "加载器没装全（不是模组的问题）",
            "detail": "日志里 `minecraft` / `neoforge` 自己被标成了 [MISSING]，"
                      "说明 Forge/NeoForge 的安装器没跑完 —— "
                      "`libraries/` 里缺它生成的那几份 jar，游戏在开窗口那一瞬间就退出了。",
            "mods": [], "fix": "",
            "hint": "到「设置 → 启动项」把这一栏的加载器重新安装一次（联网状态），"
                    "或者删掉这个整合包重装。模组不用动，删哪个都没用。",
        })

    # ---- ① 前置缺失（最优先：这条能一键补） ----
    missing = [] if broken_loader else find_missing(text)
    if missing:
        names = "、".join(m["id"] for m in missing[:4])
        more = f" 等 {len(missing)} 个" if len(missing) > 4 else ""
        by = missing[0].get("by") or ""
        detail = f"缺少前置模组：{names}{more}。"
        if by:
            # 一个前置常被好几个模组同时需要（实测 6 个模组都要 fabric-api），
            # 全列出来会把弹窗撑爆，只报前两个 + 个数
            who = by.split("、")
            head = "、".join(who[:2])
            detail += (f"（被「{head}」等 {len(who)} 个模组需要）" if len(who) > 2
                       else f"（被「{head}」需要）")
        reasons.append({
            "kind": "dep", "title": "缺少前置模组", "detail": detail,
            "mods": [m["id"] for m in missing], "fix": "deps",
            "hint": "点右下角「自动补充」会去 Modrinth 找这几个前置，按当前游戏版本下好放进 mods。",
        })

    # ---- ② 已知特征 ----
    for rx, kind, title, hint in _PATTERN_CACHE:
        m = rx.search(text)
        if not m:
            continue
        # 同一 kind 只报一次
        if any(r["kind"] == kind for r in reasons):
            continue
        line = _line_at(text, m.start())
        reasons.append({"kind": kind, "title": title,
                        "detail": _clip(line, 220), "mods": [], "fix": "",
                        "hint": hint})

    # ---- ③ 崩溃报告里的 Description / Caused by ----
    desc = _first(_RE_DESC, text)
    caused = _last(_RE_CAUSED, text)
    if caused and not any(r["kind"] in ("dep", "loader") for r in reasons):
        reasons.append({
            "kind": "caused",
            "title": "根因（Caused by）",
            "detail": _clip(caused, 240),
            "mods": [], "fix": "",
            "hint": "这一行是加载器给出的直接原因，通常配合下面的「可疑模组」一起看。",
        })

    # ---- ④ 可疑模组 ----
    suspects = suspect_mods(text, _mods_dir(info))

    # ---- ⑤ 退出码兜底 ----
    code = info.get("code")
    t, h = _code_hint(code)
    if t and not reasons:
        reasons.append({"kind": "native", "title": t, "detail": f"退出码 {code}",
                        "mods": [], "fix": "", "hint": h})

    # ---- ⑥ 什么都没认出来：至少把最后一行异常亮出来 ----
    if not reasons:
        ln = _last_error_line(text)
        reasons.append({
            "kind": "unknown",
            "title": "没认出具体原因（多半是某个模组的问题）",
            "detail": _clip(ln or desc or "日志里没有明显的错误行", 240),
            "mods": [], "fix": "",
            "hint": "点「分享日志」把 txt 发给外置 AI，它能读完整个日志再判断；"
                    "也可以先看「查看日志」里最后几十行。",
        })

    summary = reasons[0]["title"]
    if missing:
        summary = f"缺少前置模组：{'、'.join(m['id'] for m in missing[:3])}"
    elif suspects and reasons[0]["kind"] not in ("dep",):
        summary = f"{summary}（疑似 {suspects[0]['jar']}）"
    # 第六十八轮：加载器那屏原文也带出去（弹窗里内置展示，那边的窗口会被关掉）
    return {"summary": summary, "reasons": reasons, "missing": missing,
            "suspects": suspects, "text": text, "desc": desc,
            "loader_text": loader_excerpt(text)}


def _line_at(text: str, idx: int) -> str:
    s = text.rfind("\n", 0, idx) + 1
    e = text.find("\n", idx)
    return text[s:e if e > 0 else len(text)].strip()


def _clip(s: str, n: int) -> str:
    s = str(s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _first(rx, text, n=200):
    m = rx.search(text)
    return _clip(m.group(1), n) if m else ""


def _last(rx, text, n=200):
    last = None
    for m in rx.finditer(text):
        last = m
    return _clip(last.group(1), n) if last else ""


def _last_error_line(text: str) -> str:
    for ln in reversed(text.splitlines()):
        low = ln.lower()
        if any(k in low for k in ("exception", "error", "caused by", "failed")):
            return ln.strip()
    return ""


# ================================================================ 分享日志
def share(info: dict, diag: dict = None) -> tuple:
    """把日志整理成一份 .txt（给外置 AI 看）。返回 (路径, 是否成功)。

    为什么要 txt：不少 AI 上传入口不认 .log，直接拖过去会被拒；而且玩家也不知道
    「日志在哪」。这里一次性拷成 txt，复制到剪贴板 + 在资源管理器里选中，
    玩家只要拖一下就行。
    """
    from . import config as _cfg
    diag = diag or analyze(info)
    d = os.path.join(_cfg.app_data_dir(), "shared_logs")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = _cfg.app_data_dir()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[\\/:*?\"<>|]+", "_", str(info.get("name") or "游戏"))[:40]
    path = os.path.join(d, f"八桂崩溃日志_{safe}_{stamp}.txt")

    head = [
        "==== 八桂启动器 · 崩溃日志 ====",
        f"时间       ：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"启动器版本 ：{_safe_version()}",
        f"游戏实例   ：{info.get('name') or '?'}",
        f"游戏版本   ：{info.get('version') or '?'}   原版：{info.get('mc') or '?'}",
        f"游戏目录   ：{info.get('cwd') or '?'}",
        f"Java       ：{info.get('java') or '?'}",
        f"分配内存   ：{info.get('memory_mb') or 0} MB",
        f"退出码     ：{info.get('code')}    存活：{int(info.get('lived') or 0)} 秒",
        f"系统       ：{_sysinfo()}",
        "",
        "---- 启动器判断的崩溃原因 ----",
    ]
    for i, r in enumerate(diag.get("reasons") or [], 1):
        head.append(f"{i}. {r['title']}：{r['detail']}")
        if r.get("hint"):
            head.append(f"   建议：{r['hint']}")
    if diag.get("missing"):
        head.append("缺少的前置：" + "、".join(m["id"] for m in diag["missing"]))
    if diag.get("suspects"):
        head.append("可疑模组：" + "、".join(s["jar"] for s in diag["suspects"]))
    head += ["", "---- 原始日志 ----", ""]
    body = diag.get("text") or ""
    try:
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write("\n".join(head))
            f.write(body[-400000:] if len(body) > 400000 else body)
        return path, True
    except OSError:
        return path, False


def _safe_version() -> str:
    try:
        from .config import APP_VERSION
        return APP_VERSION
    except Exception:
        return "?"


def _sysinfo() -> str:
    try:
        import platform
        return f"{platform.system()} {platform.release()} ({platform.machine()})"
    except Exception:
        return "?"


def reveal(path: str):
    """在资源管理器里选中这个文件（只选中，不打开 —— 玩家要拖给 AI）"""
    try:
        if os.name == "nt":
            import subprocess
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception:
        pass


# ================================================================ 自动补前置
_LOADER_FACET = {"forge": "forge", "neoforge": "neoforge",
                 "fabric": "fabric", "quilt": "quilt", "": "forge"}


def _loader_mc(info: dict) -> tuple:
    """当前实例的 (加载器, 游戏版本)：补前置要按这两个筛版本"""
    loader, mc = "", str(info.get("mc") or "")
    inst = str(info.get("cwd") or info.get("instance") or "")
    try:
        from . import runtime
        desc = runtime.describe_version(inst) or {}
        loader = str(desc.get("loader") or "").lower()
        mc = str(desc.get("mc") or desc.get("client_version") or mc)
    except Exception:
        pass
    if not loader:
        low = str(info.get("version") or "").lower()
        for k in ("neoforge", "forge", "fabric", "quilt"):
            if k in low:
                loader = k
                break
    return loader, mc


def _project_for(modid: str):
    """modid → Modrinth 项目。先按 slug 直取，不行再搜一次取最像的。"""
    from . import modrinth
    try:
        p = modrinth.get_project(modid)
        if p and p.get("id"):
            return p
    except Exception:
        pass
    try:
        res = modrinth.search("mod", modid, limit=6)
        hits = res.get("hits") or []
        low = modid.lower().replace("_", "-")
        for h in hits:
            slug = str(h.get("slug") or "").lower()
            if slug == low or low in slug or slug in low:
                return h
        return hits[0] if hits else None
    except Exception:
        return None


def fill_missing(info: dict, diag: dict = None, progress=None,
                 bytes_cb=None) -> dict:
    """按诊断结果去 Modrinth 下缺的前置，放进 <实例>/mods。

    返回 {ok, installed:[文件名], failed:[(modid, 原因)], dir}

    progress(i, total, text)   第 i 个前置开始了（界面据此知道总数）
    bytes_cb(i, done, total)   第 i 个文件下到多少字节（第六十二轮：环形进度条要连续的）
    """
    diag = diag or analyze(info)
    missing = diag.get("missing") or []
    loader, mc = _loader_mc(info)
    facet = _LOADER_FACET.get(loader, loader or "forge")
    mods = _mods_dir(info)
    if not mods:
        mods = os.path.join(str(info.get("cwd") or info.get("instance") or ""), "mods")
    result = {"ok": False, "installed": [], "failed": [], "dir": mods}
    if not missing:
        return result
    try:
        os.makedirs(mods, exist_ok=True)
    except OSError as e:
        result["failed"].append(("*", f"mods 目录建不出来：{e}"))
        return result

    from . import modrinth
    total = len(missing)
    for i, m in enumerate(missing):
        mid = m["id"]
        if progress:
            progress(i, total, f"查找 {mid}…")
        try:
            proj = _project_for(mid)
            best = None
            if proj:
                pid = (proj.get("project_id") or proj.get("id")
                       or proj.get("slug") or mid)
                best = modrinth.best_file_for(pid, loaders=[facet] if facet else None,
                                              game_versions=[mc] if mc else None)
            if not best:
                # 开源版只走 Modrinth：找不到适配版本就记失败，不乱装别的模组。
                result["failed"].append((mid, "Modrinth 上没有可用的版本"))
                continue
            _ver, url, fname = best
            fname = fname or f"{mid}.jar"
            dest = os.path.join(mods, fname)
            if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                result["installed"].append(fname)
                continue
            if progress:
                progress(i, total, f"下载 {fname}…")
            # 第 i 个文件下到多少字节 → 界面把它折算成 (i + frac) / total
            cb = (lambda d, t, _i=i: bytes_cb(_i, d, t)) if bytes_cb else None
            try:
                fsize = int((_ver or {}).get("size") or 0)
            except (TypeError, ValueError, AttributeError):
                fsize = 0
            modrinth.download_to(url, dest, progress_cb=cb, expected_size=fsize)
            if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                result["installed"].append(fname)
            else:
                result["failed"].append((mid, "下载下来是空文件"))
        except Exception as e:                          # noqa: BLE001
            result["failed"].append((mid, str(e)[:80]))
    if progress:
        progress(total, total, "完成")
    result["ok"] = bool(result["installed"]) and not result["failed"]
    return result
