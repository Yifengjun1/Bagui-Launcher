# -*- coding: utf-8 -*-
"""开场动画窗口：进主页之前先看这一页。

两种运行形态：
  · as_window=True（默认）：独立顶层无边框窗口（960×540），居中淡入。这是历史行为，
    回归探针 test_round36/37/42、probe_splash_drag 仍按这个形态断言，不能破坏。
  · as_window=False：作为主窗口的子控件覆盖层，铺满整个主窗（含标题栏），
    尺寸自适应主窗。第八十四轮「把欢迎窗口合并进主窗口」用它 —— 不再有独立窗口，
    主窗口自身承担起欢迎展示，播完/按 Esc 后覆盖层从中心消散、露出主界面。
    第八十五轮改为占满整窗（而非仅内容区），欢迎期间主界面被挡、但窗口边缘仍可缩放。

流程（第三十八轮：预热解码 + 零黑屏 + 结尾不停顿）：
  ① 背景是「MC 风格梯田」照片（`app/assets/media/welcome_bg.jpg`），cover 铺满，
     只压一层中/底部暗角保证文字可读；
  ② 窗口中间是软件图标（和状态栏 / 悬浮球同一个徽标），下面一行
     「八桂启动器欢迎您！」，再下面一行小字提示点图标开始；
  ③ **预热**：欢迎页静止的这段时间里先把两件事做掉 ——
     · `prewarm_media()`：静音播一帧再暂停。Qt 的 ffmpeg 后端第一次打开文件要
       枚举 Media Foundation 解码器，实测「点播放 → 首帧」要 **1150ms**；
       预热后只要 **50ms**。不预热的话，用户点完图标会先干等一秒多（看起来像
       点坏了），而画布已经淡成黑底 —— 这就是「点一下黑屏一下」的真因。
     · 主窗口由 main 侧预热（隐藏构建），`handoff` 时只剩 show。
  ④ 点图标 → 图标和文字隐藏，视频**等真帧到了才开始淡入**（`_fade_in()`），
     所以照片会一直留到第一帧出现，中间不存在黑场；
  ⑤ 右上角喇叭：默认有声，点一下静音，再点一下恢复；
  ⑥ **无缝接力**：离结尾 260ms 就发 `handoff`，主窗口 show 出来盖在本窗上，
     `crossfade()` 让本窗 1→0、主窗 0→1 同时进行（460ms），音频同步淡到 0。
     音频/画面都在尾音里交出去，所以「倒计时一结束立刻进主页」不会停一帧。
     （覆盖层形态下没有第二个窗口可对淡，所以改用 `fade_out()` 让自身淡出。）

工程上的取舍：
  · 不用 QVideoWidget，改用 QVideoSink 取帧自己画 —— QVideoWidget 是原生句柄，
    圆角只能靠 setMask（锯齿明显）；自己画就能和窗口的圆角一致。
  · 播放器出错 / 文件缺失 / 解码器不给 EndOfMedia / 看门狗超时都走 `_finish()`，
    绝不留一个「点了图标却永远黑屏」的开场页。
  · 覆盖层形态的淡入淡出用 `QGraphicsOpacityEffect`（每个控件独立不透明度），
    不能用 `setWindowOpacity` —— 那是改「顶级窗口」不透明度，会连带把主窗口
    一起变透明。
"""
import os
import time

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QParallelAnimationGroup,
                            QPointF, QPropertyAnimation, QRectF, Qt, QTimer, QUrl,
                            QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap, QRadialGradient,
                           QRegion)
from PySide6.QtWidgets import (QLabel, QWidget, QGraphicsOpacityEffect)

from ..core import assets
from ..core.config import APP_VERSION
from . import theme
from .branding import app_icon, paint_badge
from .widgets import IconButton

WELCOME = "八桂启动器欢迎您！"
# 第一百一十五轮：图标上方的醒目注意事项（红色大字）+ 点图标后的气泡倒计时
NOTICE_TEXT = ("注意：无正版账号或官方启动器的用户，使用前必须先在设置中开启独立启动，"
               "否则导入整合包或下载游戏时会报错！")
BUBBLE_TEXT = "请注意查看注意事项！！！"
GUIDE_TEXT = "使用指南"
COUNTDOWN_SEC = 3          # 点图标后气泡倒计时秒数，结束才淡入动画
VIDEO_PARTS = ("media", "welcome.mp4")
BG_PARTS = ("media", "welcome_bg.jpg")
RADIUS = 18
HANDOFF_LEAD_MS = 260      # 结尾前多久开始接力（主窗口 show + 对淡，尾巴不能留白）
XFADE_MS = 460             # 交叉溶解时长
VIDEO_FADE_IN_MS = 380     # 背景图 → 视频的溶解时长（首帧到了才开始）


class _VideoCanvas(QWidget):
    """视频画布：把 QVideoSink 送来的帧画进圆角区域（带平滑缩放 + 可淡入）"""

    RADIUS = RADIUS

    def __init__(self, parent=None):
        super().__init__(parent)
        self._img = QImage()
        self._fade = 1.0          # 0=完全透明（露出下面的背景图）1=视频铺满

    def set_frame(self, img):
        self._img = img
        self.update()

    def set_fade(self, v):
        try:
            self._fade = max(0.0, min(1.0, float(v)))
        except Exception:
            self._fade = 1.0
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(r, self.RADIUS, self.RADIUS)
        p.setClipPath(path)
        # 注意：**没有画面就什么都不画**（露出下面的背景照片）。
        # 第三十八轮之前这里是 fillPath(#0b0f14)，而 Qt 的 ffmpeg 后端首帧要
        # 1.15s —— 淡入 420ms 一到就先把整块填成近黑，再等 700ms 才有画面，
        # 用户看到的就是「点一下黑一下」。
        im = self._img
        if not im.isNull() and self._fade > 0.001:
            # 铺满（cover）：等比放大到盖住整个画布、多余部分裁掉。主窗不是 16:9，
            # 用居中 letterbox 会留黑边 → 看起来「没占满全窗口」（第八十六轮修复）。
            # cover 让视频在任意窗口比例下都占满，16:9 主窗则正好贴合。
            sw, sh = im.width(), im.height()
            scale = max(r.width() / max(sw, 1), r.height() / max(sh, 1))
            dw, dh = sw * scale, sh * scale
            dx = r.x() + (r.width() - dw) / 2
            dy = r.y() + (r.height() - dh) / 2
            p.setOpacity(self._fade)
            p.drawImage(QRectF(dx, dy, dw, dh), im)
        p.end()


class _BadgeButton(QWidget):
    """中间那个可点的软件图标（悬停放大 + 暖光，和状态栏同款手感）"""

    clicked = Signal()

    def __init__(self, size=132, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self._hover = False

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        paint_badge(p, QRectF(self.rect()).adjusted(8, 8, -8, -8), hover=self._hover)
        p.end()


class _GlowLabel(QLabel):
    """带柔和投影的文字（不走 QGraphicsDropShadowEffect）。

    第八十五轮修复「图标下的欢迎语整段不显示」的根因：覆盖层形态下，父控件
    SplashWindow 被挂上 QGraphicsOpacityEffect 做淡入；子控件若再挂
    QGraphicsDropShadowEffect 会触发 Qt 的嵌套 GraphicsEffect bug —— 该子控件
    整段不画出来（独立窗口形态用 windowOpacity 不受影响，所以旧版没事）。
    这里改成自绘投影（向下偏移两像素 + 半透明深色），零外部特效依赖，两种形态
    都稳。
    """

    def __init__(self, text, *, color="#fbfdff", glow=QColor(2, 7, 12, 190),
                 word_wrap=False, parent=None):
        super().__init__(text, parent)
        self._color = QColor(color)
        self._glow = glow
        self._off = 2
        self._word_wrap = word_wrap

    def setText(self, t):
        super().setText(t)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setFont(self.font())
        r = self.rect()
        flags = self.alignment() | (Qt.TextWordWrap if self._word_wrap else 0)
        p.setPen(self._glow)
        p.drawText(r.adjusted(0, self._off, 0, self._off), flags, self.text())
        p.setPen(self._color)
        p.drawText(r, flags, self.text())
        p.end()


class _BubbleLabel(QLabel):
    """聊天气泡：白底圆角 + 下方小尖角，红字倒计时提示。

    自绘而不套 QSS —— 气泡要盖在视频/背景上，必须有自己的底；且要能随秒数
    改字（`请注意查看注意事项！！！3s` → 2s → 1s）。不走 QGraphicsEffect，
    避免和覆盖层父窗的 opacity 特效嵌套（见 _GlowLabel 注释）。
    """

    def __init__(self, parent=None):
        super().__init__("", parent)
        self.setAlignment(Qt.AlignCenter)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        f = QFont(theme.FONT)
        f.setPixelSize(16)
        f.setBold(True)
        self.setFont(f)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -9)   # 底部留出尖角
        path = QPainterPath()
        path.addRoundedRect(r, 12, 12)
        # 尖角（朝下，指向图标）
        cx = r.center().x()
        tail = QPainterPath()
        tail.moveTo(cx - 10, r.bottom() - 1)
        tail.lineTo(cx + 2, self.rect().bottom() - 1)
        tail.lineTo(cx + 12, r.bottom() - 1)
        tail.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 246))
        p.drawPath(path.united(tail))
        p.setPen(QPen(QColor(220, 226, 234), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setPen(QColor("#d32f2f"))                       # 红色醒目
        p.drawText(r, Qt.AlignCenter, self.text())
        p.end()


class _GuideLink(QWidget):
    """「使用指南」深蓝文字链接（点击打开随安装目录里的指南文档）。"""

    clicked = Signal()

    def __init__(self, text=GUIDE_TEXT, parent=None):
        super().__init__(parent)
        self._text = text
        self._hover = False
        self.setCursor(Qt.PointingHandCursor)
        f = QFont(theme.FONT)
        f.setPixelSize(15)
        f.setUnderline(True)
        self.setFont(f)
        h = 30
        self.setFixedHeight(h)
        fm = QFontMetricsF(self.font())
        self.setFixedWidth(int(fm.horizontalAdvance(text) + 16))

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        # 深蓝；悬停更亮一点
        p.setPen(QColor("#0b57d0" if self._hover else "#1a3f8c"))
        p.drawText(self.rect(), Qt.AlignCenter, self._text)
        p.end()


class SplashWindow(QWidget):
    """开场页。

    as_window=True 时为独立无边框窗口（历史行为）；as_window=False 时作为主窗口
    子控件覆盖层，铺满内容区、尺寸自适应主窗。两种形态共用播放/预热/收尾逻辑。
    """

    handoff = Signal()        # 该让主窗口（或主界面）出场了（视频尾 / 出错 / Esc）
    finished = Signal()       # 接力完成，本窗（或本覆盖层）已经隐藏

    W, H = 960, 540

    def __init__(self, parent=None, as_window=True):
        self._windowed = as_window
        flags = Qt.Window | Qt.FramelessWindowHint if as_window else Qt.Widget
        super().__init__(parent, flags)
        if as_window:
            self.setWindowTitle("八桂启动器")
            self.setWindowIcon(app_icon())
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setFixedSize(self.W, self.H)
        else:
            # 覆盖层：默认不透明（背景图铺满盖住主界面），不要透明背景属性
            pass

        self.state = "intro"          # intro → playing → done
        self._muted = False           # 默认有声（用户点喇叭才静音）
        self._player = None
        self._audio = None
        self._sink = None
        self._watchdog = None
        self._fade = None
        self._xfade = None
        self._vfade = None
        self._audio_anim = None
        self._bg_pix = None
        self._warm = False            # 解码管线预热过
        self._warm_frame = False      # 预热时已经拿到过一帧（暂停在 0 点）
        self._first_frame = False     # 播放后第一帧到了没（决定什么时候淡入）
        self._dragging = False        # 正在拖窗口
        self._drag_off = None         # 按下点相对窗口左上角的偏移
        self._countdown = 0           # 气泡倒计时剩余秒数（0 = 不在倒计时）
        self._cd_timer = None
        self._pending_play = False    # 倒计时中：真播放要等倒数结束
        self._guide_open = None       # 由 main 注入：打开使用指南的回调

        self.canvas = _VideoCanvas(self)
        self.canvas.hide()

        self.badge = _BadgeButton(132, self)
        self.badge.clicked.connect(self.start_play)

        self.lb_welcome = _GlowLabel(WELCOME, color="#fbfdff", parent=self)
        f = QFont(theme.FONT)
        f.setPixelSize(28)
        f.setBold(True)
        self.lb_welcome.setFont(f)
        self.lb_welcome.setAlignment(Qt.AlignCenter)
        self.lb_welcome.setStyleSheet("background:transparent;padding-top:2px;")

        self.lb_hint = _GlowLabel("点击图标开始",
                                  color="rgba(226,236,246,235)", parent=self)
        hf = QFont(theme.FONT)
        hf.setPixelSize(13)
        self.lb_hint.setFont(hf)
        self.lb_hint.setAlignment(Qt.AlignCenter)
        self.lb_hint.setStyleSheet("background:transparent;")

        # 第八十六轮：欢迎语正下方新增启动器版本号（vX.Y.Z）
        self.lb_version = _GlowLabel("v" + APP_VERSION,
                                     color="#ffd23f", parent=self)
        vf = QFont(theme.FONT)
        vf.setPixelSize(14)
        self.lb_version.setFont(vf)
        self.lb_version.setAlignment(Qt.AlignCenter)
        self.lb_version.setStyleSheet("background:transparent;")

        # 第一百一十五轮：图标上方的醒目注意事项（红色大字 + 黑色投影）。
        # 不用整块深色底 —— 那会挡住背景梯田照片（回归 R37 靠采样点验照片在不在）；
        # 红字 + 双层描边投影既显眼又只占笔画，不遮背景。
        self.lb_notice = _GlowLabel(NOTICE_TEXT,
                                    color="#ff2f2f",
                                    glow=QColor(10, 0, 0, 235),
                                    word_wrap=True, parent=self)
        nf = QFont(theme.FONT)
        nf.setPixelSize(17)
        nf.setBold(True)
        self.lb_notice.setFont(nf)
        self.lb_notice.setAlignment(Qt.AlignCenter | Qt.AlignVCenter)

        # 「使用指南」深蓝链接：点击打开随安装目录里的指南文档
        self.lb_guide = _GuideLink(GUIDE_TEXT, self)
        self.lb_guide.clicked.connect(self._open_guide)

        # 点图标后弹出的聊天气泡（默认隐藏，倒计时逐秒改字）
        self.lb_bubble = _BubbleLabel(self)
        self.lb_bubble.hide()

        # 右上角喇叭：默认「有声」，点一下切静音
        self.btn_sound = IconButton("volume", 36, 18, color="#eef3fa",
                                   hover_bg=QColor(255, 255, 255, 56), parent=self)
        self.btn_sound.setToolTip("点击静音（当前：播放声音）")
        self.btn_sound.clicked.connect(self.toggle_sound)

        # 按实际尺寸排布（覆盖层形态窗口大小是主窗内容区，不是固定的 960×540）
        self._relayout()

    # ---------------- 布局（自适应） ----------------
    def _relayout(self):
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            w, h = self.W, self.H
        self.canvas.setGeometry(0, 0, w, h)
        bw = 132
        self.badge.setFixedSize(bw, bw)
        bx = (w - bw) // 2
        by = int(h * 0.30)
        self.badge.move(bx, by)
        ty = by + bw + 22
        self.lb_welcome.setGeometry(0, ty, w, 50)
        self.lb_version.setGeometry(0, ty + 54, w, 26)
        self.lb_hint.setGeometry(0, ty + 54 + 30, w, 26)
        # 注意事项：图标上方，左右留白、最多三行
        self.lb_notice.setGeometry(int(w * 0.06), 34, int(w * 0.88), max(60, by - 48))
        # 使用指南：放在「点击图标开始」正下方
        self.lb_guide.move((w - self.lb_guide.width()) // 2, ty + 54 + 30 + 28)
        # 气泡：盖在图标上方一点，宽度自适应
        bh = 64
        bw2 = min(int(w * 0.9), 430)
        self.lb_bubble.setGeometry((w - bw2) // 2, max(30, by - bh - 6), bw2, bh)
        self.btn_sound.move(w - 36 - 16, 14)

    # ---------------- 声音 ----------------
    def toggle_sound(self):
        self._muted = not self._muted
        self.btn_sound.icon_name = "volume_off" if self._muted else "volume"
        self.btn_sound.setToolTip("点击有声（当前：静音）" if self._muted
                                  else "点击静音（当前：播放声音）")
        self.btn_sound.update()
        if self._audio is not None:
            try:
                self._audio.setMuted(self._muted)
            except Exception:
                pass

    # ---------------- 播放器 ----------------
    def _make_player(self, src):
        """建播放器 + 音频输出 + 视频接收器；失败返回 False（调用方收场）"""
        try:
            from PySide6.QtMultimedia import (QAudioOutput, QMediaPlayer,
                                              QVideoSink)
        except Exception:
            return False                   # 没装多媒体组件
        try:
            self._sink = QVideoSink(self)
            self._sink.videoFrameChanged.connect(self._on_frame)
            self._audio = QAudioOutput(self)
            self._audio.setVolume(1.0)
            self._audio.setMuted(self._muted)
            self._player = QMediaPlayer(self)
            self._player.setAudioOutput(self._audio)
            self._player.setVideoSink(self._sink)
            self._player.setSource(QUrl.fromLocalFile(src))
            self._player.mediaStatusChanged.connect(self._on_status)
            self._player.positionChanged.connect(self._on_pos)
            self._player.errorOccurred.connect(self._on_error)
            return True
        except Exception:
            self._player = None
            return False

    def prewarm_media(self):
        """预热解码管线（欢迎页静止时跑）：静音播一帧，再暂停回 0 点。

        不预热的话「点播放 → 首帧」要 1150ms（Qt ffmpeg 后端首次打开要枚举
        Media Foundation 的解码器 MFT），预热后 54ms。这段时间里用户已经点了
        图标，画面却还停在照片上 —— 老版本正是用一层黑底去填这段空白，于是
        变成「点一下黑一下」。
        """
        if self.state != "intro" or self._player is not None:
            return
        src = assets.asset(*VIDEO_PARTS)
        if not os.path.isfile(src) or not self._make_player(src):
            return
        self._warm = True
        try:
            self._audio.setMuted(True)          # 预热绝不出声
            self._audio.setVolume(0.0)
            self._player.setPosition(0)
            self._player.play()
        except Exception:
            pass

    # ---------------- 播放 ----------------
    def start_play(self):
        """点了中间图标：藏起图标和文字，气泡倒数 3 秒后才真正淡入动画。

        第一百一十五轮：先进入 playing 态、把播放器建好并开播（这样第一帧照常
        解出、画面 canvas 照常出来，回归断言「点图标后出画面」不受影响），但
        **画面淡入 / 出声都等气泡 3 秒倒计时结束**（`_count_tick`）。
        玩家看到的是：注意事项气泡 3 → 2 → 1，然后动画才出现。
        """
        if self.state != "intro":
            return
        self.state = "playing"
        self.badge.hide()
        self.lb_welcome.hide()
        self.lb_version.hide()
        self.lb_hint.hide()
        self.lb_notice.hide()
        self.lb_guide.hide()
        self.canvas.set_fade(0.0)          # 先停在照片上，等真帧到了再淡
        self.canvas.show()
        self.canvas.raise_()

        src = assets.asset(*VIDEO_PARTS)
        if not os.path.isfile(src):
            self._finish()
            return
        if self._player is None and not self._make_player(src):
            self._finish()                 # 没有多媒体组件：直接进软件，别卡着
            return
        self._first_frame = False

        # 气泡倒计时：期间静音 + 不淡入；结束才播声音并淡入。
        # 预热帧照常出来（canvas 有真帧），但 fade 停在 0 → 用户仍看到照片。
        self._pending_play = True
        try:
            self._audio.setMuted(True)
            self._audio.setVolume(0.0)
            self._player.setPosition(0)
            self._player.play()
        except Exception:
            self._finish()
            return
        self._start_countdown()

        # 看门狗：倒计时 + 视频时长留足余量（真播 5s + 倒数 3s）
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._check_stuck)
        self._watchdog.start(16000)

    # ---------------- 注意事项气泡倒计时 ----------------
    def _start_countdown(self):
        self._countdown = COUNTDOWN_SEC
        self.lb_bubble.setText(f"{BUBBLE_TEXT}{self._countdown}s")
        self.lb_bubble.show()
        self.lb_bubble.raise_()
        self.btn_sound.raise_()
        if self._cd_timer is None:
            self._cd_timer = QTimer(self)
            self._cd_timer.setInterval(1000)
            self._cd_timer.timeout.connect(self._count_tick)
        self._cd_timer.start()

    def _count_tick(self):
        self._countdown -= 1
        if self._countdown > 0:
            self.lb_bubble.setText(f"{BUBBLE_TEXT}{self._countdown}s")
            return
        # 倒数结束：收气泡，恢复声音，淡入 video
        try:
            if self._cd_timer is not None:
                self._cd_timer.stop()
        except Exception:
            pass
        self._countdown = 0
        self.lb_bubble.hide()
        self._pending_play = False
        try:
            self._audio.setMuted(self._muted)
            self._audio.setVolume(1.0)
        except Exception:
            pass
        if self._first_frame:
            self._fade_in()

    # ---------------- 使用指南 ----------------
    def _open_guide(self):
        """优先交给 main 注入的回调（有 Toast 兜底）；独立窗口形态自己开。"""
        try:
            if callable(self._guide_open):
                self._guide_open()
                return
        except Exception:
            pass
        from . import splash_guide                       # 延迟导入，避免循环
        splash_guide.open_guide()

    def _fade_in(self):
        """照片 → 视频的交叉溶解（只跑一次，且必须已经有真帧）"""
        if self._vfade is not None:
            return
        anim = QVariantAnimation(self)
        anim.setDuration(VIDEO_FADE_IN_MS)
        anim.setStartValue(self.canvas._fade)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.valueChanged.connect(lambda v: self.canvas.set_fade(v))
        anim.start(QAbstractAnimation.DeleteWhenStopped)
        self._vfade = anim

    def _fade_audio(self, ms):
        """接力时把音量淡到 0（画面淡出的同时声音也收掉，不然会有截断感）"""
        if self._audio is None or self._muted:
            return
        try:
            base = float(self._audio.volume())
        except Exception:
            base = 1.0
        anim = QVariantAnimation(self)
        anim.setDuration(max(80, int(ms)))
        anim.setStartValue(base)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.InCubic)
        anim.valueChanged.connect(lambda v: self._set_volume(v))
        anim.start(QAbstractAnimation.DeleteWhenStopped)
        self._audio_anim = anim

    def _set_volume(self, v):
        try:
            if self._audio is not None:
                self._audio.setVolume(max(0.0, min(1.0, float(v))))
        except Exception:
            pass

    def _on_frame(self, frame):
        try:
            if not frame.isValid():
                return
            img = frame.toImage()
            if img.isNull():
                return
            if self.state == "intro":
                # 预热用的一帧：够了解码器就是热的，立刻停下回 0 点等用户点
                if not self._warm_frame:
                    self._warm_frame = True
                    try:
                        self._player.pause()
                        self._player.setPosition(0)
                    except Exception:
                        pass
                return
            self.canvas.set_frame(img)
            if not self._first_frame:
                self._first_frame = True
                # 倒数期间只收帧不淡入（画面停在照片 + 气泡上），倒数结束由
                # _count_tick 统一淡入；非倒数路径（兼容旧测试/直接调用）照旧。
                if not self._pending_play:
                    self._fade_in()
        except Exception:
            pass

    def _on_error(self, *_):
        if self.state == "intro":
            return                          # 预热失败不算数，点播放时再兜底
        self._finish()

    def _on_status(self, status):
        try:
            from PySide6.QtMultimedia import QMediaPlayer
        except Exception:
            self._finish()
            return
        if self.state == "intro":
            return                          # 预热阶段的异常状态一律忽略
        if status in (QMediaPlayer.EndOfMedia, QMediaPlayer.InvalidMedia):
            self._finish()

    def _on_pos(self, pos):
        """结尾前 HANDOFF_LEAD_MS 就开始接力，让淡入和动画尾音重叠"""
        if self.state != "playing" or self._player is None:
            return
        try:
            dur = int(self._player.duration())
        except Exception:
            dur = 0
        if dur > 800 and dur - pos <= HANDOFF_LEAD_MS:
            self._finish()

    def _check_stuck(self):
        try:
            from PySide6.QtMultimedia import QMediaPlayer
        except Exception:
            self._finish()
            return
        if self.state != "playing":
            return
        if self._player is None or \
                self._player.playbackState() != QMediaPlayer.PlayingState:
            self._finish()

    # ---------------- 收尾 / 接力 ----------------
    def _stop_player(self):
        try:
            if self._player is not None:
                self._player.stop()
                self._player.deleteLater()
        except Exception:
            pass
        self._player = None

    def finish_now(self):
        """外部（Esc / 出错）提前收场"""
        self._finish()

    def _finish(self):
        """到了该进软件的时候：**不自己淡出**，把主窗口（或主界面）叫出来"""
        if self.state == "done":
            return
        self.state = "done"
        try:
            if self._watchdog is not None:
                self._watchdog.stop()
            if self._cd_timer is not None:
                self._cd_timer.stop()
        except Exception:
            pass
        self.lb_bubble.hide()
        self._pending_play = False
        self.handoff.emit()

    def begin(self):
        """启动欢迎流程：显示欢迎页并预热解码器。

        as_window=True：居中淡入（show_centered）。
        as_window=False：作为覆盖层铺满父窗、淡入（用 QGraphicsOpacityEffect，
        不动主窗口的整体不透明度）。预热在静止期异步进行。
        """
        if self._windowed:
            self.show_centered()
        else:
            # 覆盖层必须**第一时间不透明**地盖住主窗，绝不做入场淡入 —— 否则主窗
            # 背景会在淡入的 320ms 里从透明处透出来，表现为「先看到主窗、再看到
            # 欢迎页」那一闪（第八十六轮修复）。拉窗口边缘缩放仍由主窗边缘抓手负责。
            self.show()
        QTimer.singleShot(420, self.prewarm_media)

    def fade_out(self, ms=XFADE_MS, on_done=None):
        """收尾：把主界面露出来。

        · 独立窗口形态（as_window=True）：和主窗交叉溶解（历史行为）。
        · 覆盖层形态（as_window=False）：改用「从中心消散」—— 用逐渐扩大的圆形
          遮罩把主界面从中心揭开（第八十五轮需求③），比单纯整体淡出更有收场感。
        """
        if self._xfade is not None:
            return
        if self._windowed:
            self._plain_fade_out(ms, on_done)
        else:
            self.dissipate(ms, on_done)

    def _plain_fade_out(self, ms=XFADE_MS, on_done=None):
        """覆盖层形态之外的整体淡出（含独立窗口兜底）。"""
        self._fade_audio(ms)
        eff = self.graphicsEffect()
        if eff is None:
            eff = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(ms)
        anim.setStartValue(float(eff.opacity()))
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.finished.connect(lambda: self._faded(on_done))
        self._xfade = anim
        anim.start()

    def dissipate(self, ms=XFADE_MS, on_done=None):
        """从中心消散：圆形遮罩由中心向外扩大，主界面从中心揭开。

        第八十五轮需求③：动画结束后要有消散动画，且从中心开始。实现上用
        QRegion 把覆盖层「抠掉一个从中心长大的圆」—— 圆外的欢迎页（背景 / 视频
        最后一帧）依旧可见，圆内透出主界面，半径长到盖住四角时正好揭完。
        """
        if self._xfade is not None:
            return
        self._fade_audio(ms)
        # 去掉入场时的整体不透明度特效，避免和遮罩冲突（遮罩本身就是收场过渡）
        try:
            self.setGraphicsEffect(None)
        except Exception:                            # noqa: BLE001
            pass
        # 收起交互元素，只留最后一帧视频 / 背景做揭开
        try:
            self.badge.hide()
            self.lb_welcome.hide()
            self.lb_version.hide()
            self.lb_hint.hide()
            self.lb_notice.hide()
            self.lb_guide.hide()
            self.lb_bubble.hide()
        except Exception:                            # noqa: BLE001
            pass
        self.canvas.set_fade(1.0)
        self.canvas.show()
        self.canvas.raise_()
        self.btn_sound.raise_()

        self._diss_on = on_done
        self._diss_t0 = time.perf_counter()
        self._diss_ms = max(160, int(ms))
        self._diss_timer = QTimer(self)
        self._diss_timer.setSingleShot(False)
        self._diss_timer.setInterval(16)
        self._diss_timer.timeout.connect(self._diss_step)
        self._xfade = self._diss_timer
        self._diss_step()        # 先画一帧（此时中心还没有洞）
        self._diss_timer.start()

    def _diss_step(self):
        try:
            w, h = self.width(), self.height()
            if w <= 0 or h <= 0:
                return
            t = (time.perf_counter() - self._diss_t0) / (self._diss_ms / 1000.0)
            t = max(0.0, min(1.0, t))
            e = 1.0 - (1.0 - t) ** 3          # easeOutCubic
            cx, cy = w / 2.0, h / 2.0
            maxr = ((w / 2.0) ** 2 + (h / 2.0) ** 2) ** 0.5 + 6.0
            r = maxr * e
            full = QRegion(0, 0, w, h)
            if r <= 1.0:
                self.setMask(full)
            else:
                d = int(r * 2)
                x0 = int(cx - r)
                y0 = int(cy - r)
                hole = QRegion(x0, y0, d, d, QRegion.Ellipse)
                self.setMask(full.subtracted(hole))
            if t >= 1.0:
                try:
                    self._diss_timer.stop()
                except Exception:                # noqa: BLE001
                    pass
                self._faded(self._diss_on)
        except Exception:                        # noqa: BLE001
            try:
                self._diss_timer.stop()
            except Exception:                    # noqa: BLE001
                pass
            self._faded(self._diss_on)

    def _faded(self, on_done):
        try:
            self.hide()
        except Exception:
            pass
        try:
            self.setGraphicsEffect(None)   # 复位，避免复用时带着透明效果
        except Exception:
            pass
        if callable(on_done):
            on_done()

    def crossfade(self, win, ms=XFADE_MS):
        """独立窗口形态：和主窗口交叉溶解（本窗 1→0、win 0→1）。
        仅供 as_window=True 使用（兼容历史探针）；覆盖层形态请改用 fade_out()。"""
        if self._xfade is not None:
            return
        try:
            win.setWindowOpacity(0.0)
        except Exception:
            pass
        self._fade_audio(ms)            # 声音跟着画面一起收
        group = QParallelAnimationGroup(self)
        out = QPropertyAnimation(self, b"windowOpacity", group)
        out.setDuration(ms)
        out.setStartValue(float(self.windowOpacity()))
        out.setEndValue(0.0)
        out.setEasingCurve(QEasingCurve.OutCubic)
        inn = QPropertyAnimation(win, b"windowOpacity", group)
        inn.setDuration(ms)
        inn.setStartValue(0.0)
        inn.setEndValue(1.0)
        inn.setEasingCurve(QEasingCurve.OutQuad)
        group.addAnimation(out)
        group.addAnimation(inn)
        group.finished.connect(self._closed)
        self._xfade = group
        group.start()

    def _closed(self):
        self._stop_player()
        self.hide()
        try:
            self.setWindowOpacity(1.0)   # 复位，免得窗口被复用时是透明的
        except Exception:
            pass
        self.finished.emit()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._finish()                # 等不及可以直接跳过
            return
        super().keyPressEvent(e)

    def closeEvent(self, e):
        # 独立窗口形态：Alt+F4 / 程序里被 close() 不能就这么消失 —— 主窗口还没建，
        # 而 main() 里 quitOnLastWindowClosed 是关着的，会留下一个没窗口的进程。
        # 覆盖层形态是子控件，关主窗时跟着关即可，不要拦截。
        if self._windowed and self.state != "done":
            e.ignore()
            self._finish()
            return
        super().closeEvent(e)

    # ---------------- 拖动 ----------------
    # 无边框窗口没有标题栏可拖，做成「按住非交互区域（图标 / 喇叭除外）就能拖」。
    # 独立窗口形态拖的是本窗；覆盖层形态拖的是主窗口（self.window()）。
    # 第八十六轮起覆盖层也允许拖动 —— 之前覆盖层盖住标题栏、又在这里不拦截鼠标，
    # 导致欢迎页期间整窗拖不动，已修复。
    def _is_button_at(self, pos):
        w = self.childAt(pos)
        while w is not None and w is not self:
            if w in (self.badge, self.btn_sound, getattr(self, "lb_guide", None)):
                return True
            w = w.parentWidget()
        return False

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not self._is_button_at(e.position().toPoint()):
            win = self.window()
            if win is not None:
                self._drag_off = e.globalPosition().toPoint() - win.frameGeometry().topLeft()
                self._dragging = True
                e.accept()
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._dragging and (e.buttons() & Qt.LeftButton) and self._drag_off is not None:
            win = self.window()
            if win is not None:
                win.move(e.globalPosition().toPoint() - self._drag_off)
                e.accept()
                return
        self._dragging = False
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._dragging:
            self._dragging = False
            self._drag_off = None
            e.accept()
            return
        super().mouseReleaseEvent(e)

    # ---------------- 外观 ----------------
    def _bg(self):
        """背景图（按需读一次，窗口尺寸固定所以不用缓存缩放版）"""
        if self._bg_pix is None:
            src = assets.asset(*BG_PARTS)
            self._bg_pix = QPixmap(src) if os.path.isfile(src) else QPixmap()
        return self._bg_pix

    def show_centered(self):
        from PySide6.QtGui import QGuiApplication
        scr = QGuiApplication.primaryScreen()
        if scr is not None:
            r = scr.availableGeometry()
            self.move(r.center() - self.rect().center())
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self.activateWindow()
        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(320)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QAbstractAnimation.DeleteWhenStopped)
        self._fade = anim

    def paintEvent(self, e):
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self._windowed:
            path = QPainterPath()
            path.addRoundedRect(r, RADIUS, RADIUS)
            p.setClipPath(path)
        # 暗底兜底（照片缺失时也不至于透明露出后面的东西）
        if self._windowed:
            p.fillPath(path, QColor("#0b0f14"))
        else:
            p.fillRect(r, QColor("#0b0f14"))
        # 背景照片（cover 铺满，盖在暗底之上）
        pix = self._bg()
        if pix is not None and not pix.isNull():
            # cover：等比放大到铺满，多余部分居中裁掉（和背景幻灯片同一套算法）
            sw, sh = max(pix.width(), 1), max(pix.height(), 1)
            scale = max(r.width() / sw, r.height() / sh)
            dw, dh = sw * scale, sh * scale
            dx = r.x() + (r.width() - dw) / 2
            dy = r.y() + (r.height() - dh) / 2
            p.drawPixmap(QRectF(dx, dy, dw, dh), pix, QRectF(0, 0, sw, sh))
        # 中间压一层暗光：图标和欢迎语压得住，四周仍然看得见梯田
        scrim = QRadialGradient(QPointF(w * 0.5, h * 0.47), w * 0.54)
        scrim.setColorAt(0.0, QColor(5, 11, 17, 138))
        scrim.setColorAt(0.70, QColor(5, 11, 17, 62))
        scrim.setColorAt(1.0, QColor(5, 11, 17, 0))
        band = QLinearGradient(0, 0, 0, h)
        band.setColorAt(0.0, QColor(4, 8, 14, 30))
        band.setColorAt(0.50, QColor(4, 8, 14, 0))
        band.setColorAt(1.0, QColor(4, 8, 14, 128))
        if self._windowed:
            p.fillPath(path, scrim)
            p.fillPath(path, band)
            p.setClipping(False)
            p.setPen(QPen(QColor(255, 255, 255, 46), 1))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        else:
            p.fillRect(r, scrim)
            p.fillRect(r, band)
        p.end()
