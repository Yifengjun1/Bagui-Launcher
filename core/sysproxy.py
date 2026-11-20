# -*- coding: utf-8 -*-
"""Windows 系统代理 → 给 Java 用的启动参数（第八十轮）。

为什么需要这个文件
------------------
玩家的皮肤突然变回默认的史蒂夫 / 爱丽克丝，日志里是这一行：

    Couldn't look up profile properties for <uuid>
    ... Read timed out
        at YggdrasilMinecraftSessionService.fetchProfileUncached

也就是游戏去 `sessionserver.mojang.com` 查皮肤时**超时**了，游戏不提示、直接
回退默认皮肤。而启动器自己下载却是通的 —— 因为启动器读注册表拿到了 Clash /
v2ray 的系统代理（`bbsmc._system_proxy`），**Java 不会**：它既不读注册表，
也不认 `-Dhttp.proxyHost` 之外的东西。

所以启动游戏时得自己把系统代理翻译成 JVM 参数：
`-Djava.net.useSystemProxies=true`（让 JDK 也认系统设置 / PAC）
+ 显式 `-Dhttp(s).proxyHost/Port`（不依赖 JDK 的判断，最稳）。

`tcp_ok()` 是必须的：代理端口早就不在了、注册表却还留着（玩家关了 Clash），
这时把 JVM 指过去等于让游戏**连直连都做不了** —— 比不设更糟。
"""
import os
import socket
import urllib.parse

from . import crashlog

# 本机 / 局域网永远不该走代理：不然「对局域网开放」别人连不进来，
# 自己连 127.0.0.1 的单人服务端也会绕一圈。
NON_PROXY = "localhost|127.*|[::1]|0.0.0.0"


def _env_proxy() -> dict:
    """环境变量代理（第 118 轮）：注册表关着但环境变量有代理时用它。

    Clash / v2ray 这类工具很多只写注册表，也有只写环境变量的（或在终端里
    `set HTTPS_PROXY=...`）。第 80 轮只认注册表，本机 `ProxyEnable=0` 但
    `HTTPS_PROXY=http://127.0.0.1:49201` 明明是通的 —— Java 既不读注册表
    也不读环境变量，结果游戏查皮肤直连 sessionserver 超时、角色变离线皮肤。
    `bbsmc._proxy_candidates()` 早已两条都认，这里补齐同一逻辑。

    刻意**不认** `CODEBUDDY_SERVICE_PROXY_URL`：那是 WorkBuddy 自己的内部
    hook 代理，跟玩家的网络环境无关，绝不能塞进游戏 JVM。
    """
    def pick(*names):
        for n in names:
            v = str(os.environ.get(n) or "").strip()
            if v:
                return v
        return ""

    http = pick("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
    https = pick("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
    if not http and not https:
        return {}
    http = http or https
    https = https or http
    if "://" not in http:
        http = "http://" + http
    if "://" not in https:
        https = "http://" + https
    return {"http": http, "https": https}


def _normalize_proxy(px):
    """`{"http": addr, "https": addr}` 补齐 scheme 并校验有 host。"""
    out = {}
    for k in ("http", "https"):
        v = str(px.get(k) or "").strip()
        if not v:
            continue
        if "://" not in v:
            v = "http://" + v
        out[k] = v
    return out


def system_proxy() -> dict:
    """Windows 系统代理（注册表优先，回退环境变量；都没有就返回 {}）。

    `ProxyServer` 有两种写法，都要认：
      · `127.0.0.1:7890`                            —— 全部协议同一个
      · `http=127.0.0.1:7890;https=127.0.0.1:7891`  —— 分协议

    第 118 轮：注册表关着（`ProxyEnable=0`）但环境变量有代理时，用环境变量。
    """
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        finally:
            winreg.CloseKey(key)
        if int(enable or 0):
            raw = str(server or "").strip()
            if raw:
                if "=" in raw:
                    parts = {}
                    for seg in raw.split(";"):
                        if "=" in seg:
                            k, _, v = seg.partition("=")
                            parts[k.strip().lower()] = v.strip()
                    http = parts.get("http") or parts.get("https") or ""
                    https = parts.get("https") or http
                else:
                    http = https = raw
                if http:
                    return _normalize_proxy({"http": http, "https": https})
    except Exception:                                  # noqa: BLE001
        pass
    # 注册表没开 / 没地址：回退环境变量（Clash 等只写环境变量的情况）
    return _env_proxy()


def host_port(url: str) -> tuple:
    """`http://127.0.0.1:7890` → (`127.0.0.1`, 7890)；解析不出来就是 ("", 0)"""
    try:
        u = urllib.parse.urlsplit(str(url or "").strip())
        if u.scheme not in ("http", "https"):
            u = urllib.parse.urlsplit("http://" + str(url or "").strip())
        host = u.hostname or ""
        if not host:
            return "", 0
        port = int(u.port or (443 if u.scheme == "https" else 80))
        return host, port
    except Exception:                                  # noqa: BLE001
        return "", 0


def tcp_ok(host: str, port: int, timeout: float = 0.45) -> bool:
    """代理端口能不能连上（0.45 秒就够，本地端口要么立刻通要么立刻拒）"""
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:                                  # noqa: BLE001
        return False


def jvm_args() -> list:
    """给 JVM 的代理参数；没有可用代理就返回空列表（**绝不猜**）。

    分协议写法的两个 host 可能不一样（http 7890 / https 7891），所以分开解。
    """
    px = system_proxy()
    if not px:
        return []
    out, seen = [], set()
    for scheme in ("http", "https"):
        host, port = host_port(px.get(scheme) or px.get("http") or "")
        if not host or not port:
            continue
        if not tcp_ok(host, port):
            crashlog.note(f"系统代理 {host}:{port} 连不上，启动游戏时不带代理参数")
            return []
        key = (scheme, host, port)
        if key in seen:
            continue
        seen.add(key)
        out += [f"-D{scheme}.proxyHost={host}", f"-D{scheme}.proxyPort={port}"]
    if not out:
        return []
    return (["-Djava.net.useSystemProxies=true", f"-Dhttp.nonProxyHosts={NON_PROXY}"]
            + out)
