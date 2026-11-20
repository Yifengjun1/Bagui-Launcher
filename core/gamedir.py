# -*- coding: utf-8 -*-
"""安装目录里的 `.minecraft` —— 按这台机器的实际情况生成，绝不硬塞假文件。

规则：
  · 目标目录不存在就建出来，并补齐 Minecraft 认的那几个标准子目录（空骨架）；
  · 本机已经有能用的游戏目录（%APPDATA%\\.minecraft、微软商店版）时，
    把里面**实际存在**的 versions/libraries/assets/mods/... 用 NTFS 目录联接
    （mklink /J）挂到新目录里 —— 零拷贝，不复制几十 G，也不动玩家的原始数据；
  · 已经在目标里的目录（真目录或联接）一律不动；
  · 找不到可复用的数据就只留空骨架，等玩家第一次下载/导入时自然填充。

这里刻意不写 `launcher_profiles.json`：那是官方启动器自己的文件，
我们伪造一份空档案只会让官方启动器报错。
"""
import os
import subprocess

# Minecraft 认的标准子目录（缺了会自己建，但先建好能让官方启动器少一次报错）
SKELETON = ("versions", "libraries", "assets", "mods", "resourcepacks",
            "shaderpacks", "saves", "logs", "crash-reports", "config",
            "screenshots")
# 能从旧游戏目录共享过来的（都是"实际存在才共享"，shared 才是大头）
SHARED = ("versions", "libraries", "assets", "mods", "saves",
          "resourcepacks", "shaderpacks", "config")

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def is_junction(path):
    try:
        import ctypes
        a = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return a != -1 and bool(a & FILE_ATTRIBUTE_REPARSE_POINT)
    except Exception:
        return False


def junction(dst, src):
    """在 dst 建指向 src 的目录联接（mklink /J，不需要管理员）"""
    if is_junction(dst):
        return (os.path.normcase(os.path.realpath(dst))
                == os.path.normcase(os.path.realpath(src)))
    if os.path.isdir(dst):
        try:
            if os.listdir(dst):
                return False           # 已有真实内容，绝不覆盖
            os.rmdir(dst)
        except OSError:
            return False
    try:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", dst, src],
                           capture_output=True, text=True, encoding="gbk",
                           errors="replace", timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        return r.returncode == 0 and os.path.isdir(dst)
    except Exception:
        return False


def looks_like_mc(d):
    """像个 .minecraft 吗（只有壳不算，得有内容才值得共享）"""
    if not d or not os.path.isdir(d):
        return False
    for name in ("versions", "libraries"):
        sub = os.path.join(d, name)
        try:
            if os.path.isdir(sub) and os.listdir(sub):
                return True
        except OSError:
            pass
    return os.path.isfile(os.path.join(d, "launcher_profiles.json"))


def candidates():
    """本机可能存在的游戏目录（按可信度排序）"""
    out = []
    ap = os.path.expandvars(r"%APPDATA%\.minecraft")
    if ap and "%APPDATA%" not in ap:
        out.append(ap)
    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        pkg = os.path.join(local, "Packages")
        try:
            for n in os.listdir(pkg):
                if n.lower().startswith("microsoft.4297127d64ec6"):
                    out.append(os.path.join(pkg, n, "LocalCache", "Roaming",
                                            ".minecraft"))
        except OSError:
            pass
    return out


def find_source(exclude=""):
    """找一个可以共享的现有游戏目录（排除 exclude 自己）"""
    ex = os.path.normcase(os.path.abspath(exclude)) if exclude else ""
    for c in candidates():
        if ex and os.path.normcase(os.path.abspath(c)) == ex:
            continue
        if looks_like_mc(c):
            return os.path.abspath(c)
    return ""


def ensure_game_dir(path, source=""):
    """按实际情况把 path 变成一个能用的 .minecraft。

    返回 {"path", "source", "linked": [...], "created": [...], "error": ""}
    """
    info = {"path": os.path.abspath(path), "source": "", "linked": [],
            "created": [], "error": ""}
    try:
        os.makedirs(info["path"], exist_ok=True)
    except OSError as e:
        info["error"] = str(e)
        return info

    src = os.path.abspath(source) if source else ""
    if src and os.path.normcase(src) != os.path.normcase(info["path"]) \
            and looks_like_mc(src):
        info["source"] = src
        for name in SHARED:
            s = os.path.join(src, name)
            if not os.path.isdir(s):
                continue                    # 原件没有的就不造
            d = os.path.join(info["path"], name)
            if os.path.exists(d) or is_junction(d):
                continue                    # 已有东西不动
            if junction(d, s):
                info["linked"].append(name)

    for name in SKELETON:
        d = os.path.join(info["path"], name)
        if os.path.isdir(d):
            continue
        try:
            os.makedirs(d, exist_ok=True)
            info["created"].append(name)
        except OSError as e:
            info["error"] = str(e)
    return info


def describe(info):
    """给界面用的一句话"""
    if not info:
        return ""
    if info.get("error"):
        return f"游戏目录创建失败：{info['error']}"
    bits = []
    if info.get("linked"):
        bits.append("已共享 " + "、".join(info["linked"]))
    if info.get("created"):
        bits.append("已新建 " + "、".join(info["created"]))
    if not bits:
        return "游戏目录已就绪"
    return "游戏目录已就绪（" + "；".join(bits) + "）"
