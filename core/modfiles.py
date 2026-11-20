# -*- coding: utf-8 -*-
"""mods 目录里的文件操作（第六十四轮）：禁用 / 启用 / 删除。

**禁用 = 把 `xxx.jar` 改名成 `xxx.jar.disabled`。** 这是 Minecraft 模组圈通行的做法：
加载器只认 `.jar` 后缀，改名之后它就被整个忽略，但文件还在 —— 想恢复改回来即可，
不用重新下载。同理光影 / 材质包也能这么停用。

为什么要单独一个模块：概览页的内嵌浏览面板（`ui/modlist.py`）和内置文件管理器
（`ui/fspicker.py`）两处都要这套规则，塞在任一 UI 文件里都会被另一处反向 import
搞成循环，而且这套逻辑不该依赖 Qt（探针要能直接测）。
"""
import os

DISABLED_SUFFIX = ".disabled"
# 这些后缀算「模组」（含已禁用的），UI 靠它决定要不要给「禁用」按钮
_MOD_EXT = (".jar",)


def is_disabled(name: str) -> bool:
    """`a.jar.disabled` / `某个文件夹.disabled` 都算禁用态"""
    return str(name or "").lower().endswith(DISABLED_SUFFIX)


def is_mod(name: str) -> bool:
    """是模组文件吗（`a.jar` 与 `a.jar.disabled` 都算）"""
    low = str(name or "").lower()
    if low.endswith(DISABLED_SUFFIX):
        low = low[:-len(DISABLED_SUFFIX)]
    return low.endswith(_MOD_EXT)


def disabled_name(name: str) -> str:
    name = str(name or "")
    return name if is_disabled(name) else name + DISABLED_SUFFIX


def enabled_name(name: str) -> str:
    name = str(name or "")
    if is_disabled(name):
        return name[:-len(DISABLED_SUFFIX)]
    return name


def human_size(n) -> str:
    """1234567 → 1.2 MB（列表里显示用）"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return (f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}")
        n /= 1024.0
    return f"{n:.1f} GB"


def set_disabled(path: str, disabled: bool) -> tuple:
    """改名切换启用 / 禁用。返回 (ok, 新路径, 给玩家看的话)。

    为什么用改名而不是移到另一个文件夹：加载器只扫 `mods/` 顶层，改名后它自然
    读不到，但文件还在原地 —— 玩家在资源管理器里也一眼能看出「哦这个我关了」，
    不会以为文件丢了。而且存量整合包里的 `.disabled` 也是这个约定，互通。
    """
    path = os.path.abspath(str(path or ""))
    if not os.path.isfile(path):
        return False, path, "文件不见了（可能刚被删掉）"
    cur = os.path.basename(path)
    want = disabled_name(cur) if disabled else enabled_name(cur)
    if want == cur:
        return True, path, "已经是这个状态了"
    dest = os.path.join(os.path.dirname(path), want)
    if os.path.exists(dest):
        # 不覆盖：`.disabled` 和 `.jar` 同时存在时，覆盖掉哪一份都可能丢玩家东西
        return False, path, f"已经有同名文件了：{want}"
    try:
        os.rename(path, dest)
    except OSError as e:
        return False, path, f"改名失败（游戏是不是还开着？）：{e}"
    return True, dest, ("已禁用，游戏下次启动不会加载它" if disabled else "已启用")


def delete_to_trash(path: str) -> tuple:
    """删除单个文件 —— 走回收站，删错了还能捡回来。

    ⚠ 玩家数据：绝不 `os.remove()`。模组目录里的东西虽然能重下，但整合包自带的
    定制 jar 往往没地方下载，永久删除是不可逆的。
    """
    path = os.path.abspath(str(path or ""))
    if not os.path.isfile(path):
        return False, "文件不见了（可能刚被删掉）"
    try:
        from .installer import move_to_recycle
    except Exception:                                    # noqa: BLE001
        return False, "删除功能不可用"
    return move_to_recycle(path)


def list_files(folder: str, limit: int = 400) -> tuple:
    """列出一个目录里的文件（不含子目录）。返回 (rows, 被截掉的个数)。

    rows 里每项：{name, path, size, disabled, mod, mtime}
    排序：启用的在前、禁用的在后，各自按名字排 —— 玩家一眼看到「哪些还开着」。

    第六十七轮补丁：顺带把 `mtime` 也带出来。`e.stat()` 本来就调了（取 size），
    mtime 是白拿的 —— 上层算图标 / 排队读包就不用再 `getmtime` 一次。
    这个目录上一次 stat 约 0.7ms，213 个文件能省下 150~300ms。
    """
    folder = str(folder or "")
    rows = []
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    if not e.is_file():
                        continue
                    st = e.stat()
                except OSError:
                    continue
                rows.append({
                    "name": e.name,
                    "path": e.path,
                    "size": int(getattr(st, "st_size", 0) or 0),
                    "mtime": float(getattr(st, "st_mtime", 0.0) or 0.0),
                    "disabled": is_disabled(e.name),
                    "mod": is_mod(e.name),
                })
    except OSError:
        return [], 0
    rows.sort(key=lambda r: (r["disabled"], r["name"].lower()))
    cut = max(0, len(rows) - max(1, int(limit)))
    return rows[:max(1, int(limit))], cut
