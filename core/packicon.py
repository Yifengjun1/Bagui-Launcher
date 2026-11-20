# -*- coding: utf-8 -*-
"""整合包自带图标探测。

游戏管理里每个整合包优先显示**它自己的图标**，只有确实找不到时才回退到
本软件的加载器徽章。各家启动器放图标的位置都不一样，这里按「最常见的先试」排：

  1. 实例根目录 `icon.png` / `logo.png` / `pack.png`（HMCL、Modrinth App、
     以及自带 icon 的整合包 overrides 都落在这儿）
  2. `<实例>/.minecraft/icon.png`（Prism / MultiMC 把 .minecraft 当版本隔离目录时）
  3. `instance.cfg` 的 `iconKey` → `icons/<key>.png`（Prism / MultiMC 的图标机制）
  4. `profile.json` 的 `icon` 指向的本地文件（Modrinth App）
  5. 官方启动器档案里的 `icon`：只有 `data:image/...;base64,` 才是真图片，
     `Grass_Block` 这类是官启内置图标名，不能当图用

返回统一是 (kind, value, source)：kind 为 "file"（磁盘路径）或 "data"（data URI）。
核心层不碰任何 Qt 对象，只给字符串，加载图片交给 UI。
"""
import os
import re

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
ROOT_STEMS = ("icon", "logo", "pack", "modpack")
DATA_PREFIX = "data:image/"


def _first_file(paths):
    for p in paths:
        try:
            if p and os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
        except OSError:
            continue
    return ""


def _root_icon(d):
    paths = []
    for stem in ROOT_STEMS:
        for ext in IMG_EXT:
            paths.append(os.path.join(d, stem + ext))
    return _first_file(paths)


def icon_key(d):
    """instance.cfg 里的 iconKey（Prism / MultiMC）。"""
    cfg = os.path.join(d, "instance.cfg")
    try:
        with open(cfg, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return ""
    m = re.search(r"^\s*iconKey\s*=\s*(.+?)\s*$", txt, re.M)
    if not m:
        return ""
    key = m.group(1).strip().strip('"')
    if key.lower() in ("", "default"):
        return ""
    return key


def _key_icon(d, key):
    if not key:
        return ""
    cands = []
    if key.lower().endswith(IMG_EXT):
        cands.append(os.path.join(d, key))
        cands.append(os.path.join(d, ".minecraft", key))
        cands.append(os.path.join(d, "minecraft", key))
    for base in (d, os.path.join(d, ".minecraft"), os.path.join(d, "minecraft"),
                 os.path.dirname(os.path.abspath(d))):
        for ext in ("",) + IMG_EXT:
            name = key if ext == "" else key + ext
            cands.append(os.path.join(base, "icons", name))
    return _first_file(cands)


def _profile_local_icon(d):
    """Modrinth App 的 profile.json：icon 可能是本地相对路径，也可能是远程 URL。"""
    p = os.path.join(d, "profile.json")
    if not os.path.isfile(p):
        return ""
    try:
        import json
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception:
        return ""
    icon = str((data or {}).get("icon") or "").strip()
    if not icon or "://" in icon or icon.startswith("data:"):
        return ""
    return _first_file([os.path.join(d, icon),
                        os.path.join(d, ".minecraft", icon)])


def is_data_icon(v):
    return str(v or "").strip().lower().startswith(DATA_PREFIX)


def find(instance_dir, profile_icon="") -> tuple[str, str, str]:
    """返回 (kind, value, source)；找不到返回 ("", "", "")。

    profile_icon 是官方启动器档案里的 icon 字段原值。
    """
    d = str(instance_dir or "")
    if d and os.path.isdir(d):
        p = _root_icon(d)
        if p:
            return "file", p, "实例目录 " + os.path.basename(p)
        # Prism / MultiMC 把游戏目录放 `.minecraft`，少数启动器写成 `minecraft`
        for sub in (".minecraft", "minecraft"):
            p = _root_icon(os.path.join(d, sub))
            if p:
                return "file", p, "实例 %s 里的 %s" % (sub, os.path.basename(p))
        p = _key_icon(d, icon_key(d))
        if p:
            return "file", p, "instance.cfg 指定的 " + os.path.basename(p)
        p = _profile_local_icon(d)
        if p:
            return "file", p, "profile.json 里的 " + os.path.basename(p)
    if is_data_icon(profile_icon):
        return "data", str(profile_icon).strip(), "官方启动器档案里的自定义图标"
    return "", "", ""


def apply_to(item: dict, instance_dir, profile_icon="") -> dict:
    """就地给行数据补 icon / icon_kind / icon_src 三个字段。"""
    kind, value, src = find(instance_dir, profile_icon)
    item["icon"] = value
    item["icon_kind"] = kind
    item["icon_src"] = src
    return item
