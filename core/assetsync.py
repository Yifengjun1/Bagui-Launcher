# -*- coding: utf-8 -*-
"""Minecraft 资源文件（``assets/objects``）补齐。

## 为什么需要这个模块

``assets`` 是**几千到上万个 10~100 KB 的小文件**，内容寻址（文件名就是 sha1），
由 ``assets/indexes/<id>.json`` 索引。缺了会表现成三种完全不同的症状，玩家
很难联想到同一个原因：

- 主菜单背景是一整片品红/黑棋盘格 —— 全景贴图缺失，显示的是 Mojang 的
  ``missingno`` 缺失纹理；
- 游戏里一点声音都没有 —— ``minecraft/sounds/**`` 全缺；
- 语言列表是空白的 —— ``*.json`` 语言文件本身就在 objects 里，读不到就没有
  任何语言可选，中文自然也切不了。

**这三种情况在装有官方启动器的机器上永远看不见**，因为那份 assets 早就下全了，
``paths.py`` 还会把官方目录联接过来复用。只有干净设备（新电脑 / 只装过本启动器）
才会暴露 —— 这就是「换台电脑就花屏、没声音、切不了语言」的根因。

## 为什么不用 ``core/dl.py``

``dl.download`` 是给几十 MB 的大文件做的：每个文件先发一次 HEAD 探测、再切多段
Range 并发。拿它下 assets 有两个问题：一是每个文件一次额外请求，上万次探测的
开销比下载本身还大；二是 CDN 对海量 Range 请求并不友好。所以这里单独实现
「固定并发 + 单次 GET + 流式落盘」，刻意做成两条独立的链路。

## 落盘约定

    assets/objects/<hash 前两位>/<hash>      没有扩展名，这是 Mojang 的约定
    assets/indexes/<id>.json                 索引，由调用方先保证存在

老版本（1.6 及更早）的索引带 ``virtual`` / ``map_to_resources`` 标记，需要把
objects 再按原名铺一份到 ``assets/virtual/<id>/`` 或 ``resources/``，见
``_lay_out_virtual``。
"""
import hashlib
import json
import os
import threading

from . import dl

# 资源站：官方只有一个；保留成元组是为了以后能加镜像
ASSET_BASES = ("https://resources.download.minecraft.net",)

_RETRY = 2                      # 每个地址最多试几次
_CHUNK = 64 << 10
_local = threading.local()


def _session():
    """每线程一个 Session：requests 的 Session 跨线程复用并不安全，而每次
    请求新建一个又吃握手开销，所以按线程缓存。"""
    s = getattr(_local, "s", None)
    if s is None:
        import requests
        s = requests.Session()
        s.headers.update({"User-Agent": "BaguLauncher/assets"})
        _local.s = s
    return s


def objects_root(game_dir):
    return os.path.join(game_dir, "assets", "objects")


def index_file(game_dir, index_id):
    return os.path.join(game_dir, "assets", "indexes", str(index_id) + ".json")


def workers(default=0):
    """小文件并发数。

    不吃满 ``dl.threads()`` 的总预算（那是给大文件分段留的），取四分之一并
    夹在 4~16 之间：太高会被 CDN 限速、还会让 Windows 的文件句柄和磁盘队列
    排队，实测 16 个并发已经能跑满家用带宽。
    """
    if default:
        return max(1, int(default))
    try:
        budget = int(dl.threads() or 64)
    except Exception:
        budget = 64
    return max(4, min(16, budget // 4))


def _is_cancelled(cancel):
    """取消标志沿用 dl.py 的约定：``[True]`` 表示取消。"""
    try:
        return bool(cancel and cancel[0])
    except Exception:
        return False


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n


def load_index(path):
    """读索引，坏了就返回空 dict（调用方按「没有可下的」处理）"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def missing(game_dir, index_path, verify_size=True):
    """返回 (待下 [(hash, size)], 待下体积，索引里的对象总数)。

    只比大小不算 sha1：算 hash 要读一遍磁盘，几万个文件加起来是几 GB 的读，
    而「大小对得上」已经能挡住绝大多数写坏/截断的情况。
    """
    root = objects_root(game_dir)
    objs = (load_index(index_path).get("objects") or {})
    todo, seen, total = [], set(), 0
    for _name, v in objs.items():
        if not isinstance(v, dict):
            continue
        h = str(v.get("hash") or "")
        if not h or h in seen:
            continue
        seen.add(h)
        size = int(v.get("size") or 0)
        dst = os.path.join(root, h[:2], h)
        try:
            if os.path.isfile(dst) and (not verify_size or not size
                                        or os.path.getsize(dst) == size):
                continue
        except OSError:
            pass
        todo.append((h, size))
        total += size
    return todo, total, len(objs)


def _fetch(h, size, root, verify=True):
    """下一个对象。成功返回 True。"""
    sub = h[:2]
    dst = os.path.join(root, sub, h)
    tmp = dst + ".part"
    try:
        if os.path.isfile(dst) and (not size or os.path.getsize(dst) == size):
            return True
    except OSError:
        pass
    try:
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    except OSError:
        return False
    for base in ASSET_BASES:
        for _try in range(_RETRY):
            f = None
            try:
                r = _session().get("%s/%s/%s" % (base, sub, h),
                                   timeout=(6, 30), stream=True)
                if r.status_code != 200:
                    raise RuntimeError("HTTP %s" % r.status_code)
                dig = hashlib.sha1() if verify else None
                got = 0
                f = open(tmp, "wb")
                for chunk in r.iter_content(_CHUNK):
                    if not chunk:
                        continue
                    f.write(chunk)
                    got += len(chunk)
                    if dig is not None:
                        dig.update(chunk)
                f.close()
                f = None
                r.close()
                if size and got != size:
                    raise RuntimeError("大小不符 %d/%d" % (got, size))
                if dig is not None and dig.hexdigest() != h:
                    raise RuntimeError("sha1 不符")
                os.replace(tmp, dst)
                return True
            except Exception:
                if f is not None:
                    try:
                        f.close()
                    except Exception:
                        pass
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return False


def _lay_out_virtual(game_dir, index_id, data):
    """老索引（1.6 及更早）要把 objects 按原名再铺一份。

    ``virtual``：铺到 ``assets/virtual/<index_id>/``，游戏按名字直接找；
    ``map_to_resources``：铺到 ``<game_dir>/resources/``。
    同一卷优先硬链接 —— 这几万个文件复制一遍要多占一倍空间。
    """
    virtual = bool(data.get("virtual"))
    mapped = bool(data.get("map_to_resources"))
    if not (virtual or mapped):
        return 0
    src_root = objects_root(game_dir)
    dst_base = (os.path.join(game_dir, "assets", "virtual", str(index_id))
                if virtual else os.path.join(game_dir, "resources"))
    made = 0
    for name, v in (data.get("objects") or {}).items():
        try:
            h = str((v or {}).get("hash") or "")
            if not h:
                continue
            src = os.path.join(src_root, h[:2], h)
            dst = os.path.join(dst_base, *str(name).split("/"))
            if not os.path.isfile(src) or os.path.isfile(dst):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                os.link(src, dst)
            except Exception:
                import shutil
                shutil.copy2(src, dst)
            made += 1
        except Exception:
            continue
    return made


def sync(game_dir, index_id, index_path="", progress=None, cancel=None,
         conc=0, verify=True):
    """按索引补齐 objects，返回统计 dict。

    ``progress(text)`` 只在文本里报进度 —— **绝不碰任何 QWidget**，界面自己
    轮询（第 52 轮定下的规矩，见 core/tasks.py）。
    """
    ipath = index_path or index_file(game_dir, index_id)
    data = load_index(ipath)
    if not data:
        return {"index": str(index_id), "ok": False, "error": "资源索引缺失或损坏",
                "total": 0, "missing": 0, "done": 0, "failed": 0, "bytes": 0}
    todo, total_bytes, total_objs = missing(game_dir, ipath)
    res = {"index": str(index_id), "ok": False, "error": "", "total": total_objs,
           "missing": len(todo), "done": 0, "failed": 0, "bytes": total_bytes}
    if not todo:
        res["ok"] = True
        _lay_out_virtual(game_dir, index_id, data)
        return res

    root = objects_root(game_dir)
    n = workers(conc)
    lock = threading.Lock()
    state = {"done": 0, "bytes": 0}
    failed = []
    if progress:
        try:
            progress("下载游戏资源 %d 个（%s）…" % (len(todo), fmt_size(total_bytes)))
        except Exception:
            pass

    def work(item):
        h, size = item
        if _is_cancelled(cancel):
            return
        ok = _fetch(h, size, root, verify=verify)
        with lock:
            if ok:
                state["done"] += 1
                state["bytes"] += size
            else:
                failed.append(h)
            k = state["done"] + len(failed)
            if progress and (k % 32 == 0 or k == len(todo)):
                try:
                    progress("下载游戏资源 %d/%d（%s / %s）"
                             % (k, len(todo), fmt_size(state["bytes"]),
                                fmt_size(total_bytes)))
                except Exception:
                    pass

    # 上千个文件不能一次性 submit（全部排进队列会占掉几 MB 内存和句柄）；
    # 用「滑动窗口」维持约 2 倍并发的在途任务。
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    win = max(2, n * 2)
    from . import gcguard as _gcg
    _work = _gcg.guarded(work)
    with ThreadPoolExecutor(max_workers=n) as pool:
        it = iter(todo)
        pending = set()
        for _ in range(win):
            try:
                pending.add(pool.submit(_work, next(it)))
            except StopIteration:
                break
        while pending:
            done_set, pending = wait(pending, return_when=FIRST_COMPLETED)
            for _ in done_set:
                if _is_cancelled(cancel):
                    break
                try:
                    pending.add(pool.submit(_work, next(it)))
                except StopIteration:
                    pass

    res["done"] = state["done"]
    res["failed"] = len(failed)
    if failed:
        res["error"] = "有 %d 个资源文件没下下来（网络不稳时重试一次通常就好）" % len(failed)
    else:
        res["ok"] = True
        _lay_out_virtual(game_dir, index_id, data)
    if progress:
        try:
            progress("游戏资源就绪：%d 个文件" % state["done"])
        except Exception:
            pass
    return res


def ensure(game_dir, data, progress=None, cancel=None, conc=0):
    """给启动/安装流程用的一站式入口，返回 (ok, 中文说明)。

    ``data`` 是版本 JSON（读 ``assetIndex`` 段）。索引自己不在就顺手补下 ——
    原来这里会抛「缺少资源索引，先用官方启动器启动一次」，那等于把责任推回
    玩家，干净设备上就是死循环。
    """
    ai = (data.get("assetIndex") or {})
    idx = str(ai.get("id") or data.get("assets") or "")
    if not idx:
        return True, ""            # 极老版本没有 assets，不算错
    ipath = index_file(game_dir, idx)
    if not os.path.isfile(ipath):
        url = str(ai.get("url") or "")
        if not url:
            return False, "缺少资源索引 assets/indexes/%s.json" % idx
        if progress:
            try:
                progress("补下资源索引 %s…" % idx)
            except Exception:
                pass
        try:
            dl.download(url, ipath, expected_size=int(ai.get("size") or 0))
        except Exception as e:
            return False, "资源索引下载失败（%s）：%s" % (idx, e)
    res = sync(game_dir, idx, ipath, progress=progress, cancel=cancel, conc=conc)
    if not res.get("ok"):
        return False, res.get("error") or "资源文件补齐失败"
    if res.get("done"):
        return True, "已补下游戏资源 %d 个（%s）" % (res["done"], fmt_size(res["bytes"]))
    return True, ""
