# -*- coding: utf-8 -*-
"""下载源设置（官方 / 自动 / 镜像），按「内容类别」逐项可调。

为什么要分项：国内网络下不同域名的可达性完全不同 —— 有时只有 Maven 仓库被掐，
有时是版本清单慢，有时镜像自己反而更慢。PCL2 那类启动器也是按域名分开设的，
这里按同样粒度提供，并把「自动」做成默认：官方优先，连不上才换镜像，
且换成镜像后本次会话内不再重复等官方超时。

配置存储：CONFIG["mirror_sources"] = {"default": "auto", "items": {key: mode}}
- default：所有类别的默认策略
- items：单个类别的覆盖值
mode 取值：
- official：只用官方源
- auto：官方优先，失败自动降级镜像（推荐）
- always：直接用镜像（官方彻底连不上时最快）
"""
import json
import re
import threading
import time

import requests

UA = "MC-GuanQi-Fuzhu-Gongju/1.0.0"
BMCLAPI = "https://bmclapi2.bangbang93.com"
BMCLAPI_MAVEN = BMCLAPI + "/maven"
BMCLAPI_LIBRARIES = BMCLAPI + "/libraries"

# ---------------------------------------------------------------- 第 104 轮：MCIMirror 可选代理
# 只读实测（2026-11-08，本机直连）：
#   https://mod.mcimirror.top/modrinth/v2/project/sodium  → 200 gzip JSON, slug=sodium（355ms）
#   https://mod.mcimirror.top/ 根 → 200，指向 mcimirror.top/docs
#   CDN 代理（换成该域名）→ **302 跳回官方 CDN**，
#     不是独立镜像，接进来只是多一次往返，没有收益。
# 所以：只把 **API 类**做成可选策略（默认关），CDN 两项如实记录为「不可达/不接入」，
# 绝不硬编码一个没验证过的地址、也绝不静默把玩家切到第三方源。
MCIM = "https://mod.mcimirror.top"
MCIM_API_PREFIX = {
    "https://api.modrinth.com": MCIM + "/modrinth",
}
MCIM_CDN_NOTE = ("只读实测：CDN 路径返回 302 跳回官方 CDN，不是独立镜像，"
                 "本轮不接入（不静默降级、不硬编码不可用地址）")
RACE_DELAY = 3.0            # 错峰：官方先发，约 3 秒后镜像跟上

MODE_CN = {"official": "官方", "auto": "自动", "always": "镜像"}
MODES = ("official", "auto", "always")

# 「自动」模式下除了「连不上」，「连得上但很慢」也要换镜像（第 52 轮）。
# 判定只针对元数据请求（json/xml）：时长超过 SLOW_SECS 记一分，连续 SLOW_HITS 分
# 之后本会话直接优先镜像。**绝不看传输速率** —— 启动器的「速度限制」只作用于
# `core/dl.py` 的文件下载，跟这里不是同一条路径，所以限速不会污染判定。
SLOW_SECS = 6.0
SLOW_HITS = 2
# 官方源的分档短超时：连接/首字节 5 秒没动静就当失败，立刻换镜像，
# 不用干等调用方给的 20~25 秒。镜像自己仍用原超时（两边都慢就没得救了）。
_SHORT = 5.0

# 每个类别：key / group（设置页的展开式分组）/ 显示名 / 一句话说明 / 官方前缀 / 镜像前缀
# 前缀按「长的先匹配」处理，所以可以同时列出子路径与域名。
# mirror 为空 = 国内没有可用镜像（模组平台这一组都是），设置页只会给「官方」一个选项。
SOURCES = [
    {
        "key": "version",
        "group": "游戏本体",
        "name": "版本清单与版本文件",
        "desc": "游戏版本号列表、每个版本的 json（决定能装哪些版本）",
        "official": ["https://launchermeta.mojang.com",
                     "https://piston-meta.mojang.com",
                     # 第 104 轮：PCL-CE DlSourceLauncherOrMetaGet 里还有这两个
                     # 官方前缀，以前漏了 → 落到「没有镜像」分支直接不代理。
                     "https://piston-data.mojang.com",
                     "https://launcher.mojang.com"],
        "mirror": BMCLAPI,
    },
    {
        "key": "libraries",
        "group": "游戏本体",
        "name": "依赖库文件",
        "desc": "启动游戏所需的 libraries（jar / natives）",
        "official": ["https://libraries.minecraft.net"],
        "mirror": BMCLAPI_MAVEN,
        # 第 104 轮：BMCLAPI 同时提供 /maven 与 /libraries 两套路径，
        # 有的库只在后者有；做成**候选**而不是写死其中一个。
        "mirror_alt": [BMCLAPI_LIBRARIES],
    },
    {
        "key": "assets",
        "group": "游戏本体",
        "name": "游戏资源文件",
        "desc": "声音、语言、贴图等 assets 资源",
        "official": ["https://resources.download.minecraft.net"],
        "mirror": BMCLAPI + "/assets",
    },
    {
        "key": "forge",
        "group": "加载器",
        "name": "Forge 仓库",
        "desc": "Forge 版本列表、安装器与依赖库",
        "official": ["https://maven.minecraftforge.net"],
        "mirror": BMCLAPI_MAVEN,
    },
    {
        "key": "neoforge",
        "group": "加载器",
        "name": "NeoForge 仓库",
        "desc": "NeoForge 版本列表、安装器与依赖库",
        # /releases 前缀必须单列：镜像路径要把它去掉（bmclapi/maven/net/neoforged/…）
        "official": ["https://maven.neoforged.net/releases",
                     "https://maven.neoforged.net"],
        "mirror": BMCLAPI_MAVEN,
    },
    {
        "key": "fabric",
        "group": "加载器",
        "name": "Fabric 版本信息",
        "desc": "Fabric 加载器版本与启动配置",
        "official": ["https://meta.fabricmc.net"],
        "mirror": BMCLAPI + "/fabric-meta",
    },
    {
        "key": "quilt",
        "group": "加载器",
        "name": "Quilt 版本信息",
        "desc": "Quilt 加载器版本与启动配置",
        "official": ["https://meta.quiltmc.org"],
        "mirror": BMCLAPI + "/quilt-meta",
    },
    # ---- 模组平台：这两家本来就在用（搜索 / 依赖查询 / 整合包下载），
    #      第 52 轮把它们也纳入分类，省得「明明在下载却找不到这个源」。
    #      第 104 轮只读实测：MCIMirror 的 **API 代理**可用且默认不启用；
    #      CDN 代理实测 302 回官方，不接入（MCIM_CDN_NOTE）。
    {
        "key": "modrinth_api",
        "group": "模组平台",
        "name": "Modrinth 接口",
        "desc": "搜索、项目详情、版本与依赖查询",
        "official": ["https://api.modrinth.com"],
        "mirror": "",
        "mcim": MCIM + "/modrinth",
    },
    {
        "key": "modrinth_cdn",
        "group": "模组平台",
        "name": "Modrinth 文件",
        "desc": "整合包与模组文件下载（cdn.modrinth.com）",
        "official": ["https://cdn.modrinth.com"],
        "mirror": "",
        "mcim": "",                 # 不接入，见 MCIM_CDN_NOTE
    },
]

GROUPS = ("游戏本体", "加载器", "模组平台")

KEYS = tuple(s["key"] for s in SOURCES)

_NET_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
               requests.exceptions.SSLError, requests.exceptions.ProxyError,
               requests.exceptions.ChunkedEncodingError)

_DEAD = set()                     # 本会话内被判「连不上」的域名
_SLOW = {}                        # 域名 -> 连续「响应慢」次数（第 52 轮）
_LAST = {"host": "", "mirror": False}


# ---------------------------------------------------------------- 配置
def _flag(key, default=False):
    """读一个布尔开关；配置坏了/没这个键就给默认值，绝不抛异常。"""
    try:
        from .config import CONFIG
        v = CONFIG.get(key, default)
    except Exception:                                  # noqa: BLE001
        return default
    if isinstance(v, str):
        return v.lower() in ("1", "true", "on", "yes")
    return bool(v) if v is not None else default


def mcim_enabled() -> bool:
    """MCIMirror 代理开关（默认关：第三方源，玩家自己点开才用）"""
    return _flag("mirror_mcim", False)


def set_mcim_enabled(on: bool) -> bool:
    from .config import CONFIG
    CONFIG.set("mirror_mcim", bool(on))
    reset_cache()
    return bool(on)


def race_enabled() -> bool:
    """错峰竞速开关（默认关：保留原来的「官方优先 + 自动降级」）"""
    return _flag("mirror_race", False)


def set_race_enabled(on: bool) -> bool:
    from .config import CONFIG
    CONFIG.set("mirror_race", bool(on))
    reset_cache()
    return bool(on)


def mcim_url(url) -> str:
    """官方 API URL → MCIMirror 代理 URL；没开开关 / 没映射返回空串。

    只对**实测可用**的两个 API 前缀做映射；CDN 两项实测是 302 回官方，
    这里故意不给它们映射（不硬编码不可用地址）。
    """
    if not mcim_enabled():
        return ""
    s = str(url or "")
    for pre, repl in MCIM_API_PREFIX.items():
        if s.startswith(pre):
            return repl + s[len(pre):]
    return ""


def _settings():
    from .config import CONFIG
    d = CONFIG.get("mirror_sources")
    if not isinstance(d, dict):
        d = {}
    items = d.get("items")
    if not isinstance(items, dict):
        items = {}
    default = str(d.get("default") or CONFIG.get("mirror_mode") or "auto")
    if default == "off":
        default = "official"
    if default not in MODES:
        default = "auto"
    # items 必须返回副本：直接改配置里的 dict 会「污染」调用方持有的旧快照
    return {"default": default, "items": dict(items)}


def save_settings(default=None, items=None):
    from .config import CONFIG
    cur = _settings()
    if default in MODES:
        cur["default"] = default
    if isinstance(items, dict):
        for k, v in items.items():
            if k in KEYS and v in MODES:
                cur["items"][k] = v
    CONFIG.set("mirror_sources", {"default": cur["default"], "items": cur["items"]})
    reset_cache()
    return cur


def default_mode():
    return _settings()["default"]


def set_default_mode(mode):
    return save_settings(default=mode)["default"]


def mode_for(key):
    """某个类别的实际策略：单类覆盖优先，否则用默认。"""
    st = _settings()
    m = st["items"].get(key)
    return m if m in MODES else st["default"]


def item_of_key(key):
    for s in SOURCES:
        if s["key"] == key:
            return s
    return None


def _match(url):
    """返回 (类别项, 命中的官方前缀)。最长前缀优先。"""
    best, best_pre = None, ""
    for s in SOURCES:
        for pre in s["official"]:
            if url.startswith(pre) and len(pre) > len(best_pre):
                best, best_pre = s, pre
    return best, best_pre


def mirror_of(url):
    """官方地址 -> 镜像地址（没有对应镜像返回 ''）"""
    s, pre = _match(url)
    if not s or not pre or not s.get("mirror"):
        return ""          # 空镜像（模组平台那几项）必须返回空，否则会拼出半个路径
    return s["mirror"] + url[len(pre):]


def official_of(url):
    """镜像地址 -> 官方地址（用于把用户填的镜像地址还原，当前未用到）"""
    for s in SOURCES:
        mir = s.get("mirror") or ""
        if mir and url.startswith(mir):
            return s["official"][0] + url[len(mir):]
    return ""


def mirror_candidates(url) -> list:
    """该 URL 的全部镜像候选（按优先级、去重）。

    第 104 轮：以前 `mirror_of()` 只能给一个，libraries 的 `/libraries`
    备选与 MCIMirror 的 API 代理都塞不进来。候选里**不含**官方地址本身，
    调用方 `candidates()` 决定先后。
    """
    s, pre = _match(url)
    if not s or not pre:
        return []
    out = []
    first = mirror_of(url)
    if first:
        out.append(first)
    for alt in (s.get("mirror_alt") or []):
        out.append(alt + url[len(pre):])
    proxy = mcim_url(url)
    if proxy:
        out.append(proxy)
    return list(dict.fromkeys(x for x in out if x and x != url))


# ---------------------------------------------------------------- 版本清单有效性（D5）
_VERSION_LIST_RE = re.compile(
    r"version_manifest|/versions\.json|/mc/game/versions", re.I)
_VERSION_NOTES = []
_OFFICIAL_MANIFEST = {"t": 0.0, "data": None}


def is_version_list(url) -> bool:
    return bool(_VERSION_LIST_RE.search(str(url or "")))


def validate_version_list(payload, required=()) -> tuple[bool, str]:
    """镜像返回的版本清单是不是有效、有没有玩家要装的版本。

    返回 (ok, 原因)。`required` 是版本 id 列表；空列表只验结构。
    """
    if not isinstance(payload, dict):
        return False, "版本清单不是 JSON 对象"
    vs = payload.get("versions")
    if not isinstance(vs, list) or not vs:
        return False, "版本清单里没有 versions 列表"
    if not isinstance(vs[0], dict) or not vs[0].get("id"):
        return False, "版本清单条目缺 id"
    ids = {str(v.get("id")) for v in vs if isinstance(v, dict)}
    miss = [str(r) for r in (required or ()) if r and str(r) not in ids]
    if miss:
        return False, "镜像版本清单缺版本：" + "、".join(miss[:3])
    return True, ""


def ensure_version_list(payload, required=(), official=None):
    """镜像清单无效 / 缺版本时用官方清单补齐，返回 (payload, 说明)。

    纯函数，不联网：官方清单由调用方取好传进来（response 或 dict 都吃），
    方便测试打桩，也避免「校验一下反而多打两次网」。
    """
    ok, why = validate_version_list(payload, required)
    if ok:
        return payload, ""
    off = official
    if hasattr(off, "json"):
        try:
            off = off.json()
        except Exception:                              # noqa: BLE001
            off = None
    if not validate_version_list(off)[0]:
        return payload, why + "；官方清单也不可用，未补齐"
    base = payload if isinstance(payload, dict) else {}
    ids = {str(v.get("id")) for v in (base.get("versions") or [])
           if isinstance(v, dict)}
    merged = dict(base)
    vs = [v for v in (base.get("versions") or []) if isinstance(v, dict)]
    added = 0
    for v in off["versions"]:
        if isinstance(v, dict) and str(v.get("id")) not in ids:
            vs.append(v)
            ids.add(str(v.get("id")))
            added += 1
    merged["versions"] = vs
    if not merged.get("latest") and off.get("latest"):
        merged["latest"] = off["latest"]
    return merged, f"{why}；已从官方清单补回 {added} 个版本"


def version_list_notes() -> list:
    return list(_VERSION_NOTES[-10:])


def _merge_version_list(r, url, timeout, get):
    """镜像返回的版本清单 → 校验 + 用官方清单补齐，改的是 response 的 body，
    对调用方（`.json()`）完全透明，不用改 runtime / loader 的任何一行。
    """
    try:
        data = r.json()
    except Exception:                                  # noqa: BLE001
        return r
    ok, why = validate_version_list(data)
    if ok:
        return r
    now = time.monotonic()
    off = _OFFICIAL_MANIFEST.get("data")
    if not off or now - float(_OFFICIAL_MANIFEST.get("t") or 0) > 300:
        off_host = url.split("/")[2] if url.count("/") >= 2 else ""
        if off_host in _DEAD:
            # 走到这里多半正是「官方超时 → 换镜像」那一轮，官方刚被 fetch()
            # 记进 _DEAD。再去补一次官方清单只会白等一整段超时且必然失败：
            # 直接把镜像结果交回去并记账，别为校验再打一次必死的请求。
            _VERSION_NOTES.append(f"{why}；官方源本会话已判不可用，未补齐")
            return r
        try:
            ro = get(url, timeout=timeout)
            ro.raise_for_status()
            off = ro.json()
            if validate_version_list(off)[0]:
                _OFFICIAL_MANIFEST.update(t=now, data=off)
        except Exception as e:                         # noqa: BLE001
            _VERSION_NOTES.append(f"{why}；官方清单也没取到（{str(e)[:50]}）")
            return r
    merged, note = ensure_version_list(data, (), off)
    if note:
        _VERSION_NOTES.append(note)
    try:
        body = json.dumps(merged, ensure_ascii=False).encode("utf-8")
        r._content = body
        try:
            r.headers["content-length"] = str(len(body))
        except Exception:                              # noqa: BLE001
            pass
    except Exception:                                  # noqa: BLE001
        pass
    return r


_MIRROR_SAMPLE = {
    "version": "bmclapi2.bangbang93.com",
    "libraries": "bmclapi2.bangbang93.com/maven",
    "assets": "bmclapi2.bangbang93.com/assets",
    "forge": "bmclapi2.bangbang93.com/maven",
    "neoforge": "bmclapi2.bangbang93.com/maven",
    "fabric": "bmclapi2.bangbang93.com/fabric-meta",
    "quilt": "bmclapi2.bangbang93.com/quilt-meta",
}


def mirror_host(key):
    return _MIRROR_SAMPLE.get(key, "")


def official_host(key):
    s = item_of_key(key)
    if not s:
        return ""
    return s["official"][0].replace("https://", "")


# ---------------------------------------------------------------- 解析
def candidates(url, mode=None, default=None):
    """按当前策略给出候选 URL 列表（官方/镜像的先后顺序）。

    mode：强制指定（忽略逐项设置）；default：仅覆盖「默认策略」，
    逐项设置仍然优先 —— 供 loader 这类既有「全局开关」语义的调用方使用。
    """
    s, _pre = _match(url)
    if s is None:
        return [url]
    mirs = mirror_candidates(url)
    if not mirs:
        return [url]
    if mode in MODES:
        m = mode
    else:
        st = _settings()
        m = st["items"].get(s["key"])
        if m not in MODES:
            m = default if default in MODES else st["default"]
    if m == "always":
        return list(mirs)
    if m == "official":
        return [url]
    host = url.split("/")[2] if url.count("/") >= 2 else url
    # 自动：官方这次连不上（_DEAD），或者连续几次都「连得上但很慢」（_SLOW），
    # 都直接走镜像。慢计数由 fetch() 在拿到响应后回填。
    if host in _DEAD or _SLOW.get(host, 0) >= SLOW_HITS:
        return list(mirs)
    return [url] + list(mirs)


def is_mirror(url):
    return bool(official_of(url)) and not _match(url)[1]


def last_used_mirror():
    return bool(_LAST.get("mirror"))


def dead_hosts():
    return sorted(_DEAD)


def slow_hosts():
    """本会话被判「响应慢」的域名（连续 >= SLOW_HITS 次）"""
    return sorted(h for h, n in _SLOW.items() if n >= SLOW_HITS)


def reset_cache():
    _DEAD.clear()
    _SLOW.clear()


# ---------------------------------------------------------------- 请求
def http_get(url, timeout=25, stream=False):
    return requests.get(url, headers={"User-Agent": UA}, timeout=timeout,
                        stream=stream)


def _read_timeout(timeout):
    """从调用方给的 timeout 里取出「读取超时」那一档"""
    try:
        if isinstance(timeout, (tuple, list)):
            return float(timeout[1])
        return float(timeout)
    except Exception:                                # noqa: BLE001
        return 20.0


def _short_timeout(timeout):
    """官方源用的分档超时：连接/首字节压到 5 秒，读取仍给足。

    这是「加载缓慢时换镜像」的主力：慢在连接或首字节上，5 秒就能判死并换源，
    不用干等 20~25 秒。
    """
    read = _read_timeout(timeout)
    return (min(_SHORT, read), read)


def _is_meta(r):
    """这次响应是不是「元数据」（json / xml / 短文本）。

    只有元数据才参与慢判定：大文件即使 `stream=False` 也要读完才返回，耗时天然
    很长，拿它判慢会把一切源都判死。
    """
    try:
        if r.request is not None and str(r.request.method).upper() == "HEAD":
            return True
        ct = (r.headers.get("content-type") or "").lower()
        if ct:
            return ("json" in ct) or ("xml" in ct) or ("text" in ct)
        return int(r.headers.get("content-length") or 0) < (512 << 10)
    except Exception:                                # noqa: BLE001
        return True


def _note_speed(host, dt, r):
    """回填一次请求的耗时：慢记一分，连续 SLOW_HITS 分就优先镜像；快则清零。

    `stream=True` 时这里拿到的是「响应头到手」的耗时（body 还没读），也就是 TTFB，
    与文件大小、与启动器限速都无关 —— 所以慢判定不可能被下载速度污染。
    """
    if not _is_meta(r):
        _SLOW.pop(host, None)
        return
    if dt >= SLOW_SECS:
        _SLOW[host] = _SLOW.get(host, 0) + 1
    else:
        _SLOW.pop(host, None)


def _race_fetch(cands, timeout, stream, get, host, official_url):
    """错峰竞速：官方先发，约 3 秒后镜像跟上，谁先成功用谁并取消其它。

    第 104 轮：PCL-CE `DlSourceLoader` 是 while + Thread.Sleep(10) 轮询 + 固定
    延迟表；这里用 `ThreadPoolExecutor + as_completed` 做同样的短路，代码更短、
    也不用自己维护状态机。默认**不启用**（开关 mirror_race），启用前仍是
    「官方优先 + 失败/慢自动降级」的老语义。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    done = threading.Event()

    def one(u, delay):
        if delay and done.wait(delay):
            raise RuntimeError("已有更快的源返回")
        if done.is_set():
            raise RuntimeError("已有更快的源返回")
        r = get(u, timeout=timeout, stream=stream)
        r.raise_for_status()
        if done.is_set():
            try:
                r.close()
            except Exception:                          # noqa: BLE001
                pass
            raise RuntimeError("已有更快的源返回")
        return r

    ex = ThreadPoolExecutor(max_workers=max(1, len(cands)))
    futs = {ex.submit(one, u, 0.0 if i == 0 else RACE_DELAY): (u, i == 0)
            for i, u in enumerate(cands)}
    last, winner = None, None
    try:
        for f in as_completed(futs):
            u, official = futs[f]
            try:
                r = f.result()
            except Exception as e:                     # noqa: BLE001
                last = e
                if official and isinstance(e, _NET_ERRORS):
                    _DEAD.add(host)
                continue
            done.set()
            winner = r
            _LAST.update(host=host, mirror=not official)
            break
    finally:
        for f in futs:
            f.cancel()
        ex.shutdown(wait=False)
    if winner is None:
        raise last if last is not None else RuntimeError("请求失败")
    # D5：赢的是镜像且这是版本清单 → 校验 + 用官方清单补齐缺失版本
    if _LAST.get("mirror") and is_version_list(official_url):
        winner = _merge_version_list(winner, official_url, timeout, get)
    return winner


def fetch(url, timeout=25, stream=False, mode=None, default=None, getter=None):
    """带镜像降级的 GET（抛异常与 requests 一致，便于上层沿用 except）。

    getter 可注入（loader 用它自己那个可被打桩的 _get），默认 requests.get。
    第 52 轮：官方源改用「连接 5 秒」的分档超时，并统计响应耗时 —— 连得上但慢的
    域名连续慢两次后，本会话「自动」模式直接走镜像。
    第 104 轮：可选错峰竞速（mirror_race）+ 镜像版本清单有效性校验/补齐。
    """
    get = getter or http_get
    cands = candidates(url, mode=mode, default=default)
    last = None
    host = url.split("/")[2] if url.count("/") >= 2 else url
    has_alt = len(cands) > 1
    if has_alt and race_enabled():
        return _race_fetch(cands, timeout, stream, get, host, url)
    for u in cands:
        official = (u == url)
        # 只有在「还有镜像可换」时才压官方超时，否则把用户的超时原样给它
        t = _short_timeout(timeout) if (official and has_alt) else timeout
        t0 = time.monotonic()
        try:
            r = get(u, timeout=t, stream=stream)
            r.raise_for_status()
            dt = time.monotonic() - t0
            _LAST.update(host=host, mirror=not official)
            _note_speed(host, dt, r)
            if not official and is_version_list(url):
                r = _merge_version_list(r, url, timeout, get)
            return r
        except Exception as e:                       # noqa: BLE001
            last = e
            if official and isinstance(e, _NET_ERRORS):
                _DEAD.add(host)                      # 官方源废了，本会话不再试
    raise last if last is not None else RuntimeError("请求失败")


def speed_hint():
    """给设置页显示的一句话状态（不联网，只反映本会话观察到的结果）"""
    parts = []
    dead = dead_hosts()
    if dead:
        parts.append("已跳过（不可达）：" + "、".join(dead))
    slow = slow_hosts()
    if slow:
        parts.append("已改用镜像（响应慢）：" + "、".join(slow))
    return "；".join(parts)
