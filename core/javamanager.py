# -*- coding: utf-8 -*-
"""Java 运行环境下载与安装（官方构建，不碰第三方改包）。

下载源（按顺序试，速度太慢会自动换下一个）：
1. 微软 OpenJDK —— `aka.ms` 短链，实际落在 download.visualstudio.microsoft.com；
   **微软没有发布 JDK 8**（该短链会跳到必应），8 只能走其它源；
2. Amazon Corretto —— AWS CDN，国内一般可用，8/17/21 都有；
3. Adoptium Temurin —— 官方 API 取直链（GitHub），最后兜底。

安装：解压到 `%ProgramFiles%\\Java\\jdk-<版本>`（非管理员退到
`%LOCALAPPDATA%\\Programs\\Osmanthus flower\\Java`），随后写入 JAVA_HOME 并把
`%JAVA_HOME%\\bin` 加到 PATH 前面，最后广播 WM_SETTINGCHANGE 让新进程立刻生效。
"""
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile

import requests

from .config import INSTALL_DIR_NAME, LEGACY_INSTALL_NAMES, app_data_dir
from .tasks import TASKS
from .winadmin import is_admin

UA = "MC-GuanQi-Fuzhu-Gongju/1.0.0"
MS_URL = "https://aka.ms/download-jdk/microsoft-jdk-{maj}-windows-x64.zip"
CORRETTO_URL = ("https://corretto.aws/downloads/latest/"
                "amazon-corretto-{maj}-x64-windows-jdk.zip")
ADOPTIUM_API = ("https://api.adoptium.net/v3/assets/latest/{maj}/hotspot"
                "?architecture=x64&image_type=jdk&os=windows&vendor=eclipse")
TUNA = "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/{maj}/jdk/x64/windows/{name}"

MIN_BYTES = 20 * 1024 * 1024          # 小于 20MB 的一定不是 JDK（多半是跳转页）
MIN_SPEED = 120 * 1024                # 低于 120KB/s 判为「太慢，换源」
SPEED_WINDOW = 12                     # 秒

# 可下载的主版本（第七十二轮补齐）：以前只有 21 / 17 / 8 三个，玩家想给
# 1.17 装个 16、或者用 11 跑老整合包都没有入口。顺序按「新 -> 旧」排。
# 每个源能给的版本不一样：微软只发 11/17/21/25，Corretto 没有 16，
# Adoptium 8~25 全有 —— candidates() 会逐个探活，探不到就换下一个。
# 下拉里的字要短（面板只有 370 宽），「哪个 MC 版本用它」放 MAJOR_NOTE 里，
# 选中时显示在提示行上。
MAJORS = [
    (25, "25 · LTS（最新）"),
    (21, "21 · LTS（1.20.5+）"),
    (17, "17 · LTS（1.18~1.20.4）"),
    (16, "16（1.17 专用）"),
    (11, "11 · LTS"),
    (8, "8 · LTS（1.16.5 及更早）"),
]

MAJOR_NOTE = {
    25: "最新正式版与以后的快照都要它；已装 21 的不用急着换。",
    21: "Minecraft 1.20.5 ~ 1.21.5 必须用它（1.20.4 及更早用不上）。",
    17: "Minecraft 1.18 ~ 1.20.4 必须用它，NeoForge 1.20.1 的服务端也要。",
    16: "只有 1.17 需要；其它版本装它会启动失败，别混用。",
    11: "1.13 ~ 1.16 也能跑；有些老整合包和模组明确要求 11。",
    8: "1.16.5 及更早（含 1.7.10 / 1.12.2 整合包）用它。",
}


def _get(url, timeout=25, **kw):
    return requests.get(url, headers={"User-Agent": UA}, timeout=timeout, **kw)


def _probe(url: str, source: str) -> dict | None:
    """HEAD/GET 首包确认这是个真 zip，返回候选信息"""
    try:
        r = _get(url, timeout=25, stream=True)
        size = int(r.headers.get("content-length") or 0)
        ctype = (r.headers.get("content-type") or "").lower()
        final = r.url
        r.close()
        if r.status_code >= 400 or size < MIN_BYTES:
            return None
        if "html" in ctype:
            return None
        return {"url": final, "name": os.path.basename(final.split("?")[0]) or "jdk.zip",
                "size": size, "sha256": "", "source": source}
    except Exception:
        return None


# ---------------------------------------------------------------- 版本信息
def candidates(major: int) -> list:
    """该主版本所有可用下载源（按推荐顺序）"""
    out = []
    if major >= 11:                       # 微软只发过 11/17/21
        c = _probe(MS_URL.format(maj=major), "微软 OpenJDK")
        if c:
            out.append(c)
    c = _probe(CORRETTO_URL.format(maj=major), "Amazon Corretto")
    if c:
        out.append(c)
    try:
        d = _get(ADOPTIUM_API.format(maj=major), timeout=20).json()
        for item in d:
            pkg = ((item.get("binary") or {}).get("package") or {})
            link = pkg.get("link") or ""
            if not link.endswith(".zip"):
                continue
            name = os.path.basename(link)
            out.append({"url": link, "name": name, "size": int(pkg.get("size") or 0),
                        "sha256": str(pkg.get("checksum") or ""),
                        "source": "Adoptium Temurin"})
            m = re.search(r"(?:jdk|OpenJDK)[-_]?(\d+)U?", name, re.I)
            if m:                          # 再加一个清华同文件名的镜像候选
                out.append({"url": TUNA.format(maj=m.group(1), name=name), "name": name,
                            "size": int(pkg.get("size") or 0),
                            "sha256": str(pkg.get("checksum") or ""),
                            "source": "清华 Adoptium 镜像"})
            break
    except Exception:
        pass
    return out


def resolve(major: int) -> dict:
    """第一个可用下载源；都不可用则抛异常。"""
    cands = candidates(major)
    if not cands:
        raise RuntimeError(f"拿不到 Java {major} 的下载地址（官方源与镜像都没响应）")
    return cands[0]


# ---------------------------------------------------------------- 安装位置
def install_root() -> str:
    """Java 安装根目录（优先系统 Java 目录）"""
    if is_admin():
        pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
        return os.path.join(pf, "Java")
    base = os.environ.get("LOCALAPPDATA") or app_data_dir()
    # 改名（第三十八轮）前装在旧目录里的 Java 继续用，别让玩家白下一次
    for name in LEGACY_INSTALL_NAMES[1:]:
        old = os.path.join(base, "Programs", name, "Java")
        if os.path.isdir(old):
            return old
    return os.path.join(base, "Programs", INSTALL_DIR_NAME, "Java")


def current_java() -> tuple[str, int, str]:
    """本机当前能找到的最合适的 Java：返回 (路径, 主版本, 版本号字符串)

    第五十三轮：这个方法原来对每个候选都跑一次 `java -version`（含版本号再跑一次），
    实测单次 **3.7 秒**，而「Java 下载」和「设置」两个弹层都会在点开的瞬间调它
    → 玩家看到的就是「卡一下才出来」。现在主版本与版本号都走 `release` 文本文件
    （毫秒级），候选列表也缓存，整个调用降到个位数毫秒。
    """
    from . import loader
    for min_major in (21, 17, 8):
        p = loader.find_java(min_major)
        if p:
            return p, loader._java_major(p), _version_text(p)
    return "", 0, ""


def _version_text(java_exe: str) -> str:
    from . import loader
    v = loader._version_from_release(java_exe)
    if v:
        return v
    try:
        r = subprocess.run([java_exe, "-version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        txt = (r.stderr or "") + (r.stdout or "")
        m = re.search(r'version "([^"]+)"', txt)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _major_of(java_exe: str) -> int:
    from .loader import _java_major
    return _java_major(java_exe)


# ---------------------------------------------------------------- 本机 Java 扫描
# 「启动项 → Java」要列出玩家机器上所有能用的 java.exe，不能只认我们自己装的那份。
# 关键：**不启动 java -version**（每个要 100~300ms，扫 8 个就是两秒，界面会明显卡），
# 改成读 JDK 自带的 `release` 文件（纯文本，毫秒级），读不到再从目录名猜。
_SCAN_CACHE = {"t": 0.0, "data": []}
_SCAN_TTL = 30.0

_JAVA_GLOBS = (
    r"%ProgramFiles%\Java\*\bin\java.exe",
    r"%ProgramFiles%\Eclipse Adoptium\*\bin\java.exe",
    r"%ProgramFiles%\Microsoft\jdk*\bin\java.exe",
    r"%ProgramFiles%\Zulu\*\bin\java.exe",
    r"%ProgramFiles%\BellSoft\*\bin\java.exe",
    r"%ProgramFiles%\Semeru\*\bin\java.exe",
    r"%ProgramFiles(x86)%\Java\*\bin\java.exe",
    r"%LOCALAPPDATA%\Programs\Eclipse Adoptium\*\bin\java.exe",
    r"%LOCALAPPDATA%\Programs\Microsoft\jdk*\bin\java.exe",
    r"%LOCALAPPDATA%\Programs\Java\*\bin\java.exe",
)
# 官方启动器 / 商店版自带的 runtime 也算（玩家常常只装过游戏）
_JAVA_DEEP_GLOBS = (
    r"%APPDATA%\.minecraft\runtime\**\bin\java.exe",
    r"%LOCALAPPDATA%\Packages\Microsoft.4297127D64EC6*\LocalCache\Local\runtime\**\bin\java.exe",
)


def _release_major(home: str) -> tuple[int, str]:
    """从 <JAVA_HOME>/release 里读主版本与版本号（读不到返回 (0, "")）"""
    p = os.path.join(home, "release")
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("JAVA_VERSION="):
                    ver = line.split("=", 1)[1].strip().strip('"')
                    m = re.match(r"(\d+)(?:\.(\d+))?", ver)
                    if not m:
                        return 0, ver
                    major = int(m.group(1))
                    if major == 1 and m.group(2):      # 1.8.0_401 → 8
                        major = int(m.group(2))
                    return major, ver
    except Exception:
        pass
    return 0, ""


def _guess_major_from_path(home: str) -> int:
    """目录名兜底：jdk-17.0.12 / jdk8u402 / java-21-openjdk / 17.0.12+7"""
    base = os.path.basename(home.rstrip("\\/"))
    m = re.search(r"(?:jdk|jre|java)[-_ .]?(?:1\.)?(\d+)", base, re.I)
    if m:
        n = int(m.group(1))
        return 8 if n == 1 else n
    m = re.search(r"^(\d+)\.", base)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:java|jdk)[-_](\d+)", home, re.I)
    return int(m.group(1)) if m else 0


def java_home_of(exe: str) -> str:
    """java.exe → 它的 JAVA_HOME（.../bin/java.exe 的上一级）"""
    d = os.path.dirname(os.path.abspath(exe))
    if os.path.basename(d).lower() in ("bin", "jre", "server"):
        return os.path.dirname(d)
    return d


def is_valid_java(path: str) -> bool:
    """这个路径是不是能用的 java.exe（只看文件与命名，不启动进程）"""
    p = str(path or "").strip().strip('"')
    if not p or not os.path.isfile(p):
        return False
    if os.path.basename(p).lower() not in ("java.exe", "javaw.exe", "java"):
        return False
    return True


def _entry(exe: str, source: str = "") -> dict | None:
    if not is_valid_java(exe):
        return None
    home = java_home_of(exe)
    major, ver = _release_major(home)
    if not major:
        major = _guess_major_from_path(home)
    if not ver:
        ver = os.path.basename(home)
    return {"exe": os.path.abspath(exe), "home": home, "major": int(major or 0),
            "ver": str(ver), "source": source,
            "label": (f"Java {major}（{ver}）" if major else f"Java（{ver}）")}


def scan_all(refresh: bool = False) -> list:
    """本机能找到的所有 java.exe（按主版本从新到旧，去重）

    返回 [{exe, home, major, ver, source, label}]；`source` 是给玩家看的来源，
    例如「本软件安装」「系统」「官方启动器自带」。
    """
    now = time.time()
    if not refresh and _SCAN_CACHE["data"] and now - _SCAN_CACHE["t"] < _SCAN_TTL:
        return [dict(x) for x in _SCAN_CACHE["data"]]

    cands: list[tuple[str, str]] = []

    # 1) 本软件装的那份（<install_root>/<jdk-xx>/bin/java.exe）
    try:
        root = install_root()
        if os.path.isdir(root):
            for n in sorted(os.listdir(root)):
                cands.append((os.path.join(root, n, "bin", "java.exe"), "本软件安装"))
    except Exception:
        pass

    # 2) JAVA_HOME（玩家自己配过的话）
    jh = os.environ.get("JAVA_HOME") or ""
    if jh:
        cands.append((os.path.join(jh, "bin", "java.exe"), "JAVA_HOME"))

    # 3) 系统常规安装目录 / 各发行版
    import glob as _glob
    for pat in _JAVA_GLOBS:
        for p in _glob.glob(os.path.expandvars(pat)):
            cands.append((p, "系统安装"))

    # 4) PATH 里的 java（可能是 shutil.which 找到的，也可能是外面的目录）
    try:
        w = shutil.which("java")
        if w:
            cands.append((w, "PATH"))
    except Exception:
        pass

    # 5) 官方启动器自带的 runtime（递归扫最贵：冷盘 240ms）
    #    第五十三轮：改成借 loader 的后台预热结果 —— 那边已经在后台线程扫过一次，
    #    这里直接拿，没扫完就用当前已有的（缺了以后再打开设置页就补上了）。
    _ld = None
    try:
        from . import loader as _ld
        _ld._warm_deep_java()
        if not _ld._DEEP_CACHE["ready"]:
            # 后台还在扫：给它 0.4s，扫完就用；还没完就这次先不列（30s 后重扫就有）
            _ld._wait_deep_java(0.4)
        deep = list(_ld._DEEP_CACHE["list"])
    except Exception:                                     # noqa: BLE001
        deep = []
    if not deep and not (_ld is not None and _ld._DEEP_CACHE.get("busy")):
        # 后台线程根本没起来（极端情况）：才退回同步扫，否则宁可这次少几项
        for pat in _JAVA_DEEP_GLOBS:
            try:
                deep.extend(_glob.glob(os.path.expandvars(pat), recursive=True))
            except Exception:                             # noqa: BLE001
                pass
    for p in deep:
        cands.append((p, "官方启动器自带"))

    out, seen = [], set()
    for exe, src in cands:
        try:
            key = os.path.normcase(os.path.abspath(exe))
        except Exception:
            continue
        if key in seen:
            continue
        e = _entry(exe, src)
        if e:
            seen.add(key)
            out.append(e)
    out.sort(key=lambda x: (-int(x.get("major") or 0), x.get("home") or ""))
    _SCAN_CACHE.update({"t": now, "data": out})
    return [dict(x) for x in out]


def clear_scan_cache():
    _SCAN_CACHE.update({"t": 0.0, "data": []})
    # 刚装完 Java：loader 那边的「候选列表 / 主版本 / 最佳 Java」也得一起失效，
    # 否则界面还拿着装之前那份缓存，看起来像没装上。
    try:
        from . import loader
        loader.clear_cache()
    except Exception:
        pass


def find_installed(major: int) -> str:
    """本机已装好的指定主版本 Java 目录（Java 安装目录 + 用户目录）"""
    roots = [install_root()]
    # 第一百零八轮：和 loader._java_candidates() 同一套口径 —— 不少机器
    # （本机的 27/25/21/17/8 五个 JDK 就是）全装在 Program Files (x86)，
    # 以前这里只看 64 位 Program Files，find_installed 永远返回空。
    for key in ("ProgramFiles", "ProgramFiles(x86)"):
        pf = os.environ.get(key)
        if pf:
            roots.append(os.path.join(pf, "Java"))
    seen = set()
    for root in roots:
        key = os.path.normcase(root)
        if key in seen or not os.path.isdir(root):
            continue
        seen.add(key)
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for n in names:
            home = os.path.join(root, n)
            exe = os.path.join(home, "bin", "java.exe")
            if os.path.isfile(exe) and _major_of(exe) == int(major):
                return home
    return ""


# ---------------------------------------------------------------- 下载 / 解压
def _download(url: str, dest: str, task_id, expect_size: int = 0,
              sha256: str = "") -> str:
    """下载到 dest（进度/速度/取消 + 太慢自动判失败，让调用方换源）。"""
    tmp = dest + ".part"
    ok = False
    try:
        r = _get(url, timeout=60, stream=True)
        r.raise_for_status()
        ctype = (r.headers.get("content-type") or "").lower()
        total = int(r.headers.get("content-length") or 0) or expect_size
        if "html" in ctype:
            raise RuntimeError("源返回的是网页，不是安装包")
        TASKS.replace_total(task_id, total)
        done = 0
        h = hashlib.sha256()
        t0 = time.time()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(262144):
                if TASKS.is_cancelled(task_id):
                    raise RuntimeError("已取消")
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                TASKS.set_bytes(task_id, done, total)
                el = time.time() - t0
                if el > SPEED_WINDOW and done / el < MIN_SPEED:
                    raise RuntimeError(f"速度太慢（{int(done / el / 1024)} KB/s）")
        if sha256 and h.hexdigest().lower() != sha256.lower():
            raise RuntimeError("安装包校验失败（可能下载不完整）")
        os.replace(tmp, dest)
        ok = True
        return dest
    finally:
        if not ok and os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def _extract(zpath: str, root: str, task_id) -> str:
    """解压到 root，返回解出来的目录名（如 jdk-21.0.12.1+1）"""
    with zipfile.ZipFile(zpath) as z:
        entries = [i for i in z.infolist() if not i.is_dir()]
        # zip 里通常只有一个顶层目录，里面是 bin/java.exe
        tops = {i.filename.split("/")[0] for i in entries if "/" in i.filename}
        top = ""
        for cand in tops:
            if any(i.filename == f"{cand}/bin/java.exe" for i in entries):
                top = cand
                break
        if not top and tops:
            top = sorted(tops)[0]
        target = os.path.join(root, top) if top else root
        if top and os.path.isfile(os.path.join(target, "bin", "java.exe")):
            TASKS.set_phase(task_id, "已存在同版本，跳过解压")
            return top
        TASKS.set_items(task_id, 0, len(entries))
        for n, item in enumerate(entries, 1):
            if TASKS.is_cancelled(task_id):
                raise RuntimeError("已取消")
            z.extract(item, root)
            if n % 40 == 0 or n == len(entries):
                TASKS.set_items(task_id, n, len(entries), phase="解压 Java")
    if not top:
        # 没有顶层目录时，反查 java.exe 所在目录
        for cur, _dirs, files in os.walk(root):
            if "java.exe" in [f.lower() for f in files] and \
                    os.path.basename(cur).lower() == "bin":
                top = os.path.basename(os.path.dirname(cur))
                break
    return top


# ---------------------------------------------------------------- 环境变量
def _set_env(name: str, value: str, system: bool) -> bool:
    import winreg
    if system:
        path = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
        root = winreg.HKEY_LOCAL_MACHINE
    else:
        path = r"Environment"
        root = winreg.HKEY_CURRENT_USER
    try:
        key = winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS)
    except OSError:
        return False
    try:
        winreg.SetValueEx(key, name, 0, winreg.REG_EXPAND_SZ, value)
        return True
    except Exception:
        return False
    finally:
        key.Close()


def _get_env_raw(name: str, system: bool) -> tuple[str, int]:
    import winreg
    if system:
        path = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
        root = winreg.HKEY_LOCAL_MACHINE
    else:
        path = r"Environment"
        root = winreg.HKEY_CURRENT_USER
    try:
        key = winreg.OpenKey(root, path, 0, winreg.KEY_READ)
    except OSError:
        return "", winreg.REG_EXPAND_SZ
    try:
        val, typ = winreg.QueryValueEx(key, name)
        return str(val), typ
    except Exception:
        return "", winreg.REG_EXPAND_SZ
    finally:
        key.Close()


def _broadcast_env():
    """通知资源管理器/新进程环境变量已变（不影响已运行进程）"""
    try:
        import ctypes
        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 3000, None)
    except Exception:
        pass


def register_env(jhome: str) -> str:
    """写 JAVA_HOME + PATH。返回给用户看的说明。"""
    system = is_admin()
    _set_env("JAVA_HOME", jhome, system)
    cur, typ = _get_env_raw("Path", system)
    parts = [p for p in (cur or "").split(";") if p.strip()]
    new = r"%JAVA_HOME%\bin"
    if not any(p.lower().strip() in (new.lower(), (jhome + r"\bin").lower()) for p in parts):
        parts.insert(0, new)
        try:
            import winreg
            path = (r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
                    if system else r"Environment")
            root = winreg.HKEY_LOCAL_MACHINE if system else winreg.HKEY_CURRENT_USER
            key = winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS)
            winreg.SetValueEx(key, "Path", 0,
                              typ if typ in (winreg.REG_SZ, winreg.REG_EXPAND_SZ)
                              else winreg.REG_EXPAND_SZ,
                              ";".join(parts))
            key.Close()
        except Exception:
            pass
    _broadcast_env()
    os.environ["JAVA_HOME"] = jhome
    os.environ["PATH"] = jhome + r"\bin;" + os.environ.get("PATH", "")
    # 第四十三轮起软件默认就是普通权限：写 HKCU 是常态，不是「降级」，
    # 所以提示语只说明「只对当前用户生效」，别提管理员。
    return ("已设为系统 JAVA_HOME" if system
            else "已为当前用户设置 JAVA_HOME 与 PATH（只影响当前用户，不改系统环境变量）")


# ---------------------------------------------------------------- 主流程
def install(major: int, task_id) -> tuple[bool, str]:
    """下载并安装指定主版本的 Java。工作线程调用。"""
    TASKS.set_phase(task_id, f"检查本机 Java {major}")
    have = find_installed(major)
    if have:
        note = register_env(have)
        return True, f"本机已有 Java {major}（{have}），无需重复下载，{note}"

    TASKS.set_phase(task_id, f"获取 Java {major} 下载地址")
    try:
        cands = candidates(major)
    except Exception as e:                                # noqa: BLE001
        return False, f"获取下载地址失败：{e}"
    if not cands:
        return False, f"拿不到 Java {major} 的下载地址（所有源都没响应，请检查网络）"

    root = install_root()
    try:
        os.makedirs(root, exist_ok=True)
    except Exception as e:                                # noqa: BLE001
        return False, f"无法创建安装目录 {root}：{e}"

    tmpdir = os.path.join(tempfile.gettempdir(), "mcassist_java")
    os.makedirs(tmpdir, exist_ok=True)
    zpath = os.path.join(tmpdir, f"jdk-{major}-windows-x64.zip")

    info = None
    errs = []
    for c in cands:
        size_mb = c["size"] // (1024 * 1024) if c.get("size") else 0
        TASKS.set_phase(task_id, f"下载 Java {major}"
                        + (f"（约 {size_mb} MB · {c['source']}）" if size_mb
                           else f"（{c['source']}）"))
        TASKS.replace_total(task_id, 0)
        try:
            _download(c["url"], zpath, task_id, c.get("size") or 0,
                      c.get("sha256") or "")
            info = c
            break
        except Exception as e:                            # noqa: BLE001
            if "已取消" in str(e):
                return False, "已取消"
            errs.append(f"{c['source']}：{str(e)[:50]}")
            TASKS.set_phase(task_id, f"{c['source']} 不可用，换下一个源…")
    if info is None:
        return False, "下载失败（" + "；".join(errs) + "）"

    TASKS.replace_total(task_id, 0)
    TASKS.set_phase(task_id, "解压安装 Java（约 300 MB）")
    try:
        top = _extract(zpath, root, task_id)
    except Exception as e:                                # noqa: BLE001
        return False, f"解压失败：{str(e)[:120]}"
    finally:
        try:
            os.remove(zpath)
        except Exception:
            pass

    jhome = os.path.join(root, top) if top else root
    if not os.path.isfile(os.path.join(jhome, "bin", "java.exe")):
        return False, f"解压后没有找到 java.exe（{jhome}）"
    note = register_env(jhome)
    TASKS.set_phase(task_id, "完成")
    return True, f"已安装 Java {major} 到 {jhome}（来源：{info['source']}），{note}"


def uninstall_hint() -> str:
    return ("安装位置：" + install_root() + "（可在资源管理器中直接删除，"
            "并清掉 JAVA_HOME 环境变量）")


def clean_temp():
    """清掉遗留的下载缓存"""
    d = os.path.join(tempfile.gettempdir(), "mcassist_java")
    try:
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass
