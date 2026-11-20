# -*- coding: utf-8 -*-
"""BBSMC（bbsmc.net）接入：中文名 + 中文搜索 + 整合包汉化补丁。

为什么要有这个模块
------------------
Modrinth 是英文站，`title` / `description` 全英文。BBSMC 是国内
社区做的中文站，它的 `api.bbsmc.net/v2` 是 **Modrinth v2 的兼容实现** ——
字段名一模一样（`project_id` / `slug` / `title` / `description` / `versions` /
`files[].url` / `files[].filename` / `files[].hashes`），区别只有两处：

1. `title` 是「中文名 - 英文名」，`description` 是一句中文；
2. `slug` 与本家站同源（Modrinth 的项目用同一个 slug）。

于是同一份数据能干三件事：

* **中文名**：按 slug 归一化对上我们自己的搜索结果，把中文标题/简介贴上去；
* **中文搜索**：玩家输入的中文词直接问 BBSMC，它命中的 `slug` / 英文名就是
  该拿去 Modrinth 搜的英文关键词（补 `cnsearch.py` 词表的漏）；
* **整合包汉化**：`project_type=language` 那批项目。汉化版本的
  `version_number` 形如 `9366926-zh_CN-f4036137`，前面那段数字正是
  **整合包版本的 version_number**——
  「按所选整合包版本下对应汉化」就是靠它对上的。

三条铁律（跟 `cnnames.py` 一致，别破）
------------------------------------
* **查不到就返回空**，绝不机翻、绝不猜 —— 给玩家一个错的中文名比给英文更糟；
* **网络问题只让功能降级，绝不让启动器报错**：每个对外函数都用 try/except
  兜住，失败返回空值，界面上只是「没中文名 / 汉化选项是灰的」；
* **缓存永远可以删**：`<数据目录>/bbsmc_cn.json` 删掉下次自己长回来。

和前两个模块的分工
------------------
`cnsearch.py` = 中文 → 英文查询词（**内置**词表，离线）
`cnnames.py`  = 英文项目 → 中文名（**内置**词库，离线）
`bbsmc.py`    = 上面两件事的**联网脏活 + 缓存**，外加汉化补丁
三者都只管各自那份，不互相 import（`cnnames` 单向查本模块的缓存）。
"""
import json
import os
import re
import threading
import time

import requests

API = "https://api.bbsmc.net/v2"
SITE = "https://bbsmc.net"
TIMEOUT = (6, 18)

# api 不挑 UA，但 cdn.bbsmc.net 对陌生 UA 会吐人机验证页（实测 curl 默认 UA
# 只拿回 55 字节的 `/_guard/html.js`），所以下载一律用浏览器 UA。
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
UA_API = "MC-GuanQi-Fuzhu-Gongju/1.0.0 (Windows; +https://workbuddy.cn)"

# 启动器自己的类型 → BBSMC 的 project_type（两边名字基本一样）
PT_MAP = {"modpack": "modpack", "mod": "mod", "datapack": "datapack",
          "shader": "shader", "resourcepack": "resourcepack",
          "language": "language"}
PT_CN = {"modpack": "整合包", "mod": "模组", "datapack": "数据包",
         "shader": "光影", "resourcepack": "材质包", "language": "汉化"}

_session = requests.Session()
_session.headers.update({"User-Agent": UA_API, "Accept": "application/json"})

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def has_cjk(text) -> bool:
    return bool(_CJK.search(str(text or "")))


def _norm(s) -> str:
    """归一化：只留字母数字的小写串（跟 `cnnames._norm` 同一个口径）"""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


# ================================================================ HTTP
def _get(path: str, params=None, timeout=None):
    """GET 一个 api 路径，返回解析后的 JSON。失败**抛异常**（由上层兜）。"""
    r = _session.get(API + path, params=params,
                     timeout=timeout or TIMEOUT)
    r.raise_for_status()
    return r.json()


def search(query: str, ptype: str = "", limit: int = 20, offset: int = 0) -> dict:
    """搜 BBSMC。返回 {"hits": [...], "total_hits": n}，失败返回空表。

    `ptype` 给启动器的类型名（modpack/mod/...），空串 = 不限类型。
    """
    params = {"query": str(query or ""), "limit": max(1, min(50, int(limit))),
              "offset": max(0, int(offset))}
    pt = PT_MAP.get(str(ptype or "").strip())
    if pt:
        params["facets"] = json.dumps([[f"project_type:{pt}"]])
    try:
        d = _get("/search", params)
    except Exception:                                  # noqa: BLE001
        return {"hits": [], "total_hits": 0}
    if not isinstance(d, dict):
        return {"hits": [], "total_hits": 0}
    hits = [h for h in (d.get("hits") or []) if isinstance(h, dict)]
    try:
        total = int(d.get("total_hits") or d.get("total") or len(hits))
    except Exception:                                  # noqa: BLE001
        total = len(hits)
    return {"hits": hits, "total_hits": total}


def project(ref: str) -> dict:
    """按 slug 或 project_id 取单个项目；查不到返回 {}"""
    ref = str(ref or "").strip()
    if not ref:
        return {}
    try:
        d = _get("/project/" + requests.utils.quote(ref, safe=""))
    except Exception:                                  # noqa: BLE001
        return {}
    return d if isinstance(d, dict) else {}


def versions(ref: str) -> list:
    """项目版本列表（新的在前）；查不到返回 []"""
    ref = str(ref or "").strip()
    if not ref:
        return []
    try:
        d = _get("/project/" + requests.utils.quote(ref, safe="") + "/version")
    except Exception:                                  # noqa: BLE001
        return []
    return [v for v in d if isinstance(v, dict)] if isinstance(d, list) else []


def version(vid: str) -> dict:
    vid = str(vid or "").strip()
    if not vid:
        return {}
    try:
        d = _get("/version/" + requests.utils.quote(vid, safe=""))
    except Exception:                                  # noqa: BLE001
        return {}
    return d if isinstance(d, dict) else {}


# ================================================================ 标题拆解
_ASCII_HEAD = re.compile(r"[A-Za-z0-9][A-Za-z0-9 :'&,.!?_+()\-]*")


def ascii_head(text) -> str:
    """取一串文字里开头的英文部分：`Deceasedcraft 现代都市僵尸启示录`
    → `Deceasedcraft`。

    BBSMC 的标题经常「英文名 + 中文副标题」混在一段里（`亡者世界 - Deceasedcraft
    现代都市僵尸启示录`），直接拿去 Modrinth 搜会把那串中文当关键词一起发出去，
    命中率反而掉下来。所以只留开头的英文词。
    """
    s = str(text or "").strip()
    m = _ASCII_HEAD.match(s)
    return (m.group(0) if m else "").strip(" -:,")


def split_title(title) -> tuple:
    """BBSMC 的 `title` 拆成 (中文名, 英文名)。

    `钠 - Sodium`                    → ("钠", "Sodium")
    `ATM10:空岛 - All the Mods 10...` → ("ATM10:空岛", "All the Mods 10...")
    `亡者世界 Deceasedcraft 汉化包`    → ("亡者世界 Deceasedcraft 汉化包", "")
    `All the Mods 10 - ATM10`        → ("", "All the Mods 10 - ATM10")

    规则很简单：先按 `" - "` 拆两段，第一段有中文就用第一段；否则整串有中文
    就用整串。**没有中文就返回空**，让调用方老实显示英文。
    """
    s = str(title or "").strip()
    if not s:
        return "", ""
    if " - " in s:
        head, _, tail = s.partition(" - ")
        head, tail = head.strip(), tail.strip()
        if head and has_cjk(head):
            return head, tail
        if tail and has_cjk(tail) and not has_cjk(head):
            return tail, head
    return (s, "") if has_cjk(s) else ("", s)


def _clean_desc(text) -> str:
    """简介压成一行（BBSMC 的 description 常带换行和 markdown 链接）"""
    s = str(text or "")
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)          # 图片
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)      # 链接留文字
    s = re.sub(r"\s+", " ", s)
    return s.strip()[:160]


# ================================================================ 中文名缓存
# 结构：{"v": 1, "items": {归一化键: [中文名, 中文简介, 写入时间]}}
# 键同时写三份（slug / 英文标题 / project_id），查的时候哪个对得上用哪个。
_CACHE_NAME = "bbsmc_cn.json"
_CACHE_TTL = 30 * 24 * 3600        # 30 天没被再命中就丢（站上信息会变）
_c_lock = threading.Lock()
_c_mem = None


def cache_path() -> str:
    from .config import app_data_dir
    return os.path.join(app_data_dir(), _CACHE_NAME)


def _load() -> dict:
    global _c_mem
    with _c_lock:
        if _c_mem is not None:
            return _c_mem
        data = {}
        try:
            p = cache_path()
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict) and isinstance(raw.get("items"), dict):
                    for k, v in raw["items"].items():
                        if isinstance(v, (list, tuple)) and len(v) >= 2:
                            item = [str(v[0] or ""), str(v[1] or "")]
                            if len(v) > 2:
                                item.append(float(v[2] or 0))
                            data[str(k)] = item
        except Exception:                              # noqa: BLE001
            data = {}
        _c_mem = data
        return _c_mem


def _save() -> None:
    """落盘（先写临时文件再替换，写一半断电不会把整份缓存写坏）"""
    try:
        p = cache_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with _c_lock:
            payload = {"v": 1, "items": dict(_c_mem or {})}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:                                  # noqa: BLE001
        pass


def reload_cache():
    """丢掉内存里的缓存（删了文件 / 想强制重读时用）"""
    global _c_mem
    with _c_lock:
        _c_mem = None
    return _load()


def _keys_of(hit) -> list:
    """给一条 BBSMC 命中项算出它所有的可查键"""
    out = []
    for k in ("slug", "project_id", "id"):
        v = _norm(hit.get(k))
        if v:
            out.append(v)
    _cn, en = split_title(hit.get("title"))
    for cand in (en, hit.get("title"), hit.get("name")):
        v = _norm(cand)
        if v and v not in out:
            out.append(v)
    return out


def remember(hits) -> int:
    """把一批 BBSMC 命中项写进缓存，返回真正收录的条数。

    只收「有中文名」的：英文项收进来没有任何用处，白占空间。
    """
    if not hits:
        return 0
    data = _load()
    now = time.time()
    n = 0
    with _c_lock:
        for h in hits:
            if not isinstance(h, dict):
                continue
            cn, en = split_title(h.get("title"))
            if not cn:
                continue
            desc = _clean_desc(h.get("description"))
            for k in _keys_of(h):
                data[k] = [cn, desc, now]
            n += 1
    if n:
        _save()
    return n


def lookup(hit) -> tuple:
    """按我们自己的搜索结果项查中文名，返回 (中文名, 中文简介)，查不到 ("","")。

    依次试 `slug` / `title` / `name` / `id` —— 与 `cnnames.lookup` 同一个顺序，
    因为最终是它在后面兜底（见 `cnnames.lookup` 的第三层）。
    """
    if not isinstance(hit, dict):
        return "", ""
    data = _load()
    if not data:
        return "", ""
    keys = []
    for k in ("slug", "title", "name", "project_id", "id"):
        v = _norm(hit.get(k))
        if v:
            keys.append(v)
    # CF 的 title 带版本尾巴（`All the Mods 10 - ATM10`），再试一次去掉尾巴的
    t = str(hit.get("title") or "")
    if " - " in t:
        v = _norm(t.split(" - ")[0])
        if v:
            keys.append(v)
    for k in keys:
        v = data.get(k)
        if v and v[0]:
            return v[0], (v[1] if len(v) > 1 else "")
    return "", ""


def _prune() -> int:
    """清掉太久没被命中、且已经过期的条目，返回删掉的条数"""
    data = _load()
    cut = time.time() - _CACHE_TTL
    dead = [k for k, v in data.items()
            if len(v) > 2 and float(v[2] or 0) < cut]
    if dead:
        with _c_lock:
            for k in dead:
                data.pop(k, None)
        _save()
    return len(dead)


def cache_size() -> int:
    return len(_load())


# ================================================================ 补中文名（显示用）
def fill_cn(hits, query: str = "", ptype: str = "") -> dict:
    """给一批搜索结果补中文名 —— **后台线程调用**，返回补上的条数统计。

    做法：拿这批结果自己的 `slug` / 英文标题当查询词去 BBSMC 搜一遍（一次请求），
    命中的收进缓存；缓存本来就有的直接算命中。调用方（`browse._render_hits`）
    拿到返回后让卡片自己 `refresh_cn()` 读缓存即可 —— 卡片不直接碰这个模块的
    返回值，省得两边对不齐。

    「一次请求」怎么够：BBSMC 的 search 是全文检索，`query=all the mods 10`
    这种英文串命中率很高；一页 24 条里通常能补上大半。补不上的保持英文，
    绝不猜。
    """
    stat = {"asked": 0, "hit": 0, "added": 0}
    if not hits:
        return stat
    stat["asked"] = len(hits)
    # ① 缓存已有的先算命中（离线也有中文名）
    for h in hits:
        if isinstance(h, dict) and lookup(h)[0]:
            stat["hit"] += 1
    # ② 联网补：优先用玩家自己输入的词（最准），不行再用第一条结果的标题
    q = str(query or "").strip()
    if not q:
        for h in hits:
            if isinstance(h, dict) and h.get("title"):
                q = str(h["title"])
                break
    if not q:
        return stat
    try:
        got = search(q, ptype=ptype, limit=50)
        stat["added"] = remember(got.get("hits") or [])
    except Exception:                                  # noqa: BLE001
        pass
    if stat["added"]:
        stat["hit"] = sum(1 for h in hits
                          if isinstance(h, dict) and lookup(h)[0])
    return stat


def cn_query_terms(text: str, ptype: str = "", limit: int = 3) -> list:
    """中文串 → 能拿去 Modrinth 搜的英文关键词列表（**可能为空**）。

    这是 `cnsearch.py` 内置词表覆盖不到时的兜底：玩家搜「亡者世界」，词表里
    没有，就问 BBSMC，它返回 slug `deceasedcraft`，于是拿 `deceasedcraft`
    去英文站搜 —— 这条路对「整句中文搜不到」特别有效。
    """
    raw = str(text or "").strip()
    if not raw or not has_cjk(raw):
        return []
    got = search(raw, ptype=ptype, limit=12)
    hits = got.get("hits") or []
    remember(hits)
    out = []

    def push(s):
        s = str(s or "").replace("-", " ").strip()
        s = re.sub(r"\s+", " ", s)
        if s and s.lower() not in (x.lower() for x in out):
            out.append(s)

    for h in hits:
        # 英文标题优先：Modrinth 是全文检索，玩家认得出的英文名
        # 命中率最高；slug 只在没有英文名时兜底（拼音缩写那种 `cfgj` 基本没用，
        # 但 `deceasedcraft` 这种 slug 本身就是英文名，很有用）。
        en = ascii_head(split_title(h.get("title"))[1])
        if en:
            push(en)
        push(h.get("slug"))
        if len(out) >= max(1, int(limit)):
            break
    return out[:max(1, int(limit))]


# ================================================================ 整合包汉化
# BBSMC 的汉化项目 slug 惯例是 `<整合包 slug>-cn`，但个人汉化不一定（例如
# `all-the-mods-10-to-the-sky-`、`atm-syy-mh`），所以先按惯例猜一次，再退到检索。
_LANG_SUFFIX = ("-cn", "-zh-cn", "-chinese", "-hans")


def _looks_like_lang_of(proj: dict, pack) -> bool:
    """判断这个汉化项目是不是真的对应这个整合包（而不是同系列别的包）。

    **只认精确相等**（去掉 `-cn` 之类后缀之后）。这一条刻意收得很紧：

    · 用 `startswith` 会让 `all-the-mods-10-sky-cn`（ATM10 **空岛**）认领到
      `all-the-mods-10`（ATM10 本体）头上 —— 两个是完全不同的整合包，把空岛
      的汉化盖进本体，配置和任务文件全乱；
    · 代价是少数 slug 起得很随意的个人汉化（`atm-syy-mh`）认不出来，那边
      汉化按钮就是灰的，玩家自己去站上下 —— **宁可漏，不可错**。

    错的汉化会覆盖掉玩家真实的游戏文件，漏的只是少一个便利按钮。
    """
    if not proj:
        return False
    # 先按**原始 slug** 剥后缀再归一化：归一化会把 `-` 一起吃掉，
    # 之后 `endswith("-cn")` 永远不成立（`all-the-mods-10-cn` 会变成
    # `allthemods10cn`，和包 slug 的 `allthemods10` 对不上）。
    raw = str(proj.get("slug") or "").strip().lower()
    if not raw:
        return False
    core_raw = raw
    for suf in _LANG_SUFFIX:
        if core_raw.endswith(suf):
            core_raw = core_raw[: -len(suf)]
            break
    core = _norm(core_raw)
    if not core:
        return False
    # 汉化 slug 去掉后缀之后必须与包 slug / 标题**整串相等**，不做包含判断。
    cands = {core}
    want = set()
    for k in ("slug", "title", "name"):
        v = _norm(pack.get(k))
        if v:
            want.add(v)
    t = str(pack.get("title") or pack.get("name") or "")
    if " - " in t:
        want.add(_norm(t.split(" - ")[0]))
    if not want:
        return False
    return bool(cands & want)


def find_language(pack) -> dict:
    """找这个整合包的汉化项目，返回 BBSMC 项目 dict；没有就返回 {}。

    `pack` 是我们自己的搜索结果项（含 `slug` / `title` / `_cf`）。
    """
    if not isinstance(pack, dict):
        return {}
    slug = str(pack.get("slug") or "").strip()
    title = str(pack.get("title") or pack.get("name") or "").strip()
    # ① 直接按 `<slug>-cn` 猜（BBSMC 官方汉化组的惯例，最快且零误伤）
    if slug:
        for suf in _LANG_SUFFIX:
            p = project(slug + suf)
            if p and str(p.get("project_type") or "") == "language":
                return p
    # ② 检索：拿英文名去 language 类型里搜，逐条校验 slug 归属
    _cn, en = split_title(title)
    queries = []
    for q in (en, slug.replace("-", " ") if slug else "", title):
        q = str(q or "").strip()
        if q and not has_cjk(q) and q not in queries:
            queries.append(q)
    for q in queries[:2]:
        got = search(q, ptype="language", limit=20)
        for h in got.get("hits") or []:
            cand = project(str(h.get("slug") or "")) or h
            if str(cand.get("project_type") or h.get("project_type") or "") != "language":
                continue
            if _looks_like_lang_of(cand, pack):
                return cand
    return {}


def language_versions(lang: dict) -> list:
    """汉化项目的全部版本（新的在前），每个版本补上：

    · `join_vn`  —— 关联的整合包 version_number（从 `version_links` 里取）
    · `head_vn`  —— 自己 `version_number` 的前段（`9366926-zh_CN-xxx` → `9366926`）
    · `file`     —— 首选文件（含 url / filename / hashes）
    · `ver_text` —— 从名字里抠出来的版本号（`All the Mods 10-8.4 ...` → `8.4`）
    """
    if not isinstance(lang, dict):
        return []
    ref = str(lang.get("slug") or lang.get("id") or "").strip()
    rows = versions(ref)
    if not rows and lang.get("versions"):
        # 项目详情里带了版本 id 列表，逐个取（接口偶尔不给 /version）
        rows = [v for v in (version(str(x)) for x in lang["versions"]) if v]
    out = []
    for v in rows:
        files = [f for f in (v.get("files") or []) if isinstance(f, dict)]
        if not files:
            continue
        primary = next((f for f in files if f.get("primary")), files[0])
        vn = str(v.get("version_number") or "")
        links = v.get("version_links") or []
        join = ""
        for lk in links:
            if isinstance(lk, dict) and lk.get("joining_version_id"):
                join = str(lk["joining_version_id"])
                break
        out.append({
            "id": str(v.get("id") or ""),
            "name": str(v.get("name") or ""),
            "version_number": vn,
            "head_vn": vn.split("-")[0].strip(),
            "join_vn": join,
            "date": str(v.get("date_published") or ""),
            "downloads": v.get("downloads") or 0,
            "file": primary,
            "file_name": str(primary.get("filename") or ""),
            "file_url": str(primary.get("url") or ""),
            "file_size": int(primary.get("size") or 0),
            "ver_text": _ver_text(v.get("name") or vn),
        })
    return out


_NUM_VER = re.compile(r"\d+(?:\.\d+)+")


def _ver_text(text) -> str:
    """从一串文字里抠出版本号（`All the Mods 10-8.4 汉化包` → `8.4`）

    只认 `x.y` 起的写法：光「10」这种大概率是「mods 10」里的系列号，不是版本。
    """
    for m in _NUM_VER.finditer(str(text or "")):
        return m.group(0)
    return ""


def match_version(langs, pack_vn: str = "", pack_label: str = "",
                  pack_ref: str = "") -> dict:
    """在汉化版本列表里挑出「对应这个整合包版本」的那一个；挑不出返回 {}。

    三级匹配，越后面越宽松：

    1. `head_vn == pack_vn` —— 最硬：汉化的 version_number 前段就是整合包
       版本的 version_number；
    2. `ver_text` 与整合包版本的文字版本号一致（`8.4` == `8.4`）；
    3. `join_vn` 反查 —— 拿 BBSMC 整合包项目的版本表把 `joining_version_id`
       翻成 version_number 再比一次（多一次请求，只在 1、2 都落空时才用）。

    都对不上就返回 {}，由界面说「这个版本还没有对应汉化」，**绝不拿别的版本
    的汉化凑数**（版本错位的汉化会覆盖掉不该覆盖的文件）。
    """
    rows = [r for r in (langs or []) if isinstance(r, dict)]
    if not rows:
        return {}
    vn = str(pack_vn or "").strip()
    label = str(pack_label or "")
    want_ver = _ver_text(label) or _ver_text(vn)
    if vn and not vn.isdigit():
        # Modrinth 的 version_number 就是版本号本身（"8.4"），也当文字版本用
        want_ver = want_ver or _ver_text(vn)

    # ① 前段完全相同
    if vn:
        for r in rows:
            if str(r.get("head_vn") or "") == vn:
                return r
    # ② 文字版本号相同
    if want_ver:
        for r in rows:
            if str(r.get("ver_text") or "") == want_ver:
                return r
    # ③ join_vn 反查
    if pack_ref:
        try:
            pack_rows = versions(pack_ref)
        except Exception:                              # noqa: BLE001
            pack_rows = []
        by_id = {}
        for pv in pack_rows:
            if not isinstance(pv, dict):
                continue
            pvn = str(pv.get("version_number") or "")
            by_id[str(pv.get("id") or "")] = pvn
        for r in rows:
            jv = str(r.get("join_vn") or "")
            if not jv:
                continue
            pvn = by_id.get(jv, "")
            if pvn and (pvn == vn or _ver_text(pvn) == want_ver):
                return r
    return {}


def language_page(lang: dict) -> str:
    """汉化项目的网页地址（下载被拦时让玩家去这里手动下）"""
    slug = str((lang or {}).get("slug") or "").strip()
    return f"{SITE}/language/{slug}" if slug else SITE + "/languages"


# ================================================================ 下载
# 第七十六轮补丁（1.5.1）：**下载老是失败不是人机验证，是连不上那个 IP。**
#
# 实测（2026-10-31）：`cdn.bbsmc.net` 只有一条 CNAME 链
#   cdn.bbsmc.net → wvga7fqm.dns.dnsjxla.com → az3dg6u2.n.dnsjxla.com
#   → 202.189.5.156（香港）
# 三个公共 DNS（1.1.1.1 / 223.5.5.5 / 系统）返回的都是这一个 IP，所以不是 DNS
# 污染。而这个 IP 的 443 / 80 在国内直连是**纯超时**（TCP 都不通），走代理也是
# ReadTimeout —— 也就是「节点本身在我们这儿不可达」。
#
# 那为什么玩家的浏览器又打得开 BBSMC？因为浏览器走的是**系统代理**
# （Clash / v2ray 默认只写注册表 `ProxyEnable` + `ProxyServer`，不设环境变量），
# 而 `requests` 的 `trust_env` 只认环境变量。启动器等于一直在直连 ——
# 于是「浏览器能访问、启动器超时」。这一段就是补这个洞：
# 系统代理 → 环境代理 → 直连，逐条探测，谁通用谁。
def _system_proxy() -> dict:
    """Windows 系统代理设置（没有/关着就返回 {}）。

    实现搬到了 `core/sysproxy.py`（第八十轮）：启动游戏时也要拿这份设置翻成
    JVM 参数，**两处必须读同一份**，否则会出现「启动器能下、游戏里的皮肤还是
    查不到」这种一半通一半不通的怪事。这里保留旧名字，内部就是转一手。
    """
    from . import sysproxy
    return sysproxy.system_proxy()


def _proxy_candidates() -> list:
    """按「最可能通」的顺序给出 (名字, proxies) 列表，**去重**。

    顺序是刻意的：先系统代理（国内玩家 90% 是这个），再环境变量（命令行启动
    或 CI 里常见），最后直连（没装代理的人）。全都失败才报错。
    """
    out = []
    sys_px = _system_proxy()
    if sys_px:
        out.append(("系统代理 " + str(list(sys_px.values())[0]), sys_px))
    try:
        env = requests.utils.get_environ_proxies("https://example.com")
    except Exception:                                  # noqa: BLE001
        env = {}
    env = {k: v for k, v in (env or {}).items() if k in ("http", "https") and v}
    if env:
        out.append(("环境变量代理", env))
    out.append(("直连", {"http": None, "https": None}))
    seen, uniq = set(), []
    for label, px in out:
        sig = json.dumps(px, sort_keys=True, default=str)
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append((label, px))
    return uniq


def is_zip(path) -> bool:
    """文件头是不是 ZIP（`PK\\x03\\x04` / 空包 `PK\\x05\\x06`）

    BBSMC 的 CDN 对非浏览器 UA 会吐人机验证页，而**状态码照样是 200**；
    连不上时又会直接抛异常。两种都不该被当成「下好了」，所以不看状态码，
    只认魔数。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(4)
        return head[:2] == b"PK" and head[2:4] in (b"\x03\x04", b"\x05\x06")
    except Exception:                                  # noqa: BLE001
        return False


def _dl_headers() -> dict:
    return {
        "User-Agent": UA_BROWSER,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": SITE + "/",
    }


def _part_path(dest: str, url: str) -> str:
    """续传用的临时文件名 = 目标名 + **URL 指纹**。

    为什么不能光叫 `.part`：汉化包的落点是固定的 `downloads/_langpack.zip`，
    换一个汉化版本（或换一个整合包）还是这个名字。如果残片只叫 `.part`，
    上一次被强杀留下的**别的版本**的半个包就会被当成本次的续传起点 —— 服务器
    按 Range 把新文件的尾巴接在旧文件的头上，拼出来的东西开头仍然是
    `PK\\x03\\x04`，`is_zip()` 照样放行，玩家拿到一个坏包却毫无提示。
    带上 URL 指纹，换文件就自动换临时名，从根上不可能串。
    """
    import hashlib
    sig = hashlib.sha1(str(url or "").encode("utf-8")).hexdigest()[:10]
    return f"{dest}.{sig}.part"


def _reachable(url: str, proxies, timeout: float = 5.0) -> bool:
    """这条代理路径能不能真的拿到字节（只要 1 个字节）。

    为什么要单独探一次：`cdn.bbsmc.net` 的失败形态有三种（TCP 超时 / TLS 后
    不回数据 / 返回 55 字节人机页），直接下 59MB 的话每种都要等很久才报错。
    先用 1 字节 Range 问一句，2~5 秒内就能判出这条路行不行。
    """
    try:
        r = _session.get(url, headers=dict(_dl_headers(), Range="bytes=0-1"),
                         stream=True, timeout=(timeout, timeout),
                         proxies=proxies, allow_redirects=True)
        try:
            if r.status_code not in (200, 206):
                return False
            return bool(r.raw.read(1))
        finally:
            r.close()
    except Exception:                                  # noqa: BLE001
        return False


def _fetch(url: str, dest: str, proxies, progress_cb=None, cancel_flag=None,
           expected_size: int = 0) -> int:
    """真正下载（**支持续传**：`.part` 里已有多少就从哪儿接着下）。

    返回拿到的字节数。断线/超时抛异常，由 `download()` 决定要不要重试。
    """
    part = _part_path(dest, url)
    have = 0
    if os.path.isfile(part):
        try:
            have = os.path.getsize(part)
        except OSError:
            have = 0
    headers = _dl_headers()
    if have:
        headers["Range"] = f"bytes={have}-"
    r = _session.get(url, headers=headers, stream=True,
                     timeout=(8, 30), proxies=proxies, allow_redirects=True)
    try:
        if r.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {r.status_code}")
        if have and r.status_code == 200:
            # 服务器不认 Range（返回整份）→ 只能从头来
            have = 0
            try:
                os.remove(part)
            except OSError:
                pass
        total = int(r.headers.get("Content-Length") or 0) + (have if have else 0)
        if not total:
            total = int(expected_size or 0)
        mode = "ab" if have else "wb"
        with open(part, mode) as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if cancel_flag is not None and getattr(cancel_flag, "is_set",
                                                       lambda: False)():
                    raise RuntimeError("已取消")
                if not chunk:
                    continue
                f.write(chunk)
                have += len(chunk)
                if progress_cb:
                    progress_cb(have, total)
        return have
    finally:
        r.close()


def download(url: str, dest: str, progress_cb=None, cancel_flag=None,
             expected_size: int = 0) -> str:
    """下汉化包到 `dest`，依次试「系统代理 → 环境代理 → 直连」。

    **故意不走 `dl.py` 的分段引擎**：汉化包只有几十 MB，分段带来的收益抵不上
    「没法自定义 UA / 代理 / 续传策略」的代价。这里单流 + 断点续传 + 多路径，
    正好覆盖 BBSMC 这个 CDN 的三种失败形态。

    失败时抛的是**人话 + 下一步动作**，不是 `Max retries exceeded` 那种栈信息：
    界面上要能让玩家知道「点哪个按钮还能救」。
    """
    url = str(url or "").strip()
    if not url:
        raise RuntimeError("汉化包没有可用的下载地址")
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    part = _part_path(dest, url)
    _clean_stale_parts(dest, part)
    tried = []
    captcha = ""          # 非空 = 确实下到了字节，但那是人机验证页
    for label, proxies in _proxy_candidates():
        if cancel_flag is not None and getattr(cancel_flag, "is_set", lambda: False)():
            raise RuntimeError("已取消")
        if not _reachable(url, proxies):
            tried.append(f"{label}连不上")
            continue
        # 探通了就下；断在中途再补一次（续传会把已下的部分接上）
        for attempt in (1, 2):
            try:
                _fetch(url, dest, proxies, progress_cb=progress_cb,
                       cancel_flag=cancel_flag, expected_size=expected_size)
            except Exception as e:                      # noqa: BLE001
                # **必须是 Exception 而不是 RuntimeError**：真正断线时 requests
                # 抛的是 `ConnectionError` / `ChunkedEncodingError`（`OSError`
                # 家族），只抓 RuntimeError 会让「断在中途再补一次」这条续传
                # 逻辑对真实网络错误**永远不生效**，错误直接糊到玩家脸上。
                if cancel_flag is not None and getattr(cancel_flag,
                                                       "is_set", lambda: False)():
                    _rm(part)
                    raise RuntimeError("已取消")
                if attempt >= 2:
                    why = (str(e) or e.__class__.__name__).strip()
                    tried.append(f"{label}下到一半断了（{why[:40]}）")
                    break
                time.sleep(1.2)
                continue
            os.replace(part, dest)
            if is_zip(dest):
                return dest
            # 字节拿到了却不是 zip —— 这条最值得说清楚，**不能重试**（换个代理
            # 还是同一个 CDN，照样吐验证页），也不能被「下到一半断了」盖掉：
            # 玩家需要知道的是「得改用浏览器下」，而不是「网络不好再试试」。
            sz = os.path.getsize(dest) if os.path.isfile(dest) else 0
            _rm(dest)
            captcha = (f"下载服务器返回的是 {sz} 字节的网页而不是压缩包"
                       f"（BBSMC 对程序下载触发了人机验证）")
            break
        if captcha:
            break
    _rm(part)
    if captcha:
        raise RuntimeError(
            captcha + "。点「打开汉化页」用浏览器下载，"
            "下好后点「选择本地文件」导入就能继续")
    detail = "、".join(tried) if tried else "所有下载路径都失败了"
    raise RuntimeError(
        f"连不上 BBSMC 的下载节点（{detail}）。这个节点在国内经常不通，"
        f"可以点「打开汉化页」用浏览器下载，下好后点「选择本地文件」导入")


def _rm(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception:                                  # noqa: BLE001
        pass


def _clean_stale_parts(dest: str, keep: str):
    """删掉同一个目标文件下**别的 URL** 留下的残片。

    残片名的指纹是按 URL 算的，换版本就换名字，于是上一次的残片永远不会被
    用到 —— 不扫的话它们会一直躺在玩家的「下载」文件夹里（`_langpack.zip`
    几十 MB 一个，看着莫名其妙）。只在开工前扫一次，`keep` 是本次要用的那个。
    """
    d = os.path.dirname(dest) or "."
    base = os.path.basename(dest) + "."
    keep_abs = os.path.abspath(keep)
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if not (n.startswith(base) and n.endswith(".part")):
            continue
        p = os.path.join(d, n)
        if os.path.abspath(p) == keep_abs:
            continue
        _rm(p)


def looks_like_langpack(zip_path: str) -> bool:
    """这个 zip 像不像「覆盖型汉化包」。

    特征：有 `config/`、`kubejs/`、`resources/` 这类**要直接铺进游戏目录**的顶层
    目录，但**没有** `mods/`、也没有整合包清单（那是整合包，不是汉化）。

    只用来给「选择本地文件」把个关 —— 玩家选了明显不是汉化的东西时提醒一句，
    真判错了也没关系：后面还有核对窗摊开文件让他看。
    """
    try:
        import zipfile
        with zipfile.ZipFile(zip_path) as z:
            names = [n.replace("\\", "/").lower() for n in z.namelist()
                     if not n.endswith("/")]
    except Exception:                                  # noqa: BLE001
        return False
    if not names:
        return False
    if any(n in ("manifest.json", "modrinth.index.json") for n in names):
        return False
    tops = {n.split("/", 1)[0] for n in names if "/" in n}
    if "mods" in tops:
        return False
    marks = {"config", "kubejs", "resources", "resourcepacks", "shaderpacks",
             "defaultconfigs", "scripts", "tacz", "emixx"}
    return bool(tops & marks)


# ================================================================ 汉化包解包
def unpack(zip_path: str, out_dir: str) -> list:
    """把汉化包解到 `out_dir`，返回 [{"path": 相对路径, "size": 字节}]。

    **目录结构原样保留**：BBSMC 的汉化包是「覆盖型」—— 里面就是 `config/`、
    `kubejs/`、`resources/` 这些要直接铺进整合包目录的东西，不能像整合包
    那样剥一层。
    """
    import zipfile
    rows = []
    out_abs = os.path.abspath(out_dir)
    os.makedirs(out_abs, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            raw = info.filename.replace("\\", "/")
            # 防目录穿越（`../../windows/system32`）
            rel = os.path.normpath(raw).replace("\\", "/").lstrip("/")
            if not rel or rel.startswith("..") or ":" in rel.split("/")[0]:
                continue
            dst = os.path.join(out_abs, rel)
            if not os.path.abspath(dst).startswith(out_abs):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with z.open(info) as src, open(dst, "wb") as f:
                f.write(src.read())
            rows.append({"path": rel, "size": int(info.file_size or 0)})
    return rows
