# -*- coding: utf-8 -*-
"""模组依赖分析：前置（正向）与「谁依赖它」（反向）。

数据来源
- Modrinth 正向：某个 version 的 `dependencies`（required / optional / incompatible / embedded），
  逐层递归，直到没有新的必需前置（默认最深 3 层）。
- Modrinth 反向：`GET /v2/project/{id}/dependencies` 返回依赖该项目的项目列表
  （官方接口，直接给出完整项目对象）。
所有网络调用都在工作线程执行，结果只返回纯数据；界面负责展示。
"""
import json
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import requests

from . import modrinth

API = "https://api.modrinth.com/v2"
UA = "MC-GuanQi-Fuzhu-Gongju/1.0.0"

KIND_CN = {
    "required": "必需前置",
    "optional": "可选前置",
    "incompatible": "不兼容",
    "embedded": "已内置",
    "dependent": "依赖此模组",
    "tool": "工具依赖",
    "include": "包含",
}

_TTL = 600
# 缓存上限（第 52 轮）：Modrinth 的 /project/{id}/version 响应很大（含全部版本、
# changelog、files），`reverse()` 一次最多核验 30 个候选 × 每个 1~2 次请求，
# 原来的无界 dict 会一路涨到几百 MB。这里按 LRU 淘汰。
_CACHE_MAX = 160
_cache = OrderedDict()
_lock = threading.Lock()


def _cached(key, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _TTL:
            _cache.move_to_end(key)     # 命中也挪队尾，否则热键会被冷键挤掉
            return hit[1]
    val = fn()                          # 网络请求在锁外做，绝不持锁等待
    with _lock:
        _cache[key] = (now, val)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return val


def clear_cache():
    with _lock:
        _cache.clear()


def _mr_get(path, params=None):
    r = requests.get(API + path, headers={"User-Agent": UA},
                     params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- Modrinth
def project(pid):
    """项目基础信息（标题/图标/类型/下载量）"""
    return _cached(("p", pid), lambda: _mr_get(f"/project/{pid}"))


def versions(pid, loaders=None, game_versions=None):
    params = {}
    if loaders:
        params["loaders"] = json.dumps(list(loaders))
    if game_versions:
        params["game_versions"] = json.dumps(list(game_versions))
    key = ("v", pid, tuple(loaders or ()), tuple(game_versions or ()))
    return _cached(key, lambda: _mr_get(f"/project/{pid}/version", params))


def best(pid, loaders=None, game_versions=None):
    """该项目在某 MC 版本/加载器下可下载的版本 -> (version, file) 或 None"""
    try:
        vs = versions(pid, loaders, game_versions)
    except Exception:                                  # noqa: BLE001
        return None
    for v in vs or []:
        files = v.get("files") or []
        if not files:
            continue
        primary = [f for f in files if f.get("primary")] or files
        return v, primary[0]
    return None


def _row_from_project(p, kind, loaders, gvs, want_version=True):
    row = {
        "name": p.get("title") or p.get("slug") or p.get("id"),
        "id": p.get("id"),
        "slug": p.get("slug") or "",
        "icon": p.get("icon_url") or "",
        "desc": p.get("description") or "",
        "kind": kind,
        "kind_cn": KIND_CN.get(kind, kind),
        "type": p.get("project_type") or "mod",
        "version": "",
        "url": "",
        "size": 0,
        "available": False,
        "platform": "Modrinth",
    }
    if want_version and kind in ("required", "optional", "dependent"):
        b = best(row["id"], loaders, gvs)
        if b:
            v, f = b
            row["version"] = v.get("version_number") or ""
            row["url"] = f.get("url") or ""
            row["size"] = int(f.get("size") or 0)
            row["available"] = bool(row["url"])
            row["game_versions"] = (v.get("game_versions") or [])[:6]
            row["loaders"] = (v.get("loaders") or [])[:4]
    return row


def forward(project_id, loaders=None, game_versions=None, depth=3):
    """递归解析前置依赖（广度优先，按必需项继续下钻）"""
    rows = []
    seen = {project_id}
    queue = [(project_id, 0)]
    while queue:
        cur, d = queue.pop(0)
        if d >= depth:
            continue
        b = best(cur, loaders, game_versions)
        if not b:
            continue
        v, _f = b
        for dep in v.get("dependencies") or []:
            did = dep.get("project_id")
            kind = dep.get("dependency_type") or "required"
            if not did or did in seen:
                continue
            seen.add(did)
            try:
                p = project(did)
            except Exception:                          # noqa: BLE001
                continue
            rows.append(_row_from_project(p, kind, loaders, game_versions))
            if kind == "required":
                queue.append((did, d + 1))
    # 必需前置排前面
    order = {"required": 0, "optional": 1, "incompatible": 2, "embedded": 3}
    rows.sort(key=lambda r: order.get(r["kind"], 9))
    return rows


def reverse(project_id, loaders=None, game_versions=None, limit=30):
    """依赖该模组的模组（依赖该模组的项目）。

    说明：Modrinth 的 `/project/{id}/dependencies` 返回的是「与本项目有依赖关系的
    项目集合」（既含它自己的前置，也含依赖它的项目），**不区分方向**。
    所以这里逐个核验：拉取候选项目在我们这个 MC/加载器下的可下载版本，
    只有该版本的 dependencies 里真的声明了本项目，才算「依赖此模组」。
    """
    try:
        d = _cached(("rev", project_id),
                    lambda: _mr_get(f"/project/{project_id}/dependencies"))
    except Exception:                                  # noqa: BLE001
        return []
    cands = [p for p in (d.get("projects") or [])
             if p.get("id") and p.get("id") != project_id
             and (p.get("project_type") or "mod") in ("mod", "plugin", "datapack")]
    cands = cands[:limit]
    if not cands:
        return []

    def one(p):
        b = best(p.get("id"), loaders, game_versions)
        if not b:
            return None
        v, _f = b
        hit = any(str(dep.get("project_id") or "") == str(project_id)
                  for dep in (v.get("dependencies") or []))
        if not hit:
            return None
        return _row_from_project(p, "dependent", loaders, game_versions)

    # ⚠️ 这个池必须独立，绝不能并回 `asyncio_qt` 的全局池（12 worker）：
    # `analyze()` 本身就是跑在全局池里的，这里是「父任务里提交子任务再阻塞等」，
    # 并回全局就是 12 个 worker 全被父任务占住 → 子任务永远排不上 → 自锁。
    # 并发从 6 降到 3：查看依赖时后台可能正在装整合包，少抢点 CPU 和连接。
    with ThreadPoolExecutor(max_workers=3) as ex:
        rows = [r for r in ex.map(one, cands) if r]
    rows.sort(key=lambda r: (not r.get("available"), r.get("name") or ""))
    return rows


# ---------------------------------------------------------------- 汇总入口
def analyze(hit, mc="", loader=""):
    """分析一个搜索结果的全部依赖关系。

    返回 {
      "forward": [row...], "reverse": [row...],
      "target": {"mc","loader","version"},
      "notes": [str...],
    }
    """
    out = {"forward": [], "reverse": [], "notes": [],
           "target": {"mc": mc, "loader": loader, "version": ""}}
    if not hit:
        return out

    pid = hit.get("project_id") or hit.get("id")
    if not pid:
        out["notes"].append("缺少项目 ID，无法分析依赖")
        return out

    loaders = [loader] if loader else None
    gvs = [mc] if mc else None
    # 没指定版本/加载器时，按「即将下载的那个版本」反推，保证与安装目标一致
    try:
        b = best(pid, loaders, gvs)
        if b:
            v, _f = b
            out["target"]["version"] = v.get("version_number") or ""
            if not mc:
                gv = v.get("game_versions") or []
                if gv:
                    mc = gv[-1]
                    out["target"]["mc"] = mc
            if not loader:
                ld = v.get("loaders") or []
                if ld:
                    loader = ld[0]
                    out["target"]["loader"] = loader
    except Exception:                                  # noqa: BLE001
        pass

    loaders = [loader] if loader else None
    gvs = [mc] if mc else None
    try:
        out["forward"] = forward(pid, loaders, gvs)
    except Exception as e:                             # noqa: BLE001
        out["notes"].append(f"前置解析失败：{str(e)[:60]}")
    try:
        out["reverse"] = reverse(pid, loaders, gvs)
    except Exception as e:                             # noqa: BLE001
        out["notes"].append(f"反向依赖解析失败：{str(e)[:60]}")
    if out["target"]["version"]:
        bits = []
        if out["target"]["mc"]:
            bits.append(f"Minecraft {out['target']['mc']}")
        if out["target"]["loader"]:
            bits.append(out["target"]["loader"])
        if bits:
            out["notes"].append("按 " + " / ".join(bits) + " 分析")
    return out
