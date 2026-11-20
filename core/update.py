# -*- coding: utf-8 -*-
"""启动器自更新（第一百二十四轮 / 1.9.3）。

设计要点（别改错）：
1. **Release 里放的是「安装向导」exe**（自带新主程序 + SHA256 清单），
   不是裸主程序。所以静默更新的正解是：下载新向导 → 退出时分离进程
   静默运行向导、由向导的 payload 覆盖主程序，而不是在主进程里直接替换
   —— onefile 运行中 exe 被 Windows 锁死，直接替换必失败（ROADMAP 坑 1/2）。
2. **仓库地址可配置**：默认 owner 是 Yifengjun1（已与作者核实，中间是 jun），
   允许 `update_repo` 配置覆盖；全失败才明确报错，绝不静默降级成「已是最新」。
3. **线程铁律**：检查 / 下载全在工作线程（run_async），进度只写纯数据，
   回调不碰 QWidget；取消用 list 标志 `cancel_flag[0]`（与 dl.py 同口径）。
4. tag 形如 `BaguiLauncherv1.9.2`，版本号必须用正则抠，不能 `lstrip("v")`。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import requests

from . import bbsmc
from .config import APP_VERSION, CONFIG, DATA_DIR_NAME

# 作者的 GitHub 用户名已核实为 Yifengjun1（中间是 jun，第 125 轮更正）。
# 注意：Yifengjin1（jin）/ Yifengjin 都不是本人的号（后者是另一位 Java 作者），
# 写错就 404。已用网页实测 https://github.com/Yifengjun1/Bagui-Launcher/releases 可打开。
# 可用 update_repo 配置覆盖。
REPO_CANDIDATES = [
    "Yifengjun1/Bagui-Launcher",
]
QUARK_URL = "https://pan.quark.cn/s/a3180fa2faf1"
GITHUB_API = "https://api.github.com"
UA = "BaguiLauncher-Update/1.0"

_VER_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


def _repos():
    """配置优先，其次候选列表。"""
    custom = str(CONFIG.get("update_repo", "") or "").strip().strip("/")
    if custom:
        return [custom] + [r for r in REPO_CANDIDATES if r.lower() != custom.lower()]
    return list(REPO_CANDIDATES)


def ver_tuple(v):
    """'1.9.3' / 'BaguiLauncherv1.9.2' → (1,9,3)；抠不出返回 (0,0,0)。"""
    m = _VER_RE.search(str(v or ""))
    if not m:
        return (0, 0, 0)
    parts = [int(x) for x in m.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(remote, local=None):
    return ver_tuple(remote) > ver_tuple(local or APP_VERSION)


def _api_get(path, timeout=(6, 20)):
    """GET GitHub API，依次试系统代理 → 环境代理 → 直连。

    返回解析后的 JSON；全部失败抛 RuntimeError（带每条路径的原因）。
    不能用 bbsmc._get —— 它不带代理回退（bbsmc.py:635-640 有说明）。
    """
    url = path if str(path).startswith("http") else GITHUB_API + path
    tried = []
    last_status = ""
    for label, proxies in bbsmc._proxy_candidates():
        try:
            r = requests.get(
                url,
                headers={"User-Agent": UA,
                         "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"},
                timeout=timeout, proxies=proxies, allow_redirects=True)
            last_status = str(r.status_code)
            if r.status_code == 404:
                tried.append(f"{label}:404")
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:                          # noqa: BLE001
            tried.append(f"{label}:{str(e)[:40]}")
    raise RuntimeError("GitHub 检查更新失败（" + "、".join(tried) +
                       (f"，最后状态 {last_status}" if last_status else "") +
                       "）。请检查网络 / 代理，或在设置里核对仓库地址。")


def _parse_release(d):
    """GitHub release JSON → 更新信息字典；没有可用 exe 资产返回 None。"""
    if not isinstance(d, dict) or d.get("draft"):
        return None
    tag = str(d.get("tag_name") or "")
    ver = ver_tuple(tag)
    if ver == (0, 0, 0):
        return None
    assets = [a for a in (d.get("assets") or [])
              if isinstance(a, dict) and str(a.get("name") or "").lower().endswith(".exe")]
    if not assets:
        return None
    # 优先安装向导（名字里带 setup / 安装 / 向导），否则取第一个
    assets.sort(key=lambda a: 0 if re.search(r"setup|安装|向导|wizard",
                                             str(a.get("name") or ""), re.I) else 1)
    a = assets[0]
    digest = str(a.get("digest") or "")
    sha = digest.split(":", 1)[1] if digest.lower().startswith("sha256:") else ""
    return {
        "version": ".".join(str(x) for x in ver),
        "tag": tag,
        "name": str(d.get("name") or tag),
        "notes": str(d.get("body") or ""),
        "published": str(d.get("published_at") or ""),
        "asset_name": str(a.get("name") or ""),
        "url": str(a.get("browser_download_url") or ""),
        "size": int(a.get("size") or 0),
        "sha256": sha,
        "html_url": str(d.get("html_url") or ""),
    }


def check_for_update():
    """检查更新：返回信息字典（若有更新）或 None（已是最新）。

    每个候选仓库依次试 latest；latest 404（pre-release 时）回退到 releases 列表。
    全部候选都失败 → 抛 RuntimeError（让调用方明确提示，不静默吞掉）。
    """
    errors = []
    for repo in _repos():
        info = None
        try:
            try:
                d = _api_get(f"/repos/{repo}/releases/latest")
                info = _parse_release(d)
            except RuntimeError as e:
                if "404" not in str(e):
                    raise
                # latest 端点不返回 pre-release：退到列表
                lst = _api_get(f"/repos/{repo}/releases")
                if isinstance(lst, list):
                    for d in lst:
                        info = _parse_release(d)
                        if info:
                            break
            if info and is_newer(info["version"]):
                info["repo"] = repo
                return info
            if info:
                return None                 # 这个仓库通了，但已是最新
            errors.append(f"{repo}:无可用 release 资产")
        except Exception as e:              # noqa: BLE001
            errors.append(f"{repo}:{str(e)[:60]}")
    raise RuntimeError("检查更新失败：" + "；".join(errors))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def update_dir():
    """下载的新向导暂存目录（在独立于安装目录的数据目录下）。"""
    try:
        from .config import app_data_dir
        base = app_data_dir()
    except Exception:                       # noqa: BLE001
        base = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", DATA_DIR_NAME)
    d = os.path.join(base, "updates")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def download_setup(info, progress_cb=None, cancel_flag=None):
    """下载新版安装向导 exe 到暂存目录，校验 SHA256 后返回本地路径。

    progress_cb(done, total) 单位是字节；cancel_flag 是单元素 list（[False]）。
    """
    if not info or not info.get("url"):
        raise RuntimeError("更新信息里没有下载地址")
    dest = os.path.join(update_dir(), "bagui-setup-" + info["version"] + ".exe")
    tmp = dest + ".part"
    expected_sha = str(info.get("sha256") or "").lower()
    expected_size = int(info.get("size") or 0)
    cancelled = lambda: bool(cancel_flag is not None and cancel_flag[0])
    last_err = ""
    for label, proxies in bbsmc._proxy_candidates():
        if cancelled():
            raise RuntimeError("已取消")
        done = 0
        try:
            r = requests.get(info["url"], stream=True, timeout=(10, 40),
                             proxies=proxies,
                             headers={"User-Agent": UA}, allow_redirects=True)
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if cancelled():
                        r.close()
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                        raise RuntimeError("已取消")
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb(done, expected_size or 0)
            r.close()
            if expected_size and os.path.getsize(tmp) != expected_size:
                raise RuntimeError(f"下载不完整：{os.path.getsize(tmp)} / {expected_size}")
            if expected_sha:
                got = sha256_file(tmp)
                if got != expected_sha:
                    raise RuntimeError("SHA256 校验不通过，已丢弃下载文件")
            # 落地：确认是可执行文件（MZ 头），不是错误页
            with open(tmp, "rb") as f:
                if f.read(2) != b"MZ":
                    raise RuntimeError("下载的不是可执行程序（可能是网页错误页）")
            os.replace(tmp, dest)
            return dest
        except RuntimeError:
            raise
        except Exception as e:              # noqa: BLE001
            last_err = f"{label}:{str(e)[:50]}"
            try:
                os.remove(tmp)
            except OSError:
                pass
            continue
    raise RuntimeError("下载更新失败：" + (last_err or "所有网络路径都不可用"))


# ---- 待安装标记（退出时静默安装用） ----

def _pending_path():
    return os.path.join(update_dir(), "pending.json")


def mark_pending(info, setup_path):
    """记下「已下载好、等退出时安装」的更新。"""
    d = {
        "version": info.get("version", ""),
        "setup": os.path.abspath(setup_path),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(_pending_path(), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    return d


def pending():
    try:
        with open(_pending_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or not d.get("setup"):
            return None
        if not os.path.isfile(d["setup"]):
            return None
        return d
    except Exception:                       # noqa: BLE001
        return None


def clear_pending():
    try:
        os.remove(_pending_path())
    except OSError:
        pass


def _log(msg):
    """静默更新日志（开在更新目录，排障用）。"""
    try:
        p = os.path.join(update_dir(), "silent-update.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:                       # noqa: BLE001
        pass


def schedule_silent_install():
    """主程序退出时调用：起分离进程，等本进程消失后静默运行新向导。

    只在本程序是 frozen（真安装版）且有待安装标记时才动作；开发模式不碰文件。
    返回是否成功调度（False = 无需更新 / 条件不符 / 调度失败，均不影响退出）。
    """
    pd = pending()
    if not pd or not getattr(sys, "frozen", False):
        return False
    setup = pd["setup"]
    if not os.path.isfile(setup):
        _log("待安装的向导不存在：" + setup)
        clear_pending()
        return False
    pid = os.getpid()
    try:
        tmpd = tempfile.mkdtemp(prefix="bgl_update_")
        bat = os.path.join(tmpd, "silent_install.bat")
        # 等主程序进程真的退出（按 pid 轮询，最多 ~60s），再静默运行新向导。
        # cmd 默认 GBK：中文路径必须按 gbk 写，否则 bat 里全是乱码。
        lines = [
            "@echo off", "chcp 936 >nul",
            "set /a n=0",
            ":wait",
            f'tasklist /FI "PID eq {pid}" 2>NUL | find /I "{pid}" >NUL',
            "if errorlevel 1 goto run",
            "set /a n+=1",
            "if %n% GEQ 60 goto run",
            "ping -n 2 127.0.0.1 >nul",
            "goto wait",
            ":run",
            f'start "" /b "{setup}" --silent-update',
            'del /f /q "%~f0"',
            "exit",
        ]
        with open(bat, "w", encoding="gbk", errors="replace") as f:
            f.write("\r\n".join(lines) + "\r\n")
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(["cmd", "/c", bat], creationflags=flags, close_fds=True)
        _log(f"已调度静默更新：{setup}（等 pid {pid} 退出）")
        return True
    except Exception as e:                  # noqa: BLE001
        _log("调度静默更新失败：" + str(e))
        return False
