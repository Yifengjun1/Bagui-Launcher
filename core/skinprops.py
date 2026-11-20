"""微软正版皮肤：照官方启动器的方式，把皮肤数据直接喂给游戏。

原理（第 121 轮反编译确认）：
  * 官方启动器界面皮肤走 api.minecraftservices.com/minecraft/profile（国内通），
    而游戏进程渲染皮肤却走 sessionserver.mojang.com（国内常 403/429）。
  * MC 客户端 Main 接受 --profileProperties <json>（默认 {}），用 Gson 反序列化成
    authlib 的 PropertyMap，直接构造进游戏会话的 GameProfile —— 皮肤数据现成，
    游戏就不必再查 sessionserver。
  * 客户端 jar 里没有任何 isSignatureValid 的调用者：验签只在**服务端**做
    （玩家进正版服时由服务端验）。所以客户端渲染自己的皮肤只需 value 里的 texture
    URL，签名可以省略。Wiki 也明确 signature 是 Optional。

于是：用官方同款 api 域名拿 skin/cape 的 url，自己拼 textures JSON 并 base64，
做成 PropertyMap 格式传给游戏。sessionserver 那条被限流的路彻底不碰。

失败一律返回空串、不抛异常、不阻断启动 —— 退回游戏原行为（直连 sessionserver）。
离线 / 第三方登录 / 老版本一律不启用。
"""
import base64
import json
import re
import time

_CACHE = {}          # uuid -> (json_str 或 "", 时间戳)
_TTL = 600           # 10 分钟内不重复请求


def _ver_tuple(mc):
    out = []
    for part in str(mc or "").split("."):
        m = re.match(r"\d+", part)
        out.append(int(m.group(0)) if m else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def needs_inject(mc_version) -> bool:
    """只有 1.20.2+ 需要——更早的版本用老 authlib，皮肤机制不同，别动。"""
    return _ver_tuple(mc_version) >= (1, 20, 2)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def build_properties(profile: dict) -> str:
    """把 api.minecraftservices.com/minecraft/profile 的返回做成 PropertyMap JSON。

    形如：{"textures":[{"name":"textures","value":"<base64>"}]}
    value 解码后是 {"textures":{"SKIN":{"url":...},"CAPE":{"url":...}}}
    签名省略（客户端不验，见模块说明）。没有 skin 就返回空串。
    """
    try:
        skins = [s for s in (profile or {}).get("skins") or []
                 if str(s.get("state") or "").upper() == "ACTIVE" and s.get("url")]
        if not skins:
            return ""
        skin = skins[0]
        tex = {"SKIN": {"url": str(skin["url"])}}
        model = str(((skin.get("metadata") or {}).get("model") or "")).lower()
        if model == "slim":
            tex["SKIN"]["metadata"] = {"model": "slim"}
        for cape in (profile or {}).get("capes") or []:
            # 披风只有一个能生效；api 返回可能多条，取第一个有 url 的
            cu = cape.get("url") if isinstance(cape, dict) else None
            if cu:
                tex["CAPE"] = {"url": str(cu)}
                break
        value = _b64(json.dumps({"textures": tex}, separators=(",", ":")))
        props = {"textures": [{"name": "textures", "value": value}]}
        return json.dumps(props, separators=(",", ":"))
    except Exception:                                  # noqa: BLE001
        return ""


def fetch_properties(session: dict, force: bool = False) -> str:
    """拿 profileProperties JSON。失败返回 ""（不阻断启动）。

    session 需是微软正版且带 mc_token；否则直接返回 ""。
    结果按 uuid 缓存 10 分钟，和 1.9.0 的预取窗口对齐。
    """
    s = session or {}
    if str(s.get("mode") or "") != "microsoft":
        return ""
    token = str(s.get("mc_token") or "")
    uid = str(s.get("uuid") or "").replace("-", "")
    if not token or not uid:
        return ""
    now = time.time()
    if not force:
        hit = _CACHE.get(uid)
        if hit and now - hit[1] < _TTL:
            return hit[0]

    from . import msa, bbsmc
    profile = None
    for _label, px in bbsmc._proxy_candidates():
        try:
            import requests
            r = requests.get(msa.MC_PROFILE_URL,
                             headers={"Authorization": "Bearer " + token,
                                      "User-Agent": msa.UA},
                             proxies=px, timeout=(3.5, 6))
            if r.status_code == 200:
                profile = r.json()
                break
            if r.status_code in (401, 403):
                # token 失效/被拒：不重试其它代理，交回正常逻辑
                break
        except Exception:                              # noqa: BLE001
            continue
    result = build_properties(profile) if profile else ""
    _CACHE[uid] = (result, now)
    return result
