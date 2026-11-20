# -*- coding: utf-8 -*-
"""模组可更新检测 + 就地更新（第八十九轮改造）。

**认文件，不认来源**：给 mods 目录里每个 .jar 算 SHA1，批量发 Modrinth
`POST /v2/version_files/update`，由官方返回「这个文件属于哪个项目、最新兼容版是谁」。
于是网上下载的、整合包自带的、别的启动器装的模组也能跟着更新 ——
不再依赖「经本启动器安装」的 installed 记录。

不卡的三个前提：
  1. 整段跑在后台线程（调用方 `run_async`），主线程不做哈希、不发请求；
  2. 文件哈希按 (mtime, size) 缓存到 `<数据目录>\\cache\\mod_hash_cache.json`，
     文件没变就不重新读盘；
  3. 一个文件都没变时，仍然只发**一次**批量请求，不做逐文件请求。

任何异常（无网络 / 代理 / 限流 / 接口变动）一律静默返回空或失败信息，绝不抛、绝不卡。
"""
import hashlib
import json
import os

from . import config as _cfg
from .config import CONFIG

_HASH_CACHE_NAME = "mod_hash_cache.json"
_BATCH = 200          # 每个批量请求带多少个哈希


def _norm(p):
    try:
        return os.path.normcase(os.path.abspath(p or ""))
    except (OSError, ValueError):
        return str(p or "").lower()


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_path():
    try:
        return os.path.join(_cfg.icon_cache_dir(), _HASH_CACHE_NAME)
    except Exception:                                  # noqa: BLE001
        return ""


def _load_cache():
    p = _cache_path()
    if not p:
        return {}, ""
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return (data if isinstance(data, dict) else {}), p
    except Exception:                                  # noqa: BLE001
        return {}, p


def _save_cache(cache, path):
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    except Exception:                                  # noqa: BLE001
        pass


def _list_jars(folder):
    """[(path, mtime, size)]：目录里的 .jar / .jar.disabled（只 stat，不读内容）"""
    out = []
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    if not e.is_file():
                        continue
                    nm = e.name.lower()
                    if nm.endswith(".disabled"):
                        nm = nm[:-len(".disabled")]
                    if not nm.endswith(".jar"):
                        continue
                    st = e.stat()
                except OSError:
                    continue
                out.append((e.path, float(getattr(st, "st_mtime", 0.0) or 0.0),
                            int(getattr(st, "st_size", 0) or 0)))
    except OSError:
        return []
    return out


def check_updates(folder, mc="", loader=""):
    """返回 {normpath: {"pid","version","url","filename","version_id"}}，只含可更新的。

    流程：算/取缓存哈希 → 批量查 Modrinth → 返回里的最新文件哈希与本地不同即有新版。
    """
    folder = str(folder or "")
    if not os.path.isdir(folder):
        return {}
    jars = _list_jars(folder)
    if not jars:
        return {}

    cache, cpath = _load_cache()
    dirty = False
    by_hash = {}                       # sha1 -> [local path, ...]
    for path, mt, size in jars:
        key = _norm(path)
        rec = cache.get(key)
        sha = ""
        if isinstance(rec, dict) and rec.get("mtime") == mt \
                and rec.get("size") == size and rec.get("sha1"):
            sha = str(rec["sha1"])
        else:
            try:
                sha = _sha1(path)
            except OSError:
                continue
            cache[key] = {"mtime": mt, "size": size, "sha1": sha}
            dirty = True
        if sha:
            by_hash.setdefault(sha.lower(), []).append(path)
    if dirty:
        _save_cache(cache, cpath)
    if not by_hash:
        return {}

    from . import modrinth
    loaders = [loader] if loader else None
    gvs = [mc] if mc else None
    got = {}
    shas = list(by_hash.keys())
    for i in range(0, len(shas), _BATCH):
        chunk = shas[i:i + _BATCH]
        try:
            resp = modrinth.version_files_update(
                chunk, loaders=loaders, game_versions=gvs) or {}
        except Exception:                              # noqa: BLE001
            resp = {}
        if isinstance(resp, dict):
            got.update(resp)

    result = {}
    for sha, paths in by_hash.items():
        ver = got.get(sha)
        if not isinstance(ver, dict):
            continue
        files = ver.get("files") or []
        if not files:
            continue
        primary = next((f for f in files if f.get("primary")), files[0])
        new_sha = str(((primary.get("hashes") or {}).get("sha1") or "")).lower()
        # 返回的最新文件哈希与本地相同 → 已经是最新，不算可更新
        if new_sha and new_sha == sha:
            continue
        info = {"pid": str(ver.get("project_id") or ""),
                "version": str(ver.get("version_number") or ver.get("name") or ""),
                "url": str(primary.get("url") or ""),
                "filename": str(primary.get("filename") or ""),
                "version_id": str(ver.get("id") or "")}
        if not info["url"] or not info["filename"]:
            continue
        for p in paths:
            result[_norm(p)] = info
    return result


def update_mod(info, folder, old_path="", progress_cb=None, cancel_flag=None):
    """按 check_updates 返回的 info 下载并就地替换。返回 (ok, 给玩家看的话)。"""
    info = info or {}
    url = str(info.get("url") or "")
    fname = str(info.get("filename") or "")
    folder = str(folder or "")
    if not url or not fname:
        return False, "没有可下载的新版本文件"
    from . import installer, modrinth
    tmp = os.path.join(installer.downloads_dir(), fname)
    try:
        modrinth.download_to(url, tmp, progress_cb=progress_cb,
                             cancel_flag=cancel_flag, expected_size=0)
    except Exception as e:                              # noqa: BLE001
        return False, f"下载失败：{e}"
    try:
        installer.install_simple("mod", tmp, target_dir=folder, dest_name=fname)
    except Exception as e:                              # noqa: BLE001
        return False, f"安装失败：{e}"
    new_path = os.path.join(folder, fname)
    # 删旧文件（同 modid 不能两份都留着，否则加载两次）
    if old_path and _norm(old_path) != _norm(new_path):
        try:
            if os.path.isfile(old_path):
                os.remove(old_path)
        except OSError:
            pass
    # 若有 installed 记录指向旧文件，把 path 改成新文件（下次比对才准）
    try:
        rec = CONFIG.data.get("installed") or {}
        changed = False
        for _pid, r in rec.items():
            if isinstance(r, dict) and r.get("path") \
                    and _norm(r.get("path")) == _norm(old_path):
                r["path"] = new_path
                changed = True
        if changed:
            CONFIG.save()
    except Exception:                                   # noqa: BLE001
        pass
    return True, f"已更新到 {info.get('version') or fname}"
