# -*- coding: utf-8 -*-
"""全局下载/安装任务中心。

设计要点（为什么不用信号）：
- 任务是**纯 Python 数据**，工作线程只改数据（加锁），从不碰 QWidget；
- UI 侧用定时器轮询 snapshot() 渲染，彻底避开「工作线程操作界面/对象已销毁」的崩溃；
- 取消靠共享 cancel_flag，下载循环每块检查；取消后回调不再更新任何界面对象。

一次任务可覆盖「下载 -> 解压/安装」多个阶段，进度同时记录字节数与条目数，
供界面展示「已下载 / 总大小 · 速度 · 剩余时间」。
"""
import itertools
import threading
import time


STATUS_CN = {
    "running": "进行中",
    "done": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


class Task:
    __slots__ = ("tid", "title", "kind", "phase", "done_bytes", "total_bytes",
                 "done_items", "total_items", "status", "message", "created",
                 "updated", "cancel_flag", "background", "speed", "started",
                 "details", "_last_bytes", "_last_t", "_last_done_items")

    def __init__(self, tid, title, kind=""):
        now = time.time()
        self.tid = tid
        self.title = title
        self.kind = kind
        self.phase = "准备中"
        self.done_bytes = 0
        self.total_bytes = 0
        self.done_items = 0
        self.total_items = 0
        self.status = "running"      # running / done / failed / cancelled
        self.message = ""
        self.created = now
        self.updated = now
        self.cancel_flag = [False]
        self.background = False
        self.speed = 0.0             # 字节/秒
        self.started = now
        # 依赖明细：[{"name","size","status","note"}]，status = wait/run/done/fail/skip
        self.details = []
        self._last_bytes = 0
        self._last_t = now
        self._last_done_items = 0

    # ---------------- 派生值 ----------------
    @property
    def fraction(self):
        if self.total_bytes:
            return max(0.0, min(1.0, self.done_bytes / self.total_bytes))
        if self.total_items:
            return max(0.0, min(1.0, self.done_items / self.total_items))
        return 0.0

    @property
    def eta(self):
        if self.status != "running" or not self.speed or not self.total_bytes:
            return -1.0
        left = max(0, self.total_bytes - self.done_bytes)
        return left / self.speed if self.speed > 1 else -1.0

    def copy(self):
        t = Task(self.tid, self.title, self.kind)
        t.phase = self.phase
        t.done_bytes = self.done_bytes
        t.total_bytes = self.total_bytes
        t.done_items = self.done_items
        t.total_items = self.total_items
        t.status = self.status
        t.message = self.message
        t.created = self.created
        t.updated = self.updated
        t.cancel_flag = self.cancel_flag
        t.background = self.background
        t.speed = self.speed
        t.started = self.started
        t.details = [dict(d) for d in self.details]
        return t

    def __repr__(self):
        return (f"<Task {self.tid} {self.title!r} {self.phase} "
                f"{self.fraction:.0%} {self.status}>")


class TaskCenter:
    def __init__(self):
        self._lock = threading.RLock()
        self._tasks = {}
        self._seq = itertools.count(1)

    # ---------------- 生命周期 ----------------
    def create(self, title, kind=""):
        with self._lock:
            tid = f"t{next(self._seq)}"
            t = Task(tid, title, kind)
            self._tasks[tid] = t
            return t

    def get(self, tid):
        with self._lock:
            return self._tasks.get(tid)

    def exists(self, tid):
        with self._lock:
            return tid in self._tasks

    # ---------------- 更新 ----------------
    def set_phase(self, tid, phase, message=""):
        with self._lock:
            t = self._tasks.get(tid)
            if not t or t.status != "running" or t.cancel_flag[0]:
                return
            t.phase = phase or t.phase
            if message:
                t.message = message
            t.updated = time.time()

    def set_bytes(self, tid, done, total):
        """下载进度（字节）。会自动计算速度（0.35s 窗口平滑）。"""
        with self._lock:
            t = self._tasks.get(tid)
            if not t or t.status != "running" or t.cancel_flag[0]:
                return
            now = time.time()
            t.done_bytes = int(done or 0)
            if total:
                t.total_bytes = int(total)
            dt = now - t._last_t
            if dt >= 0.35:
                inst = max(0.0, (t.done_bytes - t._last_bytes) / dt)
                t.speed = inst if t.speed <= 0 else (t.speed * 0.55 + inst * 0.45)
                t._last_bytes = t.done_bytes
                t._last_t = now
            t.updated = now

    def set_items(self, tid, done, total, phase=None):
        """条目进度（解压/安装/第 N 个文件）。"""
        with self._lock:
            t = self._tasks.get(tid)
            if not t or t.status != "running" or t.cancel_flag[0]:
                return
            t.done_items = int(done or 0)
            t.total_items = int(total or 0)
            if phase:
                t.phase = phase
            t.updated = time.time()

    def replace_total(self, tid, total, phase=None):
        """未知总大小后拿到总大小（或换阶段重置计数）。"""
        with self._lock:
            t = self._tasks.get(tid)
            if not t or t.cancel_flag[0]:
                return
            t.total_bytes = int(total or 0)
            if phase:
                t.phase = phase
            t.updated = time.time()

    # ---------------- 依赖明细 ----------------
    def set_details(self, tid, rows):
        """设置依赖明细列表（rows: [{"name","size","status"}]）"""
        with self._lock:
            t = self._tasks.get(tid)
            if not t or t.cancel_flag[0]:
                return
            out = []
            for r in rows or []:
                if isinstance(r, dict):
                    out.append({"name": str(r.get("name") or ""),
                                "size": int(r.get("size") or 0),
                                "status": str(r.get("status") or "wait"),
                                "note": str(r.get("note") or "")})
                else:
                    out.append({"name": str(r), "size": 0, "status": "wait", "note": ""})
            t.details = out
            t.updated = time.time()

    def mark_detail(self, tid, index, status, note=""):
        """更新第 index 条明细的状态（wait/run/done/fail/skip）"""
        with self._lock:
            t = self._tasks.get(tid)
            if not t or t.cancel_flag[0]:
                return
            if not (0 <= int(index) < len(t.details)):
                return
            t.details[int(index)]["status"] = str(status or "wait")
            if note:
                t.details[int(index)]["note"] = str(note)
            t.updated = time.time()

    def mark_detail_by_name(self, tid, name, status, note=""):
        with self._lock:
            t = self._tasks.get(tid)
            if not t or t.cancel_flag[0]:
                return
            for i, d in enumerate(t.details):
                if d.get("name") == name:
                    d["status"] = str(status or "wait")
                    if note:
                        d["note"] = str(note)
                    t.updated = time.time()
                    return i
            return -1

    def finish(self, tid, status="done", message=""):
        with self._lock:
            t = self._tasks.get(tid)
            if not t:
                return
            if t.status in ("done", "failed", "cancelled"):
                return
            t.status = status
            t.message = message
            t.updated = time.time()
            if status == "done":
                t.phase = "已完成"
                if t.total_bytes:
                    t.done_bytes = t.total_bytes
                if t.total_items:
                    t.done_items = t.total_items
                for d in t.details:
                    if d.get("status") in ("run", "wait"):
                        d["status"] = "done"
            elif status in ("cancelled", "failed"):
                for d in t.details:
                    if d.get("status") == "run":
                        d["status"] = "skip"

    # ---------------- 取消 / 清理 ----------------
    def cancel(self, tid):
        with self._lock:
            t = self._tasks.get(tid)
            if not t or t.status != "running" or t.cancel_flag[0]:
                return False
            t.cancel_flag[0] = True
            t.phase = "正在取消…"
            t.updated = time.time()
            return True

    def is_cancelled(self, tid):
        with self._lock:
            t = self._tasks.get(tid)
            return bool(t and t.cancel_flag[0])

    def dismiss(self, tid):
        with self._lock:
            self._tasks.pop(tid, None)

    def clear_finished(self, keep_seconds=8.0):
        """清理已结束且超过保留时间的任务（界面轮询里顺手调用）"""
        now = time.time()
        with self._lock:
            for tid, t in list(self._tasks.items()):
                if t.status != "running" and now - t.updated > keep_seconds:
                    self._tasks.pop(tid, None)

    def snapshot(self):
        with self._lock:
            return [t.copy() for t in self._tasks.values()]

    def running_count(self):
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.status == "running")

    def any_running(self):
        return self.running_count() > 0


TASKS = TaskCenter()


# ---------------------------------------------------------------- 便捷包装
def progress_for(tid, interval=0.12):
    """给 download_to 用的节流进度回调：工作线程只写任务数据。"""
    state = {"t": 0.0}

    def cb(done, total):
        now = time.monotonic()
        if now - state["t"] < interval:
            return
        state["t"] = now
        TASKS.set_bytes(tid, done, total)

    return cb


def items_progress_for(tid, total=None):
    """给安装/解压用：done,total,text"""
    def cb(done, tot, text=""):
        TASKS.set_items(tid, done, tot or total or 0, phase=text or None)
    return cb


# ---------------------------------------------------------------- 格式化
def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return (f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}")
        n /= 1024
    return f"{n:.1f} GB"


def human_speed(bps):
    if not bps or bps <= 0:
        return ""
    return human_size(bps) + "/s"


def human_eta(sec):
    if sec is None or sec < 0:
        return ""
    sec = int(sec)
    if sec < 60:
        return f"剩 {sec} 秒"
    if sec < 3600:
        return f"剩 {sec // 60} 分 {sec % 60} 秒"
    return f"剩 {sec // 3600} 时 {(sec % 3600) // 60} 分"
