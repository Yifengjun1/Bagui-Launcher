# -*- coding: utf-8 -*-
"""微软账户网页登录（OAuth2 设备码流）+ Xbox Live XSTS + Minecraft 服务链
用户在浏览器中完成微软登录，本工具轮询拿令牌后换取 Minecraft 档案。
需要用户在设置中填写自己的 Azure 应用 Client ID（公共客户端，启用设备码流）。
"""
import requests

from .config import CONFIG

UA = "MC-GuanQi-Fuzhu-Gongju/1.0.0"
TIMEOUT = 20

# scope 与 Prism Launcher 设备码流保持一致（其用户群已大规模验证可用）
SCOPE = "XboxLive.SignIn XboxLive.offline_access"
DEVICE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
XBL_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"

# Xbox/XSTS 错误码 -> 提示
XSTS_ERRORS = {
    2148916233: "该微软账户没有 Xbox 账户（需先创建 Xbox 账户）",
    2148916238: "账户为儿童账户，无法登录（需家长同意）",
    2148916235: "所在地区不支持 Xbox 登录",
    2148916236: "账户需要年龄验证",
}


# 内置公共 Client ID：第三方注册应用（与 Prism Launcher 开源设备码流同款，CMakeLists 公开）。
# 注意不能用 Azure CLI 等微软第一方 ID —— 第一方应用未被 XboxLive 资源预授权，
# 浏览器登录页会报 "users are not permitted to consent to first party applications"。
# 在设置中填写自定义 ID 可覆盖。
BUILTIN_CLIENT_ID = "c36a9fb6-4f2a-41ff-90bd-ae7cc92031eb"


def client_id() -> str:
    custom = (CONFIG.get("msa_client_id") or "").strip()
    return custom or BUILTIN_CLIENT_ID


def has_client_id() -> bool:
    return True


def device_code_start() -> dict:
    """第一步：申请设备码。返回 {user_code, verification_uri, device_code, interval, expires_in}"""
    cid = client_id()
    if not cid:
        raise RuntimeError("未配置微软登录 Client ID（在“设置”中填写）")
    r = requests.post(DEVICE_URL, data={"client_id": cid, "scope": SCOPE},
                      headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    if "device_code" not in d:
        raise RuntimeError(f"申请设备码失败：{d.get('error_description', d)}")
    return d


def open_login_page(verification_uri: str):
    import webbrowser
    webbrowser.open(verification_uri)


def _poll_once(device_code: str) -> dict:
    """单次轮询。返回 {'pending':True} / {'slowdown':True} / token dict / 抛异常"""
    r = requests.post(TOKEN_URL, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": client_id(),
        "device_code": device_code,
    }, headers={"User-Agent": UA}, timeout=TIMEOUT)
    d = r.json()
    err = d.get("error")
    if err == "authorization_pending":
        return {"pending": True}
    if err == "slow_down":
        return {"slowdown": True}
    if err:
        raise RuntimeError(d.get("error_description") or err)
    return d


def _xbl_auth(access_token: str) -> tuple[str, str]:
    r = requests.post(XBL_URL, json={
        "Properties": {"AuthMethod": "RPS",
                       "SiteName": "user.auth.xboxlive.com",
                       "RpsTicket": "d=" + access_token},
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    }, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    return d["DisplayClaims"]["xui"][0]["uhs"], d["Token"]


def _xsts_auth(xbl_token: str) -> tuple[str, str, str]:
    """XSTS 授权。返回 (uhs, token, xuid)。

    第 118 轮：`xid` 就是玩家的 Xbox User ID（XUID）。以前只取 uhs/token，
    启动参数 `--xuid` 一直是空 —— Minecraft profile API 并不返回 xuid，
    只能从这里拿。部分正版服 / Realms 会校验它，空值会让会话看起来像离线。
    """
    r = requests.post(XSTS_URL, json={
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
        "RelyingParty": "rp://api.minecraftservices.com/",
        "TokenType": "JWT",
    }, headers={"User-Agent": UA}, timeout=TIMEOUT)
    if r.status_code == 401:
        try:
            code = r.json().get("XErr")
            raise RuntimeError(XSTS_ERRORS.get(code, f"XSTS 校验失败（{code}）"))
        except ValueError:
            raise RuntimeError("XSTS 校验失败")
    r.raise_for_status()
    d = r.json()
    claim = d["DisplayClaims"]["xui"][0]
    return claim["uhs"], d["Token"], str(claim.get("xid") or "")


def _mc_login(uhs: str, xsts: str) -> str:
    r = requests.post(MC_LOGIN_URL,
                      json={"identityToken": f"XBL3.0 x={uhs};{xsts}"},
                      headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    if "access_token" not in d:
        raise RuntimeError("Minecraft 服务登录失败（该账户可能未购买 Minecraft Java 版）")
    return d["access_token"]


def _mc_profile(access_token: str) -> dict:
    r = requests.get(MC_PROFILE_URL,
                     headers={"Authorization": "Bearer " + access_token,
                              "User-Agent": UA}, timeout=TIMEOUT)
    if r.status_code == 404:
        raise RuntimeError("该微软账户未拥有 Minecraft Java 版档案")
    r.raise_for_status()
    return r.json()


def _dash(hexid: str) -> str:
    h = hexid
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def login_full(device_code: str, interval: int, expires_in: int,
               progress=None, cancel_flag=None) -> dict:
    """完整登录链（阻塞，请在后台线程调用）。
    progress(stage_text)；返回会话 dict 并写入配置。"""
    import time
    deadline = time.time() + max(expires_in, 60)
    token = None
    while time.time() < deadline:
        if cancel_flag and cancel_flag[0]:
            raise RuntimeError("已取消")
        res = _poll_once(device_code)
        if res.get("pending"):
            if progress:
                progress("等待你在浏览器中完成登录…")
            time.sleep(max(interval, 3))
            continue
        if res.get("slowdown"):
            time.sleep(max(interval, 3) + 5)
            continue
        token = res
        break
    if not token:
        raise RuntimeError("登录超时，请重试")

    if progress:
        progress("通过 Xbox Live 验证…")
    uhs, xbl = _xbl_auth(token["access_token"])
    if progress:
        progress("XSTS 安全令牌…")
    uhs2, xsts, xuid = _xsts_auth(xbl)
    if progress:
        progress("登录 Minecraft 服务…")
    mc_token = _mc_login(uhs2, xsts)
    if progress:
        progress("获取档案…")
    prof = _mc_profile(mc_token)

    skin_url = None
    cape_url = None
    for sk in prof.get("skins", []):
        if sk.get("state") == "ACTIVE" or sk.get("alias") == "slim":
            skin_url = sk.get("url")
            if sk.get("state") == "ACTIVE":
                break
    for cp in prof.get("capes", []):
        if cp.get("state") == "ACTIVE":
            cape_url = cp.get("url")
            break

    session = {"name": prof.get("name"), "uuid": _dash(prof.get("id", "")),
               "skin_url": skin_url, "cape_url": cape_url,
               "mode": "microsoft",
               # 独立启动要用它进正版服务器；有效期约 24 小时，过期用 msa_refresh 续
               "mc_token": mc_token,
               # XUID 来自 XSTS DisplayClaims.xui[0].xid（profile API 不含它）
               "xuid": str(xuid or (prof.get("xuid") or "")),
               "client_id": client_id(),
               "msa_refresh": token.get("refresh_token", "")}
    return session


_last_refresh_extra = {}     # 第 118 轮：续期时顺带取回的 xuid / client_id


def refresh_mc_token(refresh_token: str, out: dict = None) -> tuple[str, str]:
    """用微软 refresh_token 换一份新的 Minecraft access_token。

    独立启动（不经过官启）时，用的是本地存的 mc_token；它 24 小时就过期，
    过期后靠这里续一次，别让玩家为了「登录过期」重新走一遍设备码。
    返回 (mc_token, 新的 refresh_token)；第 118 轮起 xuid / client_id 会
    写进模块级 ``_last_refresh_extra``（保持二元组签名，兼容旧测试桩）。
    """
    if not str(refresh_token or "").strip():
        raise RuntimeError("没有可用的微软登录凭据，请重新登录")
    r = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": client_id(),
        "refresh_token": refresh_token,
        "scope": SCOPE,
    }, headers={"User-Agent": UA}, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError("微软授权已过期（或已被撤销），请重新登录")
    d = r.json()
    tok = str(d.get("access_token") or "")
    if not tok:
        raise RuntimeError("续期失败：微软没有返回 access_token")
    uhs, xbl = _xbl_auth(tok)
    uhs2, xsts, xuid = _xsts_auth(xbl)
    global _last_refresh_extra
    _last_refresh_extra = {"xuid": str(xuid or ""), "client_id": client_id()}
    if out is not None:
        out.update(_last_refresh_extra)
    return _mc_login(uhs2, xsts), str(d.get("refresh_token") or refresh_token)

