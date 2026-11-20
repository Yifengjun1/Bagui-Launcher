# -*- coding: utf-8 -*-
"""第三方登录：authlib-injector 外置登录（第六十八轮）

玩家在登录窗「选项二 · 第三方登录」里填三样东西：站点地址、邮箱（或账号）、密码。
站点地址就是面板服 / 皮肤站给的 **yggdrasil API 根**（例如）
``https://littleskin.cn/api/yggdrasil``。

协议走标准 yggdrasil：

    POST {api}/authserver/authenticate   -> accessToken + selectedProfile{id,name}
    GET  {api}/sessionserver/session/minecraft/profile/{uuid}  -> 皮肤签名

启动游戏时用 ``-javaagent:authlib-injector.jar={api}`` 注入，游戏才会拿这个站
验证登录态、拉这个站的皮肤。jar 只下一次，缓存在 ``<数据目录>/authlib/``。

这个模块**不 import Qt**：登录窗在工作线程里调 authenticate，主线程只收结果。
"""
import json
import os
import re

import requests

from .config import app_data_dir

UA = "MC-GuanQi-Fuzhu-Gongju/1.0.0"
TIMEOUT = 15
META_URL = "https://authlib-injector.yushi.moe/artifact/latest.json"
GH_URL = ("https://github.com/yushijinhun/authlib-injector/"
          "releases/latest/download/authlib-injector.jar")
JAR_GLOB = "authlib-injector"


class AuthError(RuntimeError):
    """登录/配置阶段能直接翻成中文给玩家看的错误。"""


# ---------------------------------------------------------------- 站点地址
# 玩家经常把整条接口地址贴进来（面板服文档里就写着 `/authserver/authenticate`）。
# 只剥「接口名及其后面的东西」，别再顺手把 `/api/yggdrasil` 也剥掉 ——
# 那一段正是 API 根（皮肤站的地址长这样），剥了就连不上了。
_TAIL_EP = re.compile(r"/(?:authserver|sessionserver)(?:/.*)?$", re.I)


def normalize_server(url: str) -> str:
    """把玩家填的各种写法收成一个 API 根。

    常见写法：``littleskin.cn`` / ``https://littleskin.cn`` /
    ``https://littleskin.cn/api/yggdrasil`` / 甚至把 ``/authserver/authenticate``
    整条 URL 贴进来。前两种要靠 ``resolve_server()`` 再探一次 well-known。
    """
    s = (url or "").strip().strip("'\"")
    if not s:
        raise AuthError("请填写站点地址（面板服或皮肤站给的 API 地址）")
    if not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    s = s.rstrip("/")
    s = _TAIL_EP.sub("", s).rstrip("/")
    return s


def _origin(url: str) -> str:
    m = re.match(r"^(https?://[^/]+)", url, re.I)
    return m.group(1) if m else url


def _meta_of(api: str, timeout=8):
    """探测某个 url 是不是 authlib-injector 的 API 根，是就返回它的 meta。"""
    r = requests.get(api, headers={"User-Agent": UA,
                                   "Accept": "application/json"},
                     timeout=timeout)
    if r.status_code != 200:
        return None
    try:
        j = r.json()
    except Exception:
        return None
    if not isinstance(j, dict):
        return None
    meta = j.get("meta") or {}
    # 皮肤站的 API 根一定带 meta（implementationName / serverName）
    if isinstance(meta, dict) and (meta.get("serverName") or
                                   meta.get("implementationName")):
        return {"api": api, "name": str(meta.get("serverName") or ""),
                "impl": str(meta.get("implementationName") or "")}
    return None


def resolve_server(url: str) -> dict:
    """找到真正的 API 根，返回 ``{"api", "name", "impl"}``。

    顺序：玩家填的那个 → 域名根的 ``/.well-known/authlib-injector-api-location.json``
    → 补 ``/api/yggdrasil``。都探不到就直接用原值（有些站根路径要鉴权、
    或者干脆不回 JSON，硬探反而把能用的站判死），让 authenticate 去说话。
    """
    base = normalize_server(url)
    try:
        got = _meta_of(base)
    except Exception:
        got = None
    if got:
        return got
    # 站点的 well-known 里写着 API 根（authlib-injector 的规范）
    try:
        r = requests.get(_origin(base) +
                         "/.well-known/authlib-injector-api-location.json",
                         headers={"User-Agent": UA}, timeout=8)
        if r.status_code == 200:
            loc = str((r.json() or {}).get("api_root") or "").rstrip("/")
            if loc:
                if not re.match(r"^https?://", loc, re.I):
                    loc = _origin(base) + "/" + loc.lstrip("/")
                got = _meta_of(loc)
                if got:
                    return got
                return {"api": loc, "name": "", "impl": ""}
    except Exception:
        pass
    # 皮肤站最常见的「只填了域名」
    if base.count("/") == 2:                     # 只有 https://host
        guess = base + "/api/yggdrasil"
        try:
            got = _meta_of(guess)
        except Exception:
            got = None
        if got:
            return got
    return {"api": base, "name": "", "impl": ""}


# ---------------------------------------------------------------- 登录
def _auth_error(r):
    """把 yggdrasil 的 error 翻成玩家能看懂的中文。"""
    code = ""
    msg = ""
    try:
        j = r.json()
        code = str(j.get("error") or "")
        msg = str(j.get("errorMessage") or "")
    except Exception:
        pass
    if r.status_code == 404:
        return AuthError("站点上没有登录接口（地址填错？要填 …/api/yggdrasil 这类根地址）")
    if code == "ForbiddenOperationException":
        if "Invalid credentials" in msg or "password" in msg.lower():
            return AuthError("账号或密码不对（也可能这个站要求用邮箱而不是用户名）")
        return AuthError(msg or "这个站拒绝了登录请求")
    if code == "IllegalArgumentException":
        return AuthError(msg or "站点不认这个请求（地址可能不是 yggdrasil API 根）")
    if r.status_code >= 500:
        return AuthError(f"站点服务器出错（HTTP {r.status_code}），稍后再试")
    return AuthError(msg or f"登录失败（HTTP {r.status_code}）")


def _dash(uid: str) -> str:
    h = str(uid or "").replace("-", "").lower()
    if len(h) != 32:
        return str(uid or "")
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def textures(api: str, uuid_hex: str) -> dict:
    """取皮肤/披风地址（拿不到就当没有，不影响登录）。"""
    out = {"skin_url": None, "cape_url": None}
    if not uuid_hex:
        return out
    try:
        import base64
        r = requests.get(
            f"{api}/sessionserver/session/minecraft/profile/{uuid_hex}",
            headers={"User-Agent": UA}, timeout=8)
        if r.status_code != 200:
            return out
        for prop in (r.json() or {}).get("properties", []) or []:
            if prop.get("name") != "textures":
                continue
            tex = json.loads(base64.b64decode(prop.get("value") or ""))
            t = (tex.get("textures") or {})
            out["skin_url"] = ((t.get("SKIN") or {}).get("url")) or None
            out["cape_url"] = ((t.get("CAPE") or {}).get("url")) or None
            break
    except Exception:
        pass
    return out


def authenticate(server: str, account: str, password: str) -> dict:
    """登录第三方账号，返回可以直接 save_session() 的会话字典。"""
    account = (account or "").strip()
    if not account:
        raise AuthError("请填写邮箱（或站点要求的账号）")
    if not password:
        raise AuthError("请填写密码")
    info = resolve_server(server)
    api = info["api"]
    body = {
        "agent": {"name": "Minecraft", "version": 1},
        "username": account,
        "password": password,
        "requestUser": False,
    }
    try:
        r = requests.post(f"{api}/authserver/authenticate", json=body,
                          headers={"User-Agent": UA,
                                   "Content-Type": "application/json"},
                          timeout=TIMEOUT)
    except requests.RequestException as e:
        raise AuthError(f"连不上站点（{e.__class__.__name__}）：{api}")
    if r.status_code != 200:
        raise _auth_error(r)
    try:
        j = r.json() or {}
    except Exception:
        raise AuthError("站点返回的不是 JSON（地址多半不是 yggdrasil API 根）")
    token = str(j.get("accessToken") or "")
    prof = j.get("selectedProfile") or {}
    profiles = j.get("availableProfiles") or []
    if not prof and len(profiles) == 1:
        prof = profiles[0]          # 只有一个角色时有些站不回 selectedProfile
    uid = str(prof.get("id") or "")
    name = str(prof.get("name") or "")
    if not token:
        raise AuthError("登录成功但站点没给 accessToken（这个站可能不支持外置登录）")
    if not uid or not name:
        raise AuthError("这个账号还没有游戏角色：先去皮肤站建一个角色再登录")
    tex = textures(api, uid.replace("-", ""))
    sess = {
        "name": name,
        "uuid": _dash(uid),
        "skin_url": tex["skin_url"],
        "cape_url": tex["cape_url"],
        "mode": "thirdparty",
        "server": api,
        "server_name": info.get("name") or "",
        "mc_token": token,
        "client_token": str(j.get("clientToken") or ""),
        "account": account,
    }
    return sess


# ---------------------------------------------------------------- jar
def jar_dir() -> str:
    d = os.path.join(app_data_dir(), "authlib")
    os.makedirs(d, exist_ok=True)
    return d


def jar_path() -> str:
    """返回已缓存的 jar 路径（没有就返回将来会用的那个路径，别拿它当「存在」）。"""
    d = jar_dir()
    best = ""
    best_mt = -1.0
    try:
        for n in os.listdir(d):
            if not (n.startswith(JAR_GLOB) and n.lower().endswith(".jar")):
                continue
            p = os.path.join(d, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_size <= 0:
                continue
            if st.st_mtime > best_mt:
                best, best_mt = p, st.st_mtime
    except OSError:
        pass
    return best or os.path.join(d, "authlib-injector.jar")


def _is_jar(p: str) -> bool:
    try:
        with open(p, "rb") as f:
            return f.read(2) == b"PK"
    except OSError:
        return False


def _sha256(p: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_jar(progress=None) -> str:
    """确保 authlib-injector.jar 在位，返回路径。阻塞，后台线程调用。"""
    def tell(t):
        if progress:
            try:
                progress(t)
            except Exception:
                pass

    cur = jar_path()
    if os.path.isfile(cur) and _is_jar(cur):
        return cur

    from . import dl
    meta = {}
    try:
        r = requests.get(META_URL, headers={"User-Agent": UA}, timeout=15)
        if r.status_code == 200:
            meta = r.json() or {}
    except Exception:
        meta = {}
    url = str(meta.get("download_url") or "") or GH_URL
    ver = str(meta.get("version") or "").strip()
    dest = os.path.join(jar_dir(),
                        f"authlib-injector-{ver}.jar" if ver
                        else "authlib-injector.jar")
    want = str(((meta.get("checksums") or {}).get("sha256")) or "").lower()
    if os.path.isfile(dest) and _is_jar(dest) and \
            (not want or _sha256(dest) == want):
        return dest

    tell("正在下载第三方登录组件…")
    tmp = dest + ".part"
    try:
        dl.download(url, tmp, expected_size=0)
    except Exception:
        # 官方 meta 挂了就换 GitHub 直链再试一次
        if url == GH_URL:
            raise
        dl.download(GH_URL, tmp, expected_size=0)
    if not _is_jar(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise AuthError("下载到的 authlib-injector.jar 不是有效文件（网络被拦截？）")
    if want and _sha256(tmp).lower() != want:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise AuthError("authlib-injector.jar 校验不通过（下载被篡改或损坏）")
    try:
        if os.path.exists(dest):
            os.remove(dest)
    except OSError:
        pass
    os.replace(tmp, dest)
    return dest
