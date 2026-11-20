# -*- coding: utf-8 -*-
"""单实例守卫（第五十六轮）：同一个用户只允许开一个启动器窗口。

为什么不用「全局互斥量 + FindWindow」那一套：
  · 互斥量只能告诉第二份进程「已经有人了」，没法让它把第一个窗口叫到前面；
  · 靠窗口标题找窗口，标题一改（版本号 / 登录名 / 页面名）就失效。

用 QLocalServer / QLocalSocket（Windows 下就是命名管道）：
  · 不需要管理员权限、不写注册表、进程退出即释放；
  · 第二份进程连上去发一句 `show`，第一份收到就把窗口还原 / 置顶（顺带闪任务栏），
    然后第二份自己退出。

两个必须处理的现场：
  ① **上一个实例被强杀**（任务管理器 / 崩溃 / 蓝屏）会留下一个连不上的 socket。
     直接 listen 会报 AddressInUse，所以先 connect 探一下：连不上就 removeServer 清掉。
  ② 两个进程几乎同时启动：两边都连不上、都清、谁 listen 成功谁当老大，
     输的那个再 connect 一次，能连上就照样退出。

名字按用户区分（getpass + USERPROFILE 取哈希）：同一台机器多账户各开一个，
不会互相顶掉。
"""
import getpass
import hashlib
import os

from PySide6.QtCore import QObject, QSharedMemory, Signal

try:                                  # QtNetwork 是独立模块：极端裁剪的环境里可能没有
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    _HAS_NET = True
except Exception:                     # noqa: BLE001
    QLocalServer = QLocalSocket = None
    _HAS_NET = False

CONNECT_MS = 500
DEFAULT_KEY = "bagui-launcher"


def _tag() -> str:
    who = "%s|%s" % (getpass.getuser(), os.environ.get("USERPROFILE", ""))
    return hashlib.sha1(who.encode("utf-8", "ignore")).hexdigest()[:12]


class SingleGuard(QObject):
    """第一个实例 listen；后来的实例 try_acquire() 返回 False 后应立刻退出。"""

    activated = Signal()          # 第二个实例来敲门：把窗口叫出来

    def __init__(self, parent=None, key=None):
        super().__init__(parent)
        # 名字可以用环境变量覆盖：无人值守探针靠它避开玩家**正在运行**的那份 ——
        # 否则测试要么被真实实例挡回去（全红），要么把玩家的窗口叫到前面（更糟）。
        key = str(key or os.environ.get("MCASSIST_SINGLE_KEY") or DEFAULT_KEY)
        self.name = "%s-%s" % (key, _tag())
        self._server = None
        self._mem = None
        self._buf = b""

    # ------------------------------------------------------------ 抢
    def try_acquire(self) -> bool:
        """True = 本进程是唯一实例；False = 已经有一个在跑（已顺手唤醒它）

        **独占靠共享内存，不靠管道**（第五十七轮修的真机 bug）：
        Windows 上 `QLocalServer::listen()` 对同名管道**允许重复**（实测同一进程
        listen 两次都返回 True，跨进程也一样），所以「listen 成功 = 我是唯一」
        这个前提根本不成立 —— 真机就这么并排开了两个窗口。

        `QSharedMemory.create()` 是内核级的原子操作，同名段第二次创建必然失败；
        进程退出句柄关闭，段跟着销毁（Windows 上是纯内存映射，不会像 Unix 那样
        留下临时文件），所以它才配当那把锁。管道退回去只干本职工作：
        让第二个实例能把第一个的窗口叫出来。
        """
        # ① 抢锁决定生死
        if not self._acquire_lock():
            self._poke_existing()        # 顺手把已经在跑的那个叫到前面
            return False
        # ② 锁到手了，再开管道收敲门。开不出来也别放弃 —— 窗口能开就行，
        #    顶多是第二个实例点了图标之后没人应答。
        if _HAS_NET:
            try:
                QLocalServer.removeServer(self.name)   # 强杀留下的僵尸管道
            except Exception:                          # noqa: BLE001
                pass
            self._listen()
        return True

    def _listen(self) -> bool:
        """bind 管道名，用来收「又开了一个，把窗口叫出来」"""
        srv = QLocalServer(self)
        try:
            srv.setSocketOptions(
                getattr(QLocalServer, "UserAccessOption", 0))
        except Exception:                            # noqa: BLE001
            pass
        if not srv.listen(self.name):
            srv.deleteLater()
            return False
        srv.newConnection.connect(self._on_new)
        self._server = srv
        return True

    def _poke_existing(self) -> bool:
        """连一下现有实例：连得上说明真的有人在跑，顺手让它把窗口弹出来"""
        if not _HAS_NET:
            return False
        sock = QLocalSocket()
        try:
            sock.connectToServer(self.name)
            if not sock.waitForConnected(CONNECT_MS):
                return False
            sock.write(b"show")
            sock.flush()
            sock.waitForBytesWritten(CONNECT_MS)
            # 只断开、**不 abort()**：abort 会丢掉还没被对端读走的字节，
            # 同一进程内自测时就是「连上了但对方收不到」。deleteLater 让它自然关闭。
            sock.disconnectFromServer()
            return True
        except Exception:                                # noqa: BLE001
            try:
                sock.abort()
            except Exception:                            # noqa: BLE001
                pass
            return False
        finally:
            try:
                sock.deleteLater()
            except Exception:                            # noqa: BLE001
                pass

    def _acquire_lock(self) -> bool:
        """同名共享内存段 = 那把锁：attach 得到说明已经有人在跑。

        这是整个单实例**唯一**可靠的判定（管道同名可重复，见 `try_acquire`）。
        `create()` 失败但不是「已存在」（权限 / 系统限制）时放行 ——
        守卫坏了可以多开一个窗口，不能把用户挡在软件外面。
        """
        try:
            mem = QSharedMemory(self.name, self)
            if mem.attach():
                mem.detach()             # 只减引用计数，不会销毁别人那段
                return False
            if mem.create(8):
                self._mem = mem
                return True
            try:
                already = mem.error() == QSharedMemory.AlreadyExists
            except Exception:            # noqa: BLE001
                already = True
            return not already
        except Exception:                # noqa: BLE001
            return True

    # ------------------------------------------------------------ 收
    def _on_new(self):
        """有人连上来 = 又开了一个实例，直接叫窗口。

        不依赖 payload（谁连上来都算），这样即使那句 `show` 因为管道时序丢了，
        窗口照样会被叫出来 —— 别把「叫窗口」这件事压在几个字节上。
        """
        while self._server is not None and self._server.hasPendingConnections():
            c = self._server.nextPendingConnection()
            if c is None:
                break
            c.readyRead.connect(lambda c=c: self._drain(c))
            c.disconnected.connect(c.deleteLater)
            self._drain(c)
            self.activated.emit()

    def _drain(self, conn):
        try:
            self._buf += bytes(conn.readAll())
        except RuntimeError:
            return

    def release(self):
        if self._server is not None:
            try:
                self._server.close()
            except Exception:                            # noqa: BLE001
                pass
            self._server = None
        if self._mem is not None:
            try:
                self._mem.detach()
            except Exception:                            # noqa: BLE001
                pass
            self._mem = None


def flash_taskbar(hwnd: int):
    """闪几下任务栏。

    为什么要闪：Windows 有「前台窗口锁」，被别的进程叫起来的窗口常常拿到焦点
    却不显示在最前（`raise_()` / `activateWindow()` 都可能被系统忽略）。闪任务栏
    是唯一一定生效的「看这里」，玩家点一下就上来了。
    """
    if os.name != "nt" or not hwnd:
        return
    try:
        import ctypes
        from ctypes import wintypes

        class _FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT),
                        ("hwnd", wintypes.HWND),
                        ("dwFlags", wintypes.DWORD),
                        ("uCount", wintypes.UINT),
                        ("dwTimeout", wintypes.DWORD)]

        FLASHW_ALL = 0x3
        FLASHW_TIMERNOFG = 0xC
        info = _FLASHWINFO(ctypes.sizeof(_FLASHWINFO), wintypes.HWND(hwnd),
                           FLASHW_ALL | FLASHW_TIMERNOFG, 4, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:                                    # noqa: BLE001
        pass
