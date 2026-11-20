# -*- coding: utf-8 -*-
"""「总路径」（Osmanthus flower）整体搬迁 —— 第九十八轮（第九十九轮修语义）。

**总路径只装游戏本体**：应用路径 `.minecraft`、官方路径
`official_assets\\.minecraft`、游戏缓存、整合包。
**配置 / 数据目录（`%APPDATA%\\bgl_datat_config`：账号、设置、日志）不在总路径里** ——
改总路径不会动它，账号也不会跟着搬。这是第九十九轮玩家明确纠正的语义。

玩家在「设置 → 游戏目录」里把总路径改成别处之后：

  · 点「是」→ 记下待办 + 立刻重启；**新进程启动时**第一件事就是把整个
    Osmanthus flower 搬到新位置，然后照常用新路径起来；
  · 点「否」→ 只记下待办，**路径保持原样、连关窗口都不搬**；
    等玩家下次重开启动器时，由 main() 在启动阶段统一落地。

为什么不趁窗口开着 / 退出时搬：搬家要动 `versions/` `libraries/` `assets/`，
还有本进程正占着的缓存与日志。开着搬 = 一半文件走了一半没走，比不搬更糟；
而「点否」之后退出这一刻就搬，等于玩家还没重启路径就变了。

待办记在**数据目录**里（`%APPDATA%\\bgl_datat_config\\install_move.json`）：
它不在 Osmanthus flower 里面，搬完也不会跟着丢。
"""
import json
import os
import shutil
import time

from .config import CONFIG, default_data_dir
from .paths import (APP_DIR_NAME, INSTALL_DIR_NAME, OFFICIAL_DIR, _move_dir,
                    _rebase_after_move)

MARK_NAME = "install_move.json"

# 配置里存「绝对路径」的键：搬家之后必须跟着改，否则一开机就是「目录不存在」
_PATH_KEYS = ("install_dir", "game_dir", "official_game_dir", "app_game_override")


def _mark_path() -> str:
    return os.path.join(default_data_dir(), MARK_NAME)


def _same(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == \
            os.path.normcase(os.path.abspath(b))
    except Exception:                                     # noqa: BLE001
        return False


def _under(path: str, root: str) -> bool:
    """path 是不是在 root 里面（含 root 自己）"""
    if not path or not root:
        return False
    try:
        p = os.path.normcase(os.path.abspath(path))
        r = os.path.normcase(os.path.abspath(root))
    except Exception:                                     # noqa: BLE001
        return False
    return p == r or p.startswith(r + os.sep)


def same_volume(a: str, b: str) -> bool:
    """两个路径是不是同一个盘（同盘可以整目录改名，瞬间完成）"""
    try:
        return os.path.splitdrive(os.path.abspath(a))[0].lower() == \
            os.path.splitdrive(os.path.abspath(b))[0].lower()
    except Exception:                                     # noqa: BLE001
        return False


# ---------------------------------------------------------------- 待办记录
def pending() -> dict:
    """读待办；没有 / 读坏了都返回 {}"""
    try:
        with open(_mark_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {}
        if not (d.get("from") and d.get("to")):
            return {}
        return d
    except (OSError, ValueError):
        return {}


def set_pending(src: str, dst: str) -> tuple[bool, str]:
    """记下「开机后把 src 搬到 dst」"""
    src = os.path.abspath(str(src or ""))
    dst = os.path.abspath(str(dst or ""))
    if not src or not dst or _same(src, dst):
        return False, "路径没变"
    if os.path.isdir(dst):
        try:
            if os.listdir(dst):
                return False, "新位置已经有同名文件夹，而且不是空的"
        except OSError as e:
            return False, f"新位置读不到：{str(e)[:50]}"
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
    except OSError as e:
        return False, f"新位置建不出来：{str(e)[:50]}"
    try:
        with open(_mark_path(), "w", encoding="utf-8") as f:
            json.dump({"from": src, "to": dst,
                       "at": time.strftime("%Y-%m-%d %H:%M:%S")}, f,
                      ensure_ascii=False, indent=2)
    except OSError as e:
        return False, f"记录待办失败：{str(e)[:50]}"
    return True, dst


def clear_pending():
    try:
        if os.path.isfile(_mark_path()):
            os.remove(_mark_path())
    except OSError:
        pass


def new_root_for(chosen: str) -> str:
    """玩家选了一个文件夹 → 算出「总路径」应该是什么。

    选的就是 `...\\Osmanthus flower` 本身 → 原样用；选了别的目录 → 当成父目录，
    把 Osmanthus flower 放进去（玩家点「选文件夹」时一般选的是「放到哪个盘/哪个
    游戏文件夹下」，不会先自己建一个空壳再进去选）。
    """
    p = os.path.abspath(str(chosen or "").strip().strip('"'))
    if not p:
        return ""
    if os.path.basename(p).lower() == INSTALL_DIR_NAME.lower():
        return p
    return os.path.join(p, INSTALL_DIR_NAME)


# ---------------------------------------------------------------- 搬家
def _copy_tree(src: str, dst: str, progress=None) -> bool:
    """跨盘搬：先整体拷贝，成功后再删旧目录（拷不完就绝不删）"""
    try:
        if os.path.isdir(dst):
            if os.listdir(dst):
                return False
            os.rmdir(dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
    except OSError:
        return False
    total, done = 0, 0
    for base, _dirs, files in os.walk(src):
        for n in files:
            try:
                total += os.path.getsize(os.path.join(base, n))
            except OSError:
                pass
    if progress:
        try:
            progress(f"正在复制到新位置（共 {total // (1 << 20)} MB）…", 0)
        except Exception:                                 # noqa: BLE001
            pass
    try:
        for base, dirs, files in os.walk(src):
            rel = os.path.relpath(base, src)
            target = dst if rel == "." else os.path.join(dst, rel)
            os.makedirs(target, exist_ok=True)
            for n in files:
                s = os.path.join(base, n)
                try:
                    shutil.copy2(s, os.path.join(target, n))
                    done += os.path.getsize(s)
                except OSError:
                    continue                              # 单独一个文件失败不算整体失败
                if progress and total and done % (64 << 20) < (1 << 20):
                    try:
                        progress(f"正在复制到新位置… {int(done * 100 / total)}%",
                                 int(done * 100 / total))
                    except Exception:                     # noqa: BLE001
                        pass
    except Exception:                                     # noqa: BLE001
        return False
    # 拷完了再删：只在目标确实有内容时删，避免「两边都没了」
    try:
        if os.path.isdir(dst) and os.listdir(dst):
            shutil.rmtree(src, ignore_errors=True)
        else:
            return False
    except OSError:
        return False
    return True


def _rebase_data_dir(src: str, dst: str) -> bool:
    """异常兜底：玩家把数据 / 配置目录手动塞进总路径树里时，改指针别把账号弄丢。

    **总路径本身不含配置 / 数据目录**（见文件头），正常玩家永远走不到这里。
    但设置页允许单独改「数据 / 配置路径」，万一把它指到 Osmanthus flower 内部
    之后再改总路径，整棵树一搬 `config.json`（含账号 session）就跟着去了新位置，
    而指针还指着旧路径 —— 下次启动会在旧位置**新建一个空数据目录**，
    玩家看到的就是「账号没了」。这层防护只为这种手滑存在。

    规矩（见 config.py）：指针文件 `datadir.txt` 永远待在默认数据目录里，
    它不会被搬走，所以这里只改写它的内容 + 把当前进程的 CONFIG.path 切过去。
    """
    try:
        from .config import (CONFIG as _C, ROOT_POINTER, app_data_dir,
                             default_data_dir)
        cur = app_data_dir()
        if not _under(cur, src):
            return False
        new_dir = dst + os.path.abspath(cur)[len(os.path.abspath(src)):]
        try:
            os.makedirs(new_dir, exist_ok=True)
        except OSError:
            return False
        ptr = os.path.join(default_data_dir(), ROOT_POINTER)
        try:
            tmp = ptr + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(new_dir)
            os.replace(tmp, ptr)
        except OSError:
            return False
        _C.path = os.path.join(new_dir, "config.json")
        return True
    except Exception:                                     # noqa: BLE001
        return False


def _rebase_config(src: str, dst: str) -> int:
    """把配置里指在旧总路径下面的绝对路径全部改到新位置。

    `paths._rebase_after_move` 只管「独立启动目标 / 已导入文件夹」那些，
    `install_dir` / `game_dir` / `official_game_dir` / `app_game_override`
    这四条它不碰 —— 而它们恰恰是启动后立刻要用的，不改就是「目录不存在」。
    """
    changed = 0
    _rebase_data_dir(src, dst)
    for key in _PATH_KEYS:
        cur = str(CONFIG.get(key) or "").strip()
        if cur and _under(cur, src):
            CONFIG.set(key, dst + os.path.abspath(cur)[len(os.path.abspath(src)):])
            changed += 1
    try:
        changed += _rebase_after_move(src, dst)
    except Exception:                                     # noqa: BLE001
        pass
    # 第一百零一轮：搬完当次就把「已导入」三张表里的历史坏名字/坏分类修好，
    # 别让玩家等下次重开启动器才看到正常的文件夹名。
    try:
        from . import installer as _inst
        _inst.heal_rebased_metadata()
    except Exception:                                     # noqa: BLE001
        pass
    CONFIG.save()
    return changed


def run_pending(progress=None, only_same_volume: bool = False) -> tuple[bool, str]:
    """执行待办搬迁。返回 (是否搬过, 说明)。

    `only_same_volume=True`（关窗口时用）：跨盘就不动，留给下次启动 ——
    退出时拷几十 GB 会让玩家以为程序卡死。
    """
    job = pending()
    if not job:
        return False, ""
    src = os.path.abspath(job.get("from") or "")
    dst = os.path.abspath(job.get("to") or "")
    if not src or not dst or _same(src, dst):
        clear_pending()
        return False, ""

    # 旧位置已经没了：可能是上次搬完了没来得及清待办，也可能玩家自己搬过
    if not os.path.isdir(src):
        if os.path.isdir(dst):
            _rebase_config(src, dst)
            clear_pending()
            return True, f"总路径已经是 {dst}"
        return False, f"旧位置和新位置都找不到：{src}"

    if os.path.isdir(dst):
        try:
            if os.listdir(dst):
                return False, f"新位置已经有同名文件夹，先清空它再重启：{dst}"
            os.rmdir(dst)
        except OSError as e:
            return False, f"新位置动不了：{str(e)[:60]}"

    if same_volume(src, dst):
        if not _move_dir(src, dst):
            return False, "同一个盘内改名失败（可能被别的程序占着），关掉游戏和资源管理器后重启再试"
    else:
        if only_same_volume:
            return False, ""                              # 跨盘：留给下次启动
        if not _copy_tree(src, dst, progress):
            return False, "复制到新位置失败（磁盘空间不够或被占用）"

    _rebase_config(src, dst)
    clear_pending()
    return True, f"总路径已迁移到 {dst}"
