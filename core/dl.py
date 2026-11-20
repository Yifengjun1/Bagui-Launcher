# -*- coding: utf-8 -*-
"""下载引擎：多文件并发 + 单文件分段 + 已存在跳过 + 失败重试。

为什么要这样写（此前慢的根因）：
- 老实现是「一条连接、一个文件、下一个再下一个」，80 个模组就是 80 次串行等待；
- 单个大文件（client.jar / 大模组）只吃一条 TCP 流，常被 CDN 限在 1~4 MB/s，
  带宽根本跑不满；
- 重复安装时把已经存在的文件整个重下一遍。

现在的策略（与主流启动器一致）：
1. 多个文件同时下（默认 8 路），小文件不再排队；
2. 单个大文件按 Range 切段并发（默认 4 段，>2MB 就切），拼回整文件；
3. 目标文件已存在且大小与预期一致 -> 直接跳过；
4. 每段/整文件失败重试 2 次（指数退避），全部失败才报错；
5. 连接池复用（Keep-Alive），块大小 256KB，减少握手与循环开销；
6. 每条工作线程复用自己的长连接，连续下多个文件时不再重复握手；
7. 分段失败自动退回单流，不让一次失败的并发拖慢整体。

线程模型：所有函数都在工作线程里跑；进度只通过回调上报纯数字，
由调用方（任务中心）落成数据，界面再轮询 —— 不在任何线程里碰控件。
"""
import os
import threading
import time

import requests
from requests.adapters import HTTPAdapter

UA = "MC-GuanQi-Fuzhu-Gongju/1.0.0"

CHUNK = 256 << 10                # 256 KB（实测小块读取比 1MB 更快、更稳定）
# 分段阈值：实测 CDN 限的是「每条连接」的速率而不是总带宽——同一条流只有
# 60~80 KB/s，开 4 条流总吞吐能到 170 KB/s 左右。所以 512KB 以上就值得切：
# 收尾阶段剩下的都是 100KB~2MB 的小模组，阈值定 2MB 会让它们全部单流，
# 结果就是「大件下完、速度断崖塌到 60KB/s」。
SEG_MIN = 512 << 10
# 单文件最多切几段。上限不是硬卡带宽，而是防止一个 4GB 文件切 64 段后
# 同时持有 64 个 .part 句柄、合并时 64 次顺序 IO。
SEG_MAX_FILE = 16
SEG_MAX = SEG_MAX_FILE           # 兼容旧名字
FILE_WORKERS = 64                # 总连接预算（旧名；实际值见 threads()）
# 同时开几个文件。上限不是带宽问题，是句柄与磁盘 IO：64 个文件各 1 段不如
# 16 个文件各 4 段 —— 后者在收尾阶段能留住并发。
MAX_FILE_CONC = 16
RETRY = 2                        # 每段重试次数

_TIMEOUT = (8, 20)               # (连接, 读取)
PROBE_TIMEOUT = (5, 6)           # 探测用：宁可判「不支持分段」也不让用户干等


# ---------------------------------------------------------------- 配置
def _conf():
    """延迟导入：dl 在很底层被到处 import，顶层 import config 容易成环"""
    try:
        from . import config
        return config.CONFIG
    except Exception:                                  # noqa: BLE001
        return None


def threads():
    """总连接预算。用户设 1~128，0/非法值按 64。"""
    c = _conf()
    try:
        n = int(c.get("dl_threads", 64) or 0) if c else 64
    except Exception:                                  # noqa: BLE001
        n = 64
    if n <= 0:
        n = 64
    return max(1, min(128, n))


def speed_limit():
    """限速 KB/s，0 = 不限"""
    c = _conf()
    try:
        n = int(c.get("dl_speed_kb", 0) or 0) if c else 0
    except Exception:                                  # noqa: BLE001
        n = 0
    return max(0, n)


# ---------------------------------------------------------------- 限速
class _Throttle:
    """全局令牌桶（总带宽，不是每连接）。

    为什么必须全局：64 条连接各限 512KB/s 等于不限（32MB/s）。PCL 的语义
    同样是「总速率上限」，所以桶只有一个，所有连接一起扣额度。

    为什么要循环 + sleep ≤ 0.1s：一次睡太久会让请求看起来「长时间无数据」，
    64 线程 + 低速这种组合很容易撞上 requests 的 read timeout（20s）然后
    被误判成下载失败。小步多次睡，chunk 持续流动，永远不会超时。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._rate = 0.0        # bytes/s，0 = 不限
        self._allow = 0.0       # 当前可用令牌
        self._t = time.monotonic()

    def set_rate(self, kb):
        with self._lock:
            self._rate = float(max(0, int(kb)) * 1024)
            # 突发额度给 1 秒，避免刚开始时每个 chunk 都小睡一下
            self._allow = self._rate * 0.5
            self._t = time.monotonic()

    def take(self, n):
        rate = self._rate
        if rate <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                self._allow = min(rate, self._allow + (now - self._t) * rate)
                self._t = now
                if self._allow >= n:
                    self._allow -= n
                    return
                sleep = (n - self._allow) / rate
            time.sleep(min(0.1, max(0.005, sleep)))


THROTTLE = _Throttle()

# 滑块范围（UI 与校验共用一份，避免两处写死不一致）
MIN_THREADS = 1
MAX_THREADS = 64
MAX_KB = 102400                  # 100 MB/s 上限，再高滑块精度就废了
KB_STEP = 512

_rate_synced = None              # 上次 apply 过的 KB 值，用于「变了才重置桶」


def _sync_rate():
    """把配置里的限速同步给令牌桶。

    必须「变了才设」：`set_rate` 会把桶重置成半桶突发额度，每个文件都重置
    一次等于永远在拿突发额度，限速形同虚设。
    """
    global _rate_synced
    kb = speed_limit()
    if kb != _rate_synced:
        THROTTLE.set_rate(kb)
        _rate_synced = kb


def save_limits(threads=None, speed_kb=None):
    """写回下载参数（None 表示不改），并立刻让限速生效。"""
    c = _conf()
    if c is None:
        return
    if threads is not None:
        c.set("dl_threads", max(MIN_THREADS, min(MAX_THREADS, int(threads))))
    if speed_kb is not None:
        c.set("dl_speed_kb", max(0, min(MAX_KB, int(speed_kb))))
    _sync_rate()


def fmt_threads(v):
    return f"{int(v)} 条连接"


def fmt_speed(v):
    v = int(v)
    if v <= 0:
        return "无限制"
    if v % 1024 == 0:
        return f"{v // 1024} MB/s"
    return f"{v} KB/s"



# ---------------------------------------------------------------- 第 104 轮：URL 规整 / 强制重下
_HTTP_ASSETS = "http://resources.download.minecraft.net"
_INDEX_MARKS = ("version_manifest", "/indexes/", "/mc/game/versions",
                "versions.json", "/meta/")


def normalize_url(url) -> str:
    """http://resources.download.minecraft.net 强制升 https（PCL-CE 同款）。

    官方资源域名早就全站 https；清单里偶尔还留着老 http 前缀，明文请求会被
    运营商/代理拦成 403，表现是「资源补不齐」。
    """
    s = str(url or "")
    if s.lower().startswith(_HTTP_ASSETS):
        return "https://" + s[len("http://"):]
    return s


def must_revalidate(url) -> bool:
    """索引 / 清单类文件不该信本地缓存（PCL-CE `canUseExistsFile:false`）。

    `.json` 版本清单、assets 索引这类小文件一旦用旧的，后面成百上千个对象
    会按错的 hash 去下，错得无声无息。大文件（jar / 模组）不受影响。
    """
    low = str(url or "").lower().split("?", 1)[0]
    return low.endswith(".json") or any(m in low for m in _INDEX_MARKS)


# ---------------------------------------------------------------- 连接池
def make_session(pool=32):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Encoding": "identity"})
    ad = HTTPAdapter(pool_connections=pool, pool_maxsize=pool, max_retries=0,
                     pool_block=False)
    s.mount("https://", ad)
    s.mount("http://", ad)
    return s


SESSION = make_session()

_TL = threading.local()


def _session():
    """当前线程专用的 Session。

    每条连接都被限速，所以连接本身就是稀缺资源：同一线程连续下多个文件时
    复用同一条长连接（Keep-Alive），可以省掉每个文件的 TCP+TLS 握手。
    """
    s = getattr(_TL, "sess", None)
    if s is None:
        s = make_session(pool=8)
        _TL.sess = s
    return s


def _new_session():
    """保留旧名字：部分调用方还在用"""
    return _session()


def _probe(url, timeout=None):
    """探测文件大小与是否支持 Range。返回 (size, ranges, final_url)。

    HEAD 优先、1 字节 Range 兜底（第 52 轮实测：Modrinth CDN 上 HEAD 183ms、
    Range 0-0 要 368ms，HEAD 快一倍）。因为 Session 统一带
    `Accept-Encoding: identity`，HEAD 的 Content-Length 就是原始字节数，不会
    被压缩搞错。只有 CDN 不答 HEAD 时才走 Range，那时 `Content-Range` 里的
    total 是权威值。

    超时给得很短：探测只是「能不能分段」的优化，卡住的话直接按不分段处理，
    不能让用户为了一次探测白等半分钟。
    """
    timeout = timeout or PROBE_TIMEOUT
    try:
        r = _session().head(url, timeout=timeout, allow_redirects=True)
        if r.status_code in (200, 206):
            size = int(r.headers.get("content-length") or 0)
            ar = str(r.headers.get("accept-ranges") or "").lower()
            if size > 0:
                return size, ar == "bytes", r.url
    except Exception:                                  # noqa: BLE001
        pass
    # 有的 CDN 不答 HEAD：用一个 1 字节的 Range 请求探
    try:
        r = _session().get(url, headers={"Range": "bytes=0-0"}, stream=True,
                           timeout=timeout, allow_redirects=True)
        try:
            cr = r.headers.get("content-range") or ""
            if r.status_code == 206 and "/" in cr:
                total = int(cr.rsplit("/", 1)[-1])
                if total > 0:
                    return total, True, r.url
            size = int(r.headers.get("content-length") or 0)
            if size > 0:
                return size, False, r.url
        finally:
            r.close()
    except Exception:                                  # noqa: BLE001
        pass
    return 0, False, url


# ---------------------------------------------------------------- 单文件
def _stream_to(url, dest, progress_cb=None, cancel_flag=None, offset=None,
               end=None, headers=None):
    """一条连接把内容写进 dest。

    offset/end 都不为 None 时按闭区间 `bytes=offset-end` 取分段；
    分段必须带结束位置，否则每段都从自己起点读到文件尾，段与段重叠、合并必错。
    """
    if offset is not None and end is not None:
        h = {"Range": f"bytes={offset}-{end}"}
    elif offset is not None:
        h = {"Range": f"bytes={offset}-"}
    else:
        h = {}
    if headers:
        h.update(headers)
    got = 0
    # 第九十八轮：发请求之前先看一眼 —— 排队中的分片不必再握一次手再退
    if cancel_flag and cancel_flag[0]:
        raise RuntimeError("已取消")
    with _session().get(url, headers=h, stream=True, timeout=_TIMEOUT,
                        allow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(CHUNK):
                if cancel_flag and cancel_flag[0]:
                    raise RuntimeError("已取消")
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
                THROTTLE.take(len(chunk))
                if progress_cb:
                    progress_cb(got)
    return got


def download(url, dest, progress_cb=None, cancel_flag=None,
             expected_size=0, segments=SEG_MAX, skip_existing=True):
    """下载一个文件到 dest。

    progress_cb(done, total)   done/total 单位字节，total 可能为 0（未知）
    expected_size             已知大小（整合包索引里有 fileSize）时用于跳过与校验
    """
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    _sync_rate()                  # 用户中途改限速，下一次下载就生效
    # D6：老 http 资源前缀强制升 https；索引 / 清单一律强制校验重下，
    #     不能因为「本地有一份大小相同的」就跳过（canUseExistsFile=false）。
    url = normalize_url(url)
    if must_revalidate(url):
        skip_existing = False

    if skip_existing and expected_size and os.path.isfile(dest) and \
            os.path.getsize(dest) == expected_size:
        if progress_cb:
            progress_cb(expected_size, expected_size)
        return dest

    # 第九十八轮：**已经下好的文件仍然算数**（上面那个分支先走），剩下的就别再
    # 白探一次大小了 —— `_probe` 最长 6 秒，几百个排队文件叠起来就是「取消卡住」。
    if cancel_flag and cancel_flag[0]:
        raise RuntimeError("已取消")

    # 每文件先探一次大小（第 52 轮改成 1 字节 Range，见 _probe）：
    # 不能因为索引里带了 fileSize 就跳过 —— 索引若写小了，分段会按错的大小切，
    # 合并后字节数刚好等于 total、校验照样通过，但文件尾部是缺的（静默截断，
    # 整合包装出来直接坏）。探测现在只需几百毫秒，换这个正确性很划算。
    size, ranges, final = _probe(url)
    total = size or expected_size or 0
    if not total:
        # 探测失败（或服务器不给长度）：单流下完再说
        for attempt in range(RETRY + 1):
            try:
                got = _stream_to(final, dest, lambda d: progress_cb(d, 0) if progress_cb else None,
                                 cancel_flag)
                return dest
            except RuntimeError:
                raise
            except Exception:                          # noqa: BLE001
                if attempt >= RETRY:
                    raise
                time.sleep(0.6 * (attempt + 1))
        return dest

    if expected_size and total != expected_size and size:
        total = size

    def _single_stream():
        """单流下载（不分段），供小文件与分段失败时兜底"""
        last = {"v": 0}
        for attempt in range(RETRY + 1):
            try:
                def cb(d, _l=last):
                    last["v"] = d
                    if progress_cb:
                        progress_cb(d, total)
                got = _stream_to(final, dest, cb, cancel_flag)
                if total and got < total:
                    raise RuntimeError(f"下载不完整（{got}/{total}）")
                return dest
            except RuntimeError as e:
                if "已取消" in str(e):
                    raise
                if attempt >= RETRY:
                    raise
                time.sleep(0.6 * (attempt + 1))
        return dest

    # 小文件或服务器不支持 Range：单流
    if not ranges or total < SEG_MIN or segments <= 1:
        return _single_stream()

    # 分段并发
    nseg = max(1, min(segments, total // SEG_MIN, SEG_MAX))
    if nseg < 2:
        return _single_stream()

    def _segmented():
        part = total // nseg
        parts = []
        for i in range(nseg):
            start = i * part
            end = total - 1 if i == nseg - 1 else (start + part - 1)
            parts.append((start, end, dest + f".part{i}"))

        lock = threading.Lock()
        done = {"v": 0}

        def worker(start, end, path):
            want = end - start + 1
            last = {"v": 0}
            for attempt in range(RETRY + 1):
                try:
                    def cb(got, _last=last):
                        with lock:
                            done["v"] += got - _last["v"]
                            _last["v"] = got
                            cur = done["v"]
                        if progress_cb:
                            progress_cb(cur, total)
                    got = _stream_to(final, path, cb, cancel_flag,
                                     offset=start, end=end)
                    if got < want:
                        raise RuntimeError(f"分段不完整（{got}/{want}）")
                    return
                except RuntimeError as e:
                    if "已取消" in str(e):
                        raise
                    with lock:
                        done["v"] -= last["v"]
                    last["v"] = 0
                    if attempt >= RETRY:
                        raise
                    time.sleep(0.5 * (attempt + 1))
                except Exception:                      # noqa: BLE001
                    with lock:
                        done["v"] -= last["v"]
                    last["v"] = 0
                    if attempt >= RETRY:
                        raise
                    time.sleep(0.5 * (attempt + 1))

        try:
            # 线程里禁 GC：跨线程析构 QPixmap/QWidget 会直接访问冲突（见 core/gcguard）
            from . import gcguard as _gcg
            _w = _gcg.guarded(worker)
            ths = [threading.Thread(target=_w, args=p, daemon=True)
                   for p in parts]
            for t in ths:
                t.start()
            for t in ths:
                while t.is_alive():
                    t.join(0.2)
                    if cancel_flag and cancel_flag[0]:
                        # 让 worker 自己发现取消标志退出，再抛给上层。
                        # 第九十八轮：这里以前是 `for x in ths: x.join(5)` ——
                        # 一条卡死的连接要等读超时（20 秒），最坏 16 条顺序 join
                        # 就是几十秒，玩家看到的就是「正在取消」转不停。
                        # 现在给整批 3 秒总预算，到点直接放弃（分片文件在 finally 里清）。
                        _dl_deadline = time.time() + 3.0
                        for x in ths:
                            _left = _dl_deadline - time.time()
                            if _left <= 0:
                                break
                            x.join(_left)
                        raise RuntimeError("已取消")
            with open(dest, "wb") as out:
                for _s, _e, path in parts:
                    with open(path, "rb") as f:
                        while True:
                            buf = f.read(CHUNK)
                            if not buf:
                                break
                            out.write(buf)
            if total and os.path.getsize(dest) != total:
                raise RuntimeError("分段合并后大小不符")
            return dest
        finally:
            for _s, _e, path in parts:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass

    try:
        return _segmented()
    except RuntimeError as e:
        if "已取消" in str(e):
            raise
        # 有的 CDN 声明支持 Range 却在分段时返回整文件/断连：退回单流，
        # 不能让用户为了一次失败的并发白等更久。
        return _single_stream()
    except Exception:                                  # noqa: BLE001
        return _single_stream()


# ---------------------------------------------------------------- 多文件
def download_many(jobs, cancel_flag=None, on_item=None, on_bytes=None,
                  concurrency=FILE_WORKERS):
    """并发下载多个文件。

    jobs: [{"index": int, "url": str, "dest": str, "size": int(可 0)}]
    on_item(index, state, note="")   state: run / done / fail / skip
    on_bytes(done, total)            总进度（累加值）
    返回 (ok_count, failed: [(index, reason)])
    """
    total_all = sum(int(j.get("size") or 0) for j in jobs)
    _sync_rate()                  # 批量入口同样同步一次限速
    lock = threading.Lock()
    agg = {"done": 0}
    failed = []
    ok = 0

    # 连接预算按「实际还要下几个文件」动态分配（第 52 轮）：
    # 固定「6 文件 × 4 段 = 24 条」在收尾时只剩 1 个文件，就变成 1 条连接，
    # 而 CDN 是按单连接限速的（实测 60~80 KB/s）→ 速度断崖塌到 60KB/s。
    # 现在把用不掉的预算全给剩下的文件：1 个文件就敢切 16 段。
    # 文件并发本身也得吃预算：concurrency 的默认值是模块加载时定死的
    # FILE_WORKERS=64，改配置不会变；不夹一下就会「用户设 8 线程、实际开 64 个」。
    # 再给文件并发一个上限，否则上百个文件会一口气开 64 个句柄、每个只剩 1 段，
    # 反而比「少开几个文件、每个多切几段」慢。
    budget = threads()
    want = int(concurrency or budget)
    real_conc = max(1, min(want, budget, MAX_FILE_CONC, len(jobs) or 1))
    per_file = max(1, min(SEG_MAX_FILE, budget // real_conc))

    def report(delta):
        with lock:
            agg["done"] += delta
            cur = agg["done"]
        if on_bytes:
            on_bytes(cur, total_all)

    def run_one(job):
        idx = job["index"]
        if on_item:
            on_item(idx, "run")
        if cancel_flag and cancel_flag[0]:
            if on_item:
                on_item(idx, "skip", "已取消")
            raise RuntimeError("已取消")
        last = {"v": 0}

        def cb(done, total):
            report(done - last["v"])
            last["v"] = done

        try:
            download(job["url"], job["dest"], progress_cb=cb,
                     cancel_flag=cancel_flag, expected_size=int(job.get("size") or 0),
                     segments=per_file)
        except Exception as e:                         # noqa: BLE001
            msg = str(e)
            if "已取消" in msg:
                if on_item:
                    on_item(idx, "skip", "已取消")
                raise
            # 失败也要把已计入的字节抵掉，避免总进度虚高
            report(-last["v"])
            with lock:
                failed.append((idx, msg[:80]))
            if on_item:
                on_item(idx, "fail", msg[:60])
            return False
        if on_item:
            on_item(idx, "done")
        return True

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from . import gcguard as _gcg
    with ThreadPoolExecutor(max_workers=real_conc) as ex:
        futs = {ex.submit(_gcg.guarded(run_one), j): j for j in jobs}
        for f in as_completed(futs):
            try:
                if f.result():
                    ok += 1
            except RuntimeError as e:
                if "已取消" in str(e):
                    for other in futs:
                        other.cancel()
                    break
            except Exception:                          # noqa: BLE001
                pass
    return ok, failed


def stats():
    """给设置/调试用：当前引擎参数"""
    return {"segments": SEG_MAX, "workers": FILE_WORKERS, "chunk": CHUNK,
            "segment_min": SEG_MIN, "retry": RETRY}
