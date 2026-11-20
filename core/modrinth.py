# -*- coding: utf-8 -*-
"""Modrinth API 客户端（无需密钥）"""
import json
import re
import threading
import time

import requests

UA = "MC-GuanQi-Fuzhu-Gongju/1.0.0 (Windows; +https://workbuddy.cn)"
# 国内到 api.modrinth.com 的连接本身正常（握手 ~1s），但偶尔会停顿十几秒。
# 读超时给 15s、失败自动重试 2 次，比「一次超时整页报错」体验好得多。
TIMEOUT = (8, 15)
RETRY = 2

API = "https://api.modrinth.com/v2"

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept": "application/json"})

# ---------------------------------------------------------------- 缓存
_cache_lock = threading.Lock()
_cache = {}


def _cached(key, fn):
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    v = fn()
    with _cache_lock:
        _cache[key] = v
    return v


# ---------------------------------------------------------------- 基础
def _net_error(exc):
    """是不是「重试可能成功」的网络类错误"""
    if isinstance(exc, (requests.exceptions.Timeout,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.ChunkedEncodingError,
                        requests.exceptions.ProxyError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        code = getattr(exc.response, "status_code", 0) or 0
        return code == 429 or code >= 500
    return False


def friendly_error(exc):
    """把底层异常翻成给用户看的一句话。

    注意：`run_async` 的 on_err 传过来的是 **str(exc)**（不是异常对象），
    所以这里必须同时吃字符串 —— 否则中文翻译全部落空，界面直接显示原始英文。
    """
    msg = str(exc)
    low = msg.lower()
    if isinstance(exc, requests.exceptions.Timeout) or "timed out" in low:
        return "请求超时：网络当前不稳定，稍后重试即可"
    if isinstance(exc, (requests.exceptions.ConnectionError,
                        requests.exceptions.ProxyError)) or \
            "connection" in low or "proxy" in low or "max retries" in low:
        return "连不上服务器：请检查网络或代理设置"
    code = getattr(getattr(exc, "response", None), "status_code", 0) or 0
    if not code:                                        # 字符串入参：从文案里抠状态码
        m = re.search(r"\b([45]\d\d)\b", msg)
        code = int(m.group(1)) if m else 0
    if code == 403 or code == 401:
        return "服务端拒绝了这次请求（HTTP 403）：可能被限流，稍后重试"
    if code == 400:
        return "服务端拒绝了请求参数（HTTP 400）：接口要求变动，请更新到新版本"
    if code == 404:
        return "接口或内容不存在（HTTP 404）"
    if code == 429:
        return "请求太频繁被限流了，等十几秒再试"
    if code >= 500:
        return "服务器繁忙，请稍后重试"
    return msg[:120]


def _mirror_alts(url):
    """第 104 轮：可选的 MCIMirror API 代理（默认关）。

    只作**官方失败后的换源候选**，语义仍是「官方优先 + 自动降级」；
    开关在「设置 → 下载镜像源」，不点开时这个函数永远返回空列表。
    CDN 类 URL 实测是 302 回官方，mirror.mcim_url 不会给映射。
    """
    try:
        from . import mirror
        p = mirror.mcim_url(url)
        return [p] if p and p != url else []
    except Exception:                                  # noqa: BLE001
        return []


def _get(url, timeout=None, **kw):
    """带退避重试的 GET"""
    last = None
    for u in [url] + _mirror_alts(url):
        for attempt in range(RETRY + 1):
            try:
                r = _session.get(u, timeout=timeout or TIMEOUT, **kw)
                r.raise_for_status()
                return r
            except Exception as e:                      # noqa: BLE001
                last = e
                if not _net_error(e) or attempt >= RETRY:
                    break                               # 换下一个源再试
                time.sleep(0.8 * (attempt + 1))         # 退避后再试
    raise last


def _post(url, payload, timeout=None, **kw):
    """带退避重试的 POST（返回刚拿到 update 版本信息时要发 body）"""
    last = None
    for u in [url] + _mirror_alts(url):
        for attempt in range(RETRY + 1):
            try:
                r = _session.post(u, json=payload, timeout=timeout or TIMEOUT, **kw)
                r.raise_for_status()
                return r
            except Exception as e:                      # noqa: BLE001
                last = e
                if not _net_error(e) or attempt >= RETRY:
                    break
                time.sleep(0.8 * (attempt + 1))
    raise last


def version_files_update(hashes, loaders=None, game_versions=None,
                         algorithm="sha1", timeout=None):
    """批量查「这些文件哈希各自的最新兼容版本」（第八十九轮）。

    POST /v2/version_files/update：body = {hashes, algorithm, loaders, game_versions}，
    返回 `{ "<hash>": <version 对象> }`（只含 Modrinth 认识的文件）。
    这是「不看来源、只认文件本身」的关键 —— 文件从哪来的都行。
    """
    hashes = [str(h) for h in (hashes or []) if h]
    if not hashes:
        return {}
    body = {"hashes": hashes, "algorithm": str(algorithm or "sha1")}
    if loaders:
        body["loaders"] = list(loaders)
    if game_versions:
        body["game_versions"] = list(game_versions)
    return _post(f"{API}/version_files/update", body, timeout=timeout).json() or {}


# ---------------------------------------------------------------- 标签
_CN_CATEGORY = {
    "technology": "科技", "magic": "魔法", "adventure": "冒险",
    "worldgen": "世界生成", "optimization": "优化", "utility": "实用",
    "multiplayer": "多人", "quests": "任务", "storage": "存储",
    "food": "食物", "equipment": "装备", "combat": "战斗",
    "economy": "经济", "gameplay": "玩法", "social": "社交",
    "transportation": "交通", "kitchen-sink": "大杂烩", "library": "前置库",
    "challenging": "挑战", "cursed": "诅咒", "decoration": "装饰",
    "education": "教育", "performance": "性能", "mobs": "生物",
    " exploration": "探索", "exploration": "探索", "processing": "加工",
    "colors": "色彩", "landscape": "地形", "atmosphere": "氛围",
    # 光影
    "iris": "Iris", "canvas": "Canvas", "optifine": "OptiFine",
    "vanilla-plus": "原版增强", "potency": "强化画质", "cartoon": "卡通",
    "semi-realistic": "半写实", "realistic": "写实", "fantasy": "奇幻",
    "special": "特效", "colored-lighting": "彩色光照", "path-tracing": "路径追踪",
    # 材质包
    "8x": "8x", "16x": "16x", "32x": "32x", "64x": "64x", "128x": "128x",
    "256x": "256x", "512x": "512x", "simplistic": "简约", "themed": "主题",
    "vanilla-like": "原版风", "mod-support": "模组支持", "font": "字体",
    "gui": "界面", "items": "物品", "models": "模型", "environment": "环境",
    "tweaks": "微调", "religious": "宗教", "audio": "音效",
}


def cn_category(slug: str) -> str:
    return _CN_CATEGORY.get(slug, slug)


def fetch_categories(project_type: str):
    """拉取 Modrinth 分类标签，返回 [(slug, 中文名)]"""
    def _do():
        data = _get(f"{API}/tag/category").json()
        out = []
        for c in data:
            if c.get("project_type") == project_type and c.get("display") != "Hidden":
                slug = c["name"]
                out.append((slug, cn_category(slug)))
        out.sort(key=lambda x: x[1])
        return out
    try:
        return _cached("cats:" + project_type, _do)
    except Exception:
        return []


def fetch_game_versions():
    def _do():
        data = _get(f"{API}/tag/game_version").json()
        releases, snapshots, olds = [], [], []
        for v in data:
            item = v["version"]
            vt = v.get("version_type")
            if vt == "release":
                # 1.x 主版本判断，粗略分新旧
                releases.append(item)
            else:
                snapshots.append(item)
        def sv(x):
            try:
                p = [int(i) for i in x.split(".") if i.isdigit() or i.isdigit() or True]
                return [int(i) if i.isdigit() else -1 for i in p]
            except Exception:
                return [-1]
        releases.sort(key=sv, reverse=True)
        return {"release": releases, "snapshot": snapshots[:80]}
    try:
        return _cached("gvers", _do)
    except Exception:
        return {"release": [], "snapshot": []}


# ---------------------------------------------------------------- 搜索
SORT_MAP = {"relevance": "relevance", "downloads": "downloads",
            "follows": "follows", "newest": "newest"}


def search(project_type: str, query: str = "", categories=None, versions=None,
           loaders=None, index="relevance", limit=24, offset=0):
    """搜索项目。返回 {hits:[...], total:int}"""
    facets = [[f"project_type:{project_type}"]]
    if categories:
        facets.append([f"categories:{c}" for c in categories])
    if loaders:
        facets.append([f"categories:{l}" for l in loaders])
    if versions:
        facets.append([f"versions:{v}" for v in versions])
    params = {
        "limit": limit,
        "offset": offset,
        "index": SORT_MAP.get(index, "relevance"),
        "facets": json.dumps(facets),
    }
    if query.strip():
        params["query"] = query.strip()
    data = _get(f"{API}/search", params=params).json()
    # Modrinth 的总数字段叫 total_hits（不是 total / total_hidden）。
    # 读错字段会让总数恒为 0，翻页上限永远算成 1 页、两个箭头全禁用。
    total = (data.get("total_hits") or data.get("total") or
             data.get("total_hidden") or 0)
    return {"hits": data.get("hits", []), "total": int(total)}


def get_project(project_id: str):
    return _get(f"{API}/project/{project_id}").json()


def get_versions(project_id: str, loaders=None, game_versions=None):
    params = {}
    if loaders:
        params["loaders"] = json.dumps(loaders)
    if game_versions:
        params["game_versions"] = json.dumps(game_versions)
    return _get(f"{API}/project/{project_id}/version", params=params).json()


def best_file_for(project_id: str, loaders=None, game_versions=None):
    """自动匹配最新兼容版本文件，返回 (version, file_url, filename) 或 None"""
    vers = get_versions(project_id, loaders=loaders, game_versions=game_versions)
    if not vers:
        vers = get_versions(project_id)
    if not vers:
        return None
    ver = vers[0]
    files = ver.get("files") or []
    primary = None
    for f in files:
        if f.get("primary"):
            primary = f
            break
    if primary is None and files:
        primary = files[0]
    if primary is None:
        return None
    return ver, primary.get("url"), primary.get("filename")


# ---------------------------------------------------------------- 往期版本
# 第五十八轮：详情页要列出「往期版本」并能选着下。
# 这里统一成一种「展示用的行」，界面只管排版：
#   {label, mc, loader, date, size, channel, src, raw}
# `raw` 原样带走，下载时按 src 回去取文件（Modrinth 是 version dict，
# 中间不丢信息。
_SRC_MR = "modrinth"
_CHANNEL_CN = {"release": "正式", "beta": "测试", "alpha": "内测",
               1: "正式", 2: "测试", 3: "内测"}


def _short_date(s) -> str:
    """2024-05-01T12:33:00Z / 1714566780000 → 2024-05-01"""
    t = str(s or "")
    if t.isdigit() and len(t) >= 12:              # CF 给的是毫秒时间戳
        try:
            return time.strftime("%Y-%m-%d", time.localtime(int(t) / 1000))
        except (OSError, ValueError):
            return ""
    return t[:10]


def _loader_of(names) -> str:
    """加载器名列表 → 一句话（forge/fabric/neoforge/quilt…）"""
    keep = []
    for n in names or []:
        v = str(n or "").strip().lower()
        if v in ("forge", "neoforge", "fabric", "quilt", "liteloader", "rift"):
            if v not in keep:
                keep.append(v)
    return " / ".join(keep)


def _mr_rows(project_id: str, limit: int, mc: str = "", loader: str = "") -> list:
    # 先按当前游戏版本/加载器筛（这样「往期版本」里看到的都能直接装），
    # 筛完一个都不剩再退回全量 —— 否则小项目会看起来「没有版本」。
    vers = []
    if mc or loader:
        try:
            vers = get_versions(project_id,
                                loaders=[loader] if loader else None,
                                game_versions=[mc] if mc else None) or []
        except Exception:                          # noqa: BLE001
            vers = []
    if not vers:
        vers = get_versions(project_id) or []
    rows = []
    for v in vers[:max(1, limit)]:
        files = v.get("files") or []
        if not files:
            continue
        primary = next((f for f in files if f.get("primary")), files[0])
        rows.append({
            "label": str(v.get("version_number") or v.get("name") or "?"),
            "mc": ", ".join((v.get("game_versions") or [])[:3]),
            "loader": _loader_of(v.get("loaders")),
            "date": _short_date(v.get("date_published")),
            "size": int(primary.get("size") or 0),
            "channel": str(v.get("version_type") or "release"),
            "src": _SRC_MR,
            "raw": {"version": v, "file": primary},
        })
    return rows


def version_rows(hit, api_key: str = "", limit: int = 60, mc: str = "",
                 loader: str = "") -> list:
    """取某个项目的版本（新的在前），走 Modrinth /version。

    `api_key` 形参仅为兼容旧调用方保留（忽略即可）。
    `mc` / `loader` 会给接口做一次过滤；过滤后一个都不剩时自动退回全量。
    查不到就返回空表 —— 详情页显示「没拿到版本列表」，绝不塞假数据。
    """
    if not isinstance(hit, dict):
        return []
    try:
        rows = _mr_rows(hit.get("project_id") or hit.get("id") or "",
                        limit, mc, loader)
    except Exception:                              # noqa: BLE001
        return []
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    return rows


def channel_cn(ch) -> str:
    return _CHANNEL_CN.get(ch, str(ch or ""))


def download_to(url: str, dest: str, progress_cb=None, cancel_flag=None,
                expected_size=0):
    """下载一个文件（并发分段引擎，见 core/dl.py）。

    progress_cb(done_bytes, total_bytes|0)
    """
    from . import dl
    return dl.download(url, dest, progress_cb=progress_cb,
                       cancel_flag=cancel_flag, expected_size=expected_size)
