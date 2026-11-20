# -*- coding: utf-8 -*-
"""登录会话：正版玩家名验证（Mojang API）+ 皮肤头像/3D 渲染获取"""
import base64
import hashlib
import json
import os

import requests

from .config import CONFIG, icon_cache_dir

UA = "MC-GuanQi-Fuzhu-Gongju/1.0.0"
TIMEOUT = 10


def verify_online(name: str) -> dict:
    """通过 Mojang API 验证正版玩家名，返回 {name, uuid, skin_url} 或抛异常"""
    r = requests.get(
        f"https://api.mojang.com/users/profiles/minecraft/{name}",
        headers={"User-Agent": UA}, timeout=TIMEOUT)
    if r.status_code == 204 or r.status_code == 404:
        raise RuntimeError("未找到该正版玩家名（检查拼写，或使用离线模式）")
    r.raise_for_status()
    data = r.json()
    uuid_dashed = _dash_uuid(data["id"])
    skin_url = None
    cape_url = None
    try:
        pr = requests.get(
            f"https://sessionserver.mojang.com/session/minecraft/profile/{uuid_dashed}",
            headers={"User-Agent": UA}, timeout=TIMEOUT)
        pr.raise_for_status()
        pj = pr.json()
        for prop in pj.get("properties", []):
            if prop.get("name") == "textures":
                tex = json.loads(base64.b64decode(prop["value"]))
                skin_url = tex.get("textures", {}).get("SKIN", {}).get("url")
                cape_url = tex.get("textures", {}).get("CAPE", {}).get("url")
                break
    except Exception:
        pass
    return {"name": data["name"], "uuid": uuid_dashed,
            "skin_url": skin_url, "cape_url": cape_url, "mode": "online"}


def _dash_uuid(hexid: str) -> str:
    h = hexid
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def offline_session(name: str) -> dict:
    """离线会话：用离线 UUID（3 型）+ 默认皮肤"""
    data = ("OfflinePlayer:" + name).encode("utf-8")
    h = hashlib.md5(data).hexdigest()
    b = bytearray.fromhex(h)
    b[6] = (b[6] & 0x0F) | 0x30
    b[8] = (b[8] & 0x3F) | 0x80
    u = bytes(b).hex()
    return {"name": name, "uuid": _dash_uuid(u), "skin_url": None,
            "cape_url": None, "mode": "offline"}


def head_url(name: str, size=64) -> str:
    return f"https://mc-heads.net/avatar/{name}/{size}"


def body_url(name: str, height=300) -> str:
    return f"https://mc-heads.net/body/{name}/{height}"


_IMG_MAGIC = (b"\x89PNG", b"\xff\xd8", b"GIF8", b"RIFF")


def _looks_like_image(data: bytes) -> bool:
    return any(data.startswith(m) for m in _IMG_MAGIC) or (
        data[:4] == b"RIFF" and data[8:12] == b"WEBP")


_img_session = requests.Session()
_img_session.headers.update({"User-Agent": UA})


def save_image(url: str, timeout=TIMEOUT) -> str | None:
    """下载图片到缓存目录（带图片魔数校验与坏缓存自愈），返回本地路径"""
    if not url:
        return None
    fname = hashlib.md5(url.encode()).hexdigest() + ".img"
    path = os.path.join(icon_cache_dir(), fname)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            with open(path, "rb") as f:
                if _looks_like_image(f.read(16)):
                    return path
        except OSError:
            pass
        try:                      # 缓存损坏 -> 自愈
            os.remove(path)
        except OSError:
            pass
    try:
        r = _img_session.get(url, timeout=timeout)
        r.raise_for_status()
        data = r.content
        if not data or not _looks_like_image(data):
            return None
        with open(path, "wb") as f:
            f.write(data)
        return path
    except Exception:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
        return None


def _crop_head(skin_path: str, out=128):
    """从官方皮肤图裁出头部正面（8,8,8,8 像素区）"""
    from PySide6.QtGui import QImage
    skin = QImage(skin_path)
    if skin.isNull():
        return None
    head = skin.copy(8, 8, 8, 8)
    if head.isNull():
        return None
    return head.scaled(out, out, Qt__keep_aspect(), Qt__fast())


def Qt__keep_aspect():
    from PySide6.QtCore import Qt
    return Qt.KeepAspectRatioByExpanding


def Qt__fast():
    from PySide6.QtCore import Qt
    return Qt.FastTransformation


def fetch_avatar(name: str, uuid: str | None = None,
                 skin_url: str | None = None, size=128):
    """多源头像：官方皮肤裁剪 -> mc-heads -> crafatar -> minotar。
    返回 QImage 或 None（线程中调用）"""
    from PySide6.QtGui import QImage
    # 1) 官方皮肤本地裁剪（要求1：头像来自玩家上传至官方的皮肤）
    if skin_url:
        p = save_image(skin_url)
        if p:
            img = _crop_head(p, size)
            if img:
                return img
    # 2) 依次尝试第三方渲染源
    sources = [
        f"https://mc-heads.net/avatar/{name}/{size}",
        f"https://minotar.net/helm/{name}/{size}",
    ]
    if uuid:
        sources.insert(1, f"https://crafatar.com/avatars/{uuid}?overlay&size={size}")
    for u in sources:
        p = save_image(u)
        if p:
            img = QImage(p)
            if not img.isNull():
                return img
            try:                      # 坏缓存自愈
                os.remove(p)
            except OSError:
                pass
    return None


def fetch_body(name: str, uuid: str | None = None, height=340):
    """多源 3D 全身渲染图。返回 QImage 或 None（线程中调用）
    注意：mc-heads /body 大尺寸(>=200)会 403，用 180；minotar 支持按名渲染。"""
    from PySide6.QtGui import QImage
    sources = [
        "https://mc-heads.net/body/{}/180".format(name),
        f"https://minotar.net/body/{name}/180",
    ]
    if uuid:
        sources.append(f"https://crafatar.com/renders/body/{uuid}?overlay&scale=6")
    for u in sources:
        p = save_image(u, timeout=20)
        if p:
            img = QImage(p)
            if not img.isNull():
                return img
            try:
                os.remove(p)
            except OSError:
                pass
    return None


def save_session(session: dict):
    CONFIG.set("session", session)


def logout():
    CONFIG.set("session", None)


def current_session():
    return CONFIG.session()
