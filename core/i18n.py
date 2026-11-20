# -*- coding: utf-8 -*-
"""启动器界面语言：简体 / 繁体（台式、港式、澳式）。

第一百一十六轮。项目此前**没有**界面国际化机制 —— 所有文案都是硬编码
中文。这里用 opencc 做「简 -> 繁」转换，配合界面层的运行时文本拦截，
实现全界面简繁切换，而不必逐行改 5249 处文案。

语言状态
--------
* ``lang_mode``: ``follow``（跟随系统语言）/ ``manual``（手动锁定）
* ``lang``: 手动选的具体语言 ``zh-cn`` / ``zh-hant-tw`` / ``zh-hant-hk`` / ``zh-hant-mo``

``follow`` 时用 QLocale 判定系统语言：zh_CN -> 简体，zh_TW -> 台式，
zh_HK -> 港式，zh_MO -> 澳式，其它（含英文系统）-> 简体。

转换引擎 opencc-python-reimplemented（纯 Python + 字典，能打进 exe）：
* 台式 s2twp —— 台湾正体，含用词转换（汇入/设定）
* 港式 s2hk —— 香港繁体
* 澳式 s2t  —— 澳门沿用通用繁体（opencc 无独立 s2mo）

简体是「原文」，不做转换；繁体转换对已是繁体的字幂等，不会越转越乱。
"""
import os
import sys

from . import config

# 配置键（存进 config.json）
MODE_KEY = "lang_mode"        # follow / manual
LANG_KEY = "lang"             # zh-cn / zh-hant-tw / zh-hant-hk / zh-hant-mo

SIMPLIFIED = "zh-cn"
TRAD_TW = "zh-hant-tw"
TRAD_HK = "zh-hant-hk"
TRAD_MO = "zh-hant-mo"

LANGS = (SIMPLIFIED, TRAD_TW, TRAD_HK, TRAD_MO)
TRADITIONAL = (TRAD_TW, TRAD_HK, TRAD_MO)

# 面向玩家的显示名（本身也会被 conv 转成繁体）
LANG_LABELS = {
    SIMPLIFIED: "简体",
    TRAD_TW: "台式",        # 第117轮：菜单已在「繁体▾」下，不再重复「繁体」二字
    TRAD_HK: "港式",
    TRAD_MO: "澳式",
}

# opencc 配置：简体原文 -> 各式繁体
_OPENCC_CFG = {
    TRAD_TW: "s2twp",
    TRAD_HK: "s2hk",
    TRAD_MO: "s2t",
}

# 全局信号：语言切换时界面层订阅它刷新所有已建控件。延迟导入避免 Qt
# 依赖污染纯 core 模块（有测试只 import core 不建 QApplication）。
_changed_signal = None


def _signal():
    """拿一个「语言已变(int)」信号，只建一次。"""
    global _changed_signal
    if _changed_signal is None:
        from PySide6.QtCore import Signal, QObject

        class _LangBus(QObject):
            changed = Signal(str)

        _changed_signal = _LangBus()
    return _changed_signal


def on_changed(callback):
    """订阅语言变化（callback 接收新的 lang 字符串）。"""
    try:
        _signal().changed.connect(callback)
    except Exception:
        pass


def _cfg():
    return config.CONFIG


def mode() -> str:
    v = str(_cfg().get(MODE_KEY, "follow") or "follow")
    return v if v in ("follow", "manual") else "follow"


def manual_lang() -> str:
    v = str(_cfg().get(LANG_KEY, "") or "")
    return v if v in LANGS else SIMPLIFIED


def system_lang() -> str:
    """按系统区域判定语言。异常一律回落简体，绝不因检测失败卡住启动。"""
    try:
        from PySide6.QtCore import QLocale
        loc = QLocale.system()
        # language() 在 zh 各分支都返回 Chinese，用 territory 区分简繁
        if loc.language() != QLocale.Chinese:
            return SIMPLIFIED
        ter = loc.territory()
        if ter == QLocale.Taiwan:
            return TRAD_TW
        if ter == QLocale.HongKong:
            return TRAD_HK
        if ter == QLocale.Macao:
            return TRAD_MO
        return SIMPLIFIED                      # China / Singapore / 其它 -> 简体
    except Exception:
        pass
    # 无 Qt 时的兜底：读环境变量 / 注册表风格字符串
    raw = (os.environ.get("LANG") or os.environ.get("LC_ALL") or "").lower()
    if "tw" in raw or "hant_tw" in raw:
        return TRAD_TW
    if "hk" in raw:
        return TRAD_HK
    if "mo" in raw:
        return TRAD_MO
    return SIMPLIFIED


# 第117轮：跟随开启时的「临时语言覆盖」——只在本次进程内生效。
# 用户点了「简体/繁体」想马上看到效果，但「跟随系统语言」开关保持开启、
# 配置不写死；下次启动仍按系统语言走。这就是「跟随时切换语言不影响」。
_override = None


def current_lang() -> str:
    """当前实际生效的语言。"""
    if mode() == "follow":
        return _override if _override in LANGS else system_lang()
    return manual_lang()


def is_traditional(lang: str = None) -> bool:
    return (lang or current_lang()) in TRADITIONAL


# ---------------------------------------------------------------- 转换引擎
_CC = {}                       # lang -> OpenCC 实例
_CACHE = {}                    # (lang, text) -> 转换结果（有上限，见下）
_CACHE_MAX = 4096


def _converter(lang: str):
    cc = _CC.get(lang)
    if cc is None:
        try:
            import opencc
            cc = opencc.OpenCC(_OPENCC_CFG.get(lang, "s2t"))
        except Exception as e:
            # 引擎缺失 = 不转换（降级为简体），但留痕，绝不静默崩
            try:
                from . import crashlog
                crashlog.note("i18n: opencc 不可用，语言回退简体 | %r" % (e,))
            except Exception:
                pass
            cc = _IDENTITY
        _CC[lang] = cc
    return cc


class _Identity:
    def convert(self, s):
        return s


_IDENTITY = _Identity()


def conv(text, lang: str = None) -> str:
    """把简体原文转成当前（或指定）语言。简体原文原样返回。"""
    if not isinstance(text, str) or not text:
        return text
    lang = lang or current_lang()
    if lang == SIMPLIFIED or not any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return text
    key = (lang, text)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    out = _converter(lang).convert(text)
    if len(_CACHE) < _CACHE_MAX:
        _CACHE[key] = out
    return out


def set_mode(new_mode: str, lang: str = None):
    """设置语言模式 / 手动语言，落盘并广播变化。"""
    global _override
    new_mode = new_mode if new_mode in ("follow", "manual") else "follow"
    _override = None                      # 任何模式切换都清掉临时覆盖
    cfg = _cfg()
    with cfg.batch():
        cfg.set(MODE_KEY, new_mode)
        if lang in LANGS:
            cfg.set(LANG_KEY, lang)
    eff = current_lang()
    try:
        _signal().changed.emit(eff)
    except Exception:
        pass
    return eff


def set_lang(lang: str):
    """手动切到具体语言（自动进入 manual 模式）。"""
    if lang not in LANGS:
        lang = SIMPLIFIED
    return set_mode("manual", lang)


def follow_system():
    """切回「跟随系统语言」。"""
    return set_mode("follow")


def temp_lang(lang: str):
    """跟随开启时的临时切换：只改本次会话显示，不动跟随配置。

    第117轮：跟随开关保持勾选、配置仍是 follow / 原手动语言不变，
    下次启动重新按系统语言走。「切换语言不影响」= 不影响跟随设置。
    """
    global _override
    if lang not in LANGS:
        lang = SIMPLIFIED
    _override = lang
    eff = current_lang()
    try:
        _signal().changed.emit(eff)
    except Exception:
        pass
    return eff


def has_override() -> bool:
    """跟随模式下是否正被临时覆盖（开关仍勾选，但显示不是系统语言）。"""
    return mode() == "follow" and _override in LANGS
