# -*- coding: utf-8 -*-
"""官网图标（第六十五轮，包内图标的兜底）。

为什么要这一层：模组 jar 里的 `assets/<id>/icon.png` 只有约 2/3 的模组放，
剩下 1/3 在列表里永远是「绿方块写个 J」。这些模组大多是我们自己从下载页装进来的，
而下载页本来就拿到了官网的 `icon_url` —— 存下来、下到本地缓存，下次离线也能显示。

三层数据：
  1. `config.installed` 里的 `icon_url`（第六十五轮起，安装时顺手记下来）
  2. 没记 url 的老记录：`https://api.modrinth.com/v2/project/<pid>` 查一次（pid 就是记录键）
  3. 有些记录只存了 `path`：按**文件路径**反查 pid

缓存文件放 `<数据目录>/modicons/<pid>.png`，一旦落盘就永久命中，不再联网。
⚠ 只有「我们装过的」才有 pid；整合包带进来的 jar 没有记录，仍然只能靠包内图标。
"""
import os
import re

from . import modrinth

SUBDIR = "modicons"
_PID_RE = re.compile(r"^[A-Za-z0-9]{6,12}$")     # Modrinth 的 project_id 形如 l9m9tuPN


def cache_dir(create=True) -> str:
    from .config import app_data_dir
    d = os.path.join(app_data_dir(), SUBDIR)
    if create:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            return ""
    return d


def _safe(pid: str) -> str:
    pid = str(pid or "")
    return pid if _PID_RE.match(pid) else ""


def local_path(pid: str) -> str:
    pid = _safe(pid)
    d = cache_dir(create=bool(pid))
    return os.path.join(d, pid + ".png") if pid and d else ""


def get_cached(pid: str):
    """已经下过的图标字节；没有返回 None"""
    p = local_path(pid)
    try:
        if p and os.path.isfile(p) and os.path.getsize(p) > 0:
            with open(p, "rb") as f:
                return f.read()
    except OSError:
        pass
    return None


def _records() -> dict:
    from .config import CONFIG
    rec = CONFIG.data.get("installed") or {}
    return rec if isinstance(rec, dict) else {}


def icon_url_of(pid: str, api=True) -> str:
    """这个项目在官网的图标地址（先看安装记录，再看 Modrinth）"""
    rec = _records().get(str(pid))
    url = ""
    if isinstance(rec, dict):
        url = str(rec.get("icon_url") or "")
    if url:
        return url
    if not api or not _safe(pid):
        return ""
    try:
        data = modrinth.get_project(str(pid))
    except Exception:                                     # noqa: BLE001
        return ""
    if isinstance(data, dict):
        url = str(data.get("icon_url") or "")
    return url


def fetch(pid: str) -> bytes:
    """联网取图标并**存到本地缓存**；失败返回 b""（下次还会再试）"""
    pid = _safe(pid)
    if not pid:
        return b""
    hit = get_cached(pid)
    if hit:
        return hit
    url = icon_url_of(pid)
    if not url:
        return b""
    try:
        r = modrinth._get(url, timeout=20)
        data = r.content or b""
    except Exception:                                     # noqa: BLE001
        return b""
    if not data or len(data) > 4 * 1024 * 1024:
        return b""
    p = local_path(pid)
    if p:
        try:
            tmp = p + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
        except OSError:
            pass
    return data


# ---------------------------------------------------------------- 文件 → pid
_MAP = {"sig": None, "by_path": {}}


def _norm(p: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(str(p or "")))
    except (OSError, ValueError):
        return str(p or "").lower()


def _signature():
    """记录变没变的指纹（条数 + 各条长度之和够用了，不必逐字段比）"""
    rec = _records()
    return (len(rec), sum(len(str(v)) for v in rec.values()))


def _build_map():
    """从安装记录建「文件路径 → 项目 id」。

    记录里的 `path` 有的指向**文件**（我们自己装的模组），有的指向**实例目录**
    （整合包）—— 后者对不上单个 jar，跳过。
    """
    by_path = {}
    for pid, rec in _records().items():
        if not isinstance(rec, dict):
            continue
        p = str(rec.get("path") or "")
        if not p or os.path.isdir(p):
            continue
        by_path[_norm(p)] = str(pid)
    _MAP["by_path"] = by_path


def pid_for_file(path: str) -> str:
    """这个文件是我们装过的哪个项目（不是我们装的返回 ""）"""
    sig = _signature()
    if _MAP["sig"] != sig:
        _build_map()
        _MAP["sig"] = sig
    return _MAP["by_path"].get(_norm(path), "")


def clear_cache_file(pid: str) -> bool:
    try:
        p = local_path(pid)
        if p and os.path.isfile(p):
            os.remove(p)
            return True
    except OSError:
        pass
    return False
