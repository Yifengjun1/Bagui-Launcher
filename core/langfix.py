# -*- coding: utf-8 -*-
"""自动中文（第五十五轮）：启动游戏前把 options.txt 的语言设成简体中文。

为什么要在启动前动文件：Minecraft 只在「设置 → 语言」里改语言，改完写回
`options.txt` 的 `lang:xx_xx`。整合包、别人拷来的实例、新装的版本，语言往往是
`en_us`（甚至 `zh_tw`），玩家每次装完都得自己进去点一遍 —— 这一层就是替玩家点。

只碰 `lang:` 这一行：
  · 文件不存在 → 建一个只含 `lang:zh_cn` 的（MC 自己会把其余项补全）；
  · 有 `lang:` → 就地替换成 `zh_cn`，其它键（键位、音量、难度…）一个字节都不动；
  · 没有 `lang:` → 末尾追加一行。
关掉开关（设置 → 常规 → 自动中文）就完全不写文件。
"""
import os

ENABLED_KEY = "auto_chinese"      # 配置键（默认 True，见 config.Config.data）
LANG_LINE = "lang:zh_cn"


def enabled() -> bool:
    from .config import CONFIG
    return bool(CONFIG.get(ENABLED_KEY, True))


def set_enabled(on: bool):
    from .config import CONFIG
    CONFIG.set(ENABLED_KEY, bool(on))


def _is_cn(value: str) -> bool:
    """已经是中文（简中任意写法）就别再写一遍，免得每次启动都动文件"""
    v = str(value or "").strip().lower().replace("-", "_")
    return v in ("zh_cn", "zh_hans_cn", "zh_hans", "zh_cn_")


def options_path(game_dir: str) -> str:
    return os.path.join(str(game_dir or ""), "options.txt")


def read_lang(game_dir: str) -> str:
    """当前 options.txt 里的语言值（没有就返回空串）"""
    p = options_path(game_dir)
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                if ln.strip().lower().startswith("lang:"):
                    return ln.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def apply(game_dir: str) -> bool:
    """把 game_dir 里的 options.txt 语言改成简体中文。返回「这次真的改了没有」。

    绝不抛异常：语言只是体验，不能因为一个只读目录把游戏拦下来。
    """
    gd = str(game_dir or "")
    if not gd or not os.path.isdir(gd):
        return False
    try:
        if not enabled():
            return False
    except Exception:
        pass
    p = options_path(gd)
    try:
        if not os.path.isfile(p):
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(LANG_LINE + "\n")
            return True
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        out, hit, changed = [], False, False
        for ln in lines:
            low = ln.strip().lower()
            if low.startswith("lang:"):
                # 同一个键出现多次只留一行（MC 自己不会这么写，但别人的包会）
                if hit:
                    changed = True
                    continue
                hit = True
                if _is_cn(ln.split(":", 1)[1].strip()):
                    out.append(ln)
                else:
                    out.append(LANG_LINE)
                    changed = True
            else:
                out.append(ln)
        if not hit:
            out.append(LANG_LINE)
            changed = True
        if not changed:
            return False
        tmp = p + ".bg.tmp"
        with open(tmp, "w", encoding="utf-8", errors="replace", newline="\n") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, p)          # 原子替换：中途断电也不会留下半个 options.txt
        return True
    except OSError:
        # 只读目录 / 文件被占用：删掉临时文件，安静放弃
        try:
            if os.path.isfile(p + ".bg.tmp"):
                os.remove(p + ".bg.tmp")
        except OSError:
            pass
        return False
