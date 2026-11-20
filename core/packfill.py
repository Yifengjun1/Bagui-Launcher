# -*- coding: utf-8 -*-
"""整合包自动补全（第五十四轮）

安装整合包时，zip 里那份清单（`manifest.json` / Modrinth
`modrinth.index.json`）会**原样**留在实例目录里，另外把我们实际下到的东西
（相对路径 / 大小 / sha1 / 下载地址）追加进 `installed` 段。

之后任何时候都能照这份清单核对一遍：清单上写着、磁盘上却没有（或者大小
对不上、sha1 不对）的文件，按记录下来的地址重新拉回来 ——
端点限流、Modrinth CDN 偶发 502 造成的「装完少几个模组」，等进游戏才发现
就已经晚了。

开关：设置 → 常规 → 自动补全（默认开）。关掉只是不再自动跑，
`fill()`（设置 → 游戏目录 → 补全整合包文件）手动补全仍然可用。

几条硬规则：
  · 只补「清单上有、磁盘上没有」的，**绝不删**实例里任何已有文件；
  · 下载前先看清单里记的大小，下完按大小复核（清单记了 sha1 的再核 sha1）；
  · 认不出下载源（清单只有 sha1）的，拿 sha1 去 Modrinth 批量换直链；
    换不到就报「找不到来源」，不猜、不瞎装。
"""
import hashlib
import json
import os
import requests

from .config import CONFIG

ENABLED_KEY = "auto_complete"
MANIFEST = "manifest.json"
INDEX = "modrinth.index.json"
ZIPLIST = ".mcassist_zip.json"
BAGUI_FORMAT = "bagui-pack/1"
MODRINTH_API = "https://api.modrinth.com/v2"
UA = "bagui-launcher/1.2 (+https://modrinth.com)"


# ---------------------------------------------------------------- 开关
def enabled() -> bool:
    """自动补全是否开启（默认开）"""
    return bool(CONFIG.get(ENABLED_KEY, True))


def set_enabled(on: bool):
    CONFIG.set(ENABLED_KEY, bool(on))


# ---------------------------------------------------------------- 小工具
def sha1_of(path: str) -> str:
    """文件 sha1（读不到返回空串，调用方按「核不了」处理，别当失败）"""
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def _item(path="", size=0, sha1="", url="", cf=None) -> dict:
    p = _safe_rel(path)
    return {"path": p, "size": int(size or 0), "sha1": str(sha1 or "").lower(),
            "url": str(url or ""), "cf": tuple(cf) if cf else None}


def _safe_rel(path) -> str:
    """清单里的相对路径必须留在实例目录里。

    整合包是外来的：`"../../Windows/..."`、`"/abs/path"`、`"C:/x"` 这类
    一律当「核不了」（返回空串），绝不让下载往实例外面写文件。
    """
    p = str(path or "").replace("\\", "/").strip()
    if not p:
        return ""
    p = p.lstrip("/")
    if len(p) > 1 and p[1] == ":":          # 盘符
        return ""
    if any(seg in ("..", "") for seg in p.split("/")):
        return ""
    return p


# ---------------------------------------------------------------- 读清单
def zip_list(instance_dir: str) -> list:
    """安装时记下的「这个整合包里本该有哪些文件」（相对路径）。

    为什么需要它：manifest.json / modrinth.index.json 只列**要下载**的文件，
    `overrides/`（config、资源包、kubelet 脚本、默认按键…）**一条都不在里面** ——
    所以过去核对永远「不缺」，而玩家打开一看 config 是空的。
    第九十四轮起安装时把解压清单写进 `.mcassist_zip.json`，核对就能发现这类缺口。
    """
    d = _load_json(os.path.join(instance_dir or "", ZIPLIST))
    if not isinstance(d, dict):
        return []
    out = []
    for p in d.get("files") or []:
        q = _safe_rel(p)
        if q:
            out.append(q)
    return out


def read_index(instance_dir: str):
    """读实例里的清单，统一成 {fmt, path, name, mc, items:[…]}；没有就 None。

    fmt：modrinth / bagui —— 决定「缺地址时怎么补」。
    """
    d = os.path.abspath(instance_dir or "")
    if not d or not os.path.isdir(d):
        return None
    p = os.path.join(d, INDEX)
    data = _load_json(p) if os.path.isfile(p) else None
    if isinstance(data, dict):
        dep = data.get("dependencies") or {}
        loaders = [f"{k}-{v}" for k, v in dep.items() if k != "minecraft"]
        return {"fmt": "modrinth", "path": p, "name": str(data.get("name") or ""),
                "mc": str(dep.get("minecraft") or ""), "loaders": loaders,
                "items": _modrinth_items(data)}
    p = os.path.join(d, MANIFEST)
    data = _load_json(p) if os.path.isfile(p) else None
    if isinstance(data, dict):
        fmt = "bagui" if str(data.get("format") or "") == BAGUI_FORMAT else "manifest"
        mc = str((data.get("minecraft") or {}).get("version") or "")
        loaders = [str(l.get("id") or "") for l in
                   ((data.get("minecraft") or {}).get("modLoaders") or [])
                   if isinstance(l, dict)]
        return {"fmt": fmt, "path": p, "name": str(data.get("name") or ""),
                "mc": mc, "loaders": loaders, "items": _manifest_items(data)}
    return None


def _modrinth_items(data: dict) -> list:
    out = []
    for f in data.get("files") or []:
        if not isinstance(f, dict):
            continue
        env = f.get("env") or {}
        if str(env.get("client") or "") == "unsupported":
            continue                     # 服务端专用文件：客户端别下
        urls = f.get("downloads") or []
        hashes = f.get("hashes") or {}
        out.append(_item(f.get("path"), f.get("fileSize"),
                         hashes.get("sha1"), urls[0] if urls else ""))
    return out


def _manifest_items(data: dict) -> list:
    """manifest.json → 清单项（开源版只认自家导出的 bagui 格式）。

    优先用安装时记下的 `installed`（含真实相对路径、大小、sha1）；
    自家导出的清单再从 `files` / `mods` 补（只有文件名 + sha1）。
    """
    out = []
    inst = data.get("installed")
    if isinstance(inst, list) and inst:
        for r in inst:
            if not isinstance(r, dict) or not r.get("path"):
                continue
            out.append(_item(r.get("path"), r.get("size"), r.get("sha1"),
                             str(r.get("url") or "")))
        return out
    if str(data.get("format") or "") == BAGUI_FORMAT:
        for f in data.get("files") or []:
            if isinstance(f, dict) and f.get("path"):
                out.append(_item(f.get("path"), f.get("size"), f.get("sha1"),
                                 f.get("url")))
        if not out:
            for m in data.get("mods") or []:
                if isinstance(m, dict) and m.get("file"):
                    out.append(_item("mods/" + str(m["file"]),
                                     m.get("size"), m.get("sha1")))
    return out


# ---------------------------------------------------------------- 核对
def check(instance_dir: str, deep: bool = False) -> dict:
    """照清单核对实例目录。

    deep=True 时逐文件算 sha1（慢，但能抓到「大小对、内容错」的截断文件）。
    返回 {ok, fmt, name, items, missing, unresolved, present, unknown, bytes}

    `unresolved` = 清单里没写在目录里叫什么的那几项
    （安装时没下到的）。它们不便宜（要多打 API 换文件名），所以这里只挑出来，
    真正判「有没有」由 fill() 干。
    """
    idx = read_index(instance_dir)
    # 整合包里解压出来的那些文件（清单里没有的）也要核：它们不在 manifest 里，
    # 光看清单永远「不缺」，config / 资源包丢了也发现不了。
    zl = zip_list(instance_dir)
    zip_missing = []
    for p in zl:
        if not os.path.isfile(os.path.join(instance_dir, p.replace("/", os.sep))):
            zip_missing.append(p)
    if not idx:
        return {"ok": False, "reason": "实例目录里没有 manifest.json / modrinth.index.json",
                "fmt": "", "name": "", "items": [], "missing": [],
                "unresolved": [], "present": 0, "unknown": 0, "bytes": 0,
                "zip_total": len(zl), "zip_missing": zip_missing}
    missing, present, unknown = [], 0, 0
    unresolved = []
    for it in idx["items"]:
        if not it["path"]:
            if it.get("cf"):
                unresolved.append(it)     # 缺文件名：留给 fill() 处理
            else:
                unknown += 1
            continue
        full = os.path.join(instance_dir, it["path"].replace("/", os.sep))
        if not os.path.isfile(full):
            missing.append(it)
            continue
        try:
            size = os.path.getsize(full)
        except OSError:
            missing.append(it)
            continue
        if it["size"] and size != it["size"]:
            missing.append(it)
            continue
        if deep and it["sha1"] and sha1_of(full) != it["sha1"]:
            missing.append(it)
            continue
        present += 1
    return {"ok": True, "fmt": idx["fmt"], "name": idx["name"], "mc": idx["mc"],
            "items": idx["items"], "missing": missing, "present": present,
            "unresolved": unresolved, "unknown": unknown,
            "zip_total": len(zl), "zip_missing": zip_missing,
            "bytes": sum(int(it["size"] or 0) for it in missing)}


# ---------------------------------------------------------------- 补全
def _modrinth_by_sha1(hashes, cancel_flag=None) -> dict:
    """拿 sha1 批量换直链（Modrinth 的 version_files 接口一次能问一批）"""
    out = {}
    uniq = [h for h in dict.fromkeys(hashes) if h]
    for i in range(0, len(uniq), 100):
        if cancel_flag and cancel_flag[0]:
            break
        chunk = uniq[i:i + 100]
        try:
            r = requests.post(MODRINTH_API + "/version_files",
                              json={"hashes": chunk, "algorithm": "sha1"},
                              headers={"User-Agent": UA}, timeout=20)
            if r.status_code != 200:
                continue
            for h, v in (r.json() or {}).items():
                files = (v or {}).get("files") or []
                if not files:
                    continue
                f = files[0]
                out[str(h).lower()] = {"url": str(f.get("url") or ""),
                                       "size": int(f.get("size") or 0)}
        except Exception:
            continue
    return out


def fill(instance_dir: str, progress_cb=None, cancel_flag=None, bytes_cb=None,
         on_details=None, on_detail=None, deep: bool = False) -> dict:
    """按清单把缺的文件补回来（阻塞，放工作线程）。返回统计字典。

    progress_cb(done, total, text) —— 与安装流程同一套签名，界面直接复用。
    """
    from . import installer as _inst        # 延迟导入：installer 也 import 本模块

    def cb(d, t, text=""):
        if progress_cb:
            try:
                progress_cb(int(d), int(t), text)
            except Exception:
                pass

    res = check(instance_dir, deep=deep)
    out = {"ok": bool(res.get("ok")), "fmt": res.get("fmt", ""),
           "name": res.get("name", ""), "checked": len(res.get("items") or []),
           "missing": len(res.get("missing") or []), "filled": 0,
           "unknown": res.get("unknown", 0), "failed": [],
           "still_missing": [], "reason": res.get("reason", ""),
           "zip_missing": list(res.get("zip_missing") or [])}
    todo = list(res.get("missing") or [])
    lost = []                       # 换不出文件名的项（开源版无此类清单项）
    if not todo:
        out["failed"] = lost
        return out

    # 1) 清单里没写地址的（我们自己导出的包只留了 sha1）→ 去 Modrinth 换直链
    need = [it for it in todo if not it["url"] and it["sha1"]]
    if need:
        cb(2, 100, f"查询 {len(need)} 个模组的下载地址…")
        found = _modrinth_by_sha1([it["sha1"] for it in need], cancel_flag)
        for it in need:
            hit = found.get(it["sha1"])
            if hit and hit["url"]:
                it["url"] = hit["url"]
                if not it["size"]:
                    it["size"] = hit["size"]

    jobs, no_src = [], []
    for i, it in enumerate(todo):
        if not it["url"]:
            no_src.append(it["path"] or "（找不到来源）")
            continue
        # 路径为空说明清单写了越界路径或没写路径 —— 一律不下。
        if not it["path"]:
            no_src.append("（清单里的路径不可用，跳过）")
            continue
        jobs.append({"index": i, "row": i, "url": it["url"],
                     "rel": it["path"] or None, "dir": instance_dir,
                     "size": int(it["size"] or 0)})

    if on_details:
        try:
            on_details([{"name": (it["path"] or "未命名文件"),
                         "size": int(it["size"] or 0), "status": "wait"} for it in todo])
        except Exception:
            pass

    failed = []
    if jobs:
        cb(5, 100, f"补全 {len(jobs)} 个文件…")
        failed = _inst._download_deps(
            jobs, cb, cancel_flag,
            (lambda i, st, note="": (on_detail and on_detail(i, st, note))),
            total=len(jobs), shown=len(jobs), bytes_cb=bytes_cb) or []
    out["failed"] = [str(f) for f in failed] + no_src + lost

    # 2) 复核：逐个 stat，补回来几个就报几个（不靠 count 猜）。
    left = []
    for it in todo:
        p = str(it.get("path") or "")
        full = os.path.join(instance_dir, p.replace("/", os.sep)) if p else ""
        if full and os.path.isfile(full) and os.path.getsize(full) > 0:
            continue
        left.append(os.path.basename(p) if p else "（未知）")
    out["filled"] = max(0, len(todo) - len(left))
    out["still_missing"] = left
    after = check(instance_dir, deep=False)
    out["zip_missing"] = list(after.get("zip_missing") or [])
    out["missing_after"] = (len(after.get("missing") or []) + len(left)
                            + len(out["zip_missing"]))
    return out


def repair_misplaced(instance_dir: str) -> int:
    """把「明明是资源包、却被塞进 mods/」的 zip 挪到 resourcepacks/。返回挪了几个。

    以前安装一律落 `mods/`，于是整合包自带的材质包躺在 mods/ 里：
    游戏既不把它当模组加载、也不会出现在「资源包」列表里 —— 玩家看到的就是
    「导入后文件不齐全」。只认 **.zip + 有 pack.mcmeta + 没有模组 meta** 的档案，
    .jar 一律不动（宁可漏修，绝不把真模组挪走）。
    """
    d = os.path.abspath(instance_dir or "")
    md = os.path.join(d, "mods")
    if not os.path.isdir(md):
        return 0
    import zipfile
    _MOD_META = {"fabric.mod.json", "meta-inf/mods.toml",
                 "meta-inf/neoforge.mods.toml", "mcmod.info"}
    moved = 0
    for fn in os.listdir(md):
        if not fn.lower().endswith(".zip"):
            continue
        src = os.path.join(md, fn)
        try:
            with zipfile.ZipFile(src) as zf:
                low = {n.lower() for n in zf.namelist()}
            if "pack.mcmeta" not in low or (_MOD_META & low):
                continue
            if not any(n.startswith("assets/") for n in low):
                continue                    # 数据包之类，别乱动
        except Exception:                   # noqa: BLE001
            continue
        dst_dir = os.path.join(d, "resourcepacks")
        dst = os.path.join(dst_dir, fn)
        try:
            os.makedirs(dst_dir, exist_ok=True)
            if os.path.exists(dst):
                continue                    # 正确目录里已经有了，留着不动
            os.replace(src, dst)
            moved += 1
        except OSError:
            continue
    return moved


def list_packs(game_dir: str | None = None) -> list:
    """列出本机能被补全的整合包（轻量，不核对、不下网络）。

    第一百零九轮：点「补全整合包文件」要先让玩家挑补哪几个，而不是闷头全跑一遍。
    返回 [{path, name, fmt, mc, loader, has_manifest, installed}]
    """
    from . import installer as _inst
    out, seen = [], set()

    def add(p, installed=False):
        p = str(p or "")
        if not p or p in seen or not os.path.isdir(p):
            return
        idx = read_index(p)
        if not idx:
            return
        seen.add(p)
        try:
            _m = _load_json(os.path.join(p, ".mcassist.json")) or {}
        except Exception:                               # noqa: BLE001
            _m = {}
        out.append({"path": p,
                    "name": os.path.basename(p.rstrip("\\/")) or p,
                    "fmt": str(idx.get("fmt") or ""),
                    "mc": str(idx.get("mc") or _m.get("mc") or ""),
                    "loader": str(_m.get("loader") or ""),
                    "loader_ver": str(_m.get("loader_ver") or ""),
                    "installed": bool(installed)})

    for rec in (CONFIG.get("installed") or {}).values():
        if isinstance(rec, dict) and str(rec.get("kind") or "") == "modpack":
            add(rec.get("path"), True)
    try:
        gd = game_dir or _inst.detect_game_dir()
        vroot = os.path.join(gd, "versions")
        if os.path.isdir(vroot):
            for n in sorted(os.listdir(vroot)):
                add(os.path.join(vroot, n))
    except Exception:                                   # noqa: BLE001
        pass
    out.sort(key=lambda x: (not x["installed"], x["name"].lower()))
    return out


RUNTIME_FIX_KEY = "pack_runtime_fix"


def _runtime_state(instance_dir: str, game_dir: str | None = None) -> tuple[bool, str]:
    """这台实例的运行版本要不要修。返回 (要不要修, 为什么)。

    第一百零九轮：以前这里无条件调 `repair_runtime()`，即使文件与运行版本都好好的
    —— 一个「补 mods」的入口越界去重建运行版本，正是那次崩溃的源头（删掉
    `versions/neoforge-21.1.248` 后点补全，父版本被写成一个瘦壳，开局缺 MC 本体类）。
    现在只在真的要看时才动：

      · 实例档案已经 `mcassist.merged`（自包含）→ 不修，父版本在不在都无所谓；
      · `.mcassist.json` 认得出加载器 → 用 `loader_ready` 判，装全了就不修；
      · 认不出加载器 / 纯原版 → 不修（补全只负责文件，不负责猜运行版本）。
    """
    d = os.path.abspath(str(instance_dir or ""))
    if not d or not os.path.isdir(d):
        return False, "实例目录不见了"
    inst = os.path.basename(d.rstrip("\\/"))
    try:
        from . import installer as _inst
        from . import runtime as _rt
    except Exception:                                   # noqa: BLE001
        return False, ""
    vroot = _inst.versions_dir(game_dir or _inst.detect_game_dir())
    shell = _inst._json_file(os.path.join(vroot, inst, inst + ".json")) or {}
    if (shell.get("mcassist") or {}).get("merged") and not shell.get("inheritsFrom"):
        return False, ""
    meta = {}
    try:
        with open(os.path.join(d, ".mcassist.json"), "r", encoding="utf-8") as f:
            meta = json.load(f) or {}
    except Exception:                                   # noqa: BLE001
        meta = {}
    mc = str(meta.get("mc") or "")
    kind = str(meta.get("loader") or "").lower()
    lver = str(meta.get("loader_ver") or "")
    if not kind or kind not in ("forge", "neoforge", "fabric", "quilt"):
        return False, ""
    gd = game_dir or _inst.detect_game_dir()
    if kind in ("fabric", "quilt"):
        ok = bool(shell.get("inheritsFrom") or shell.get("mainClass"))
        return (not ok), "运行版本档案不完整"
    if not lver:
        return False, ""                # 版本号都没记下，别拿空版本拼安装器
    try:
        ok_l, why_l = _rt.loader_ready(gd, kind, lver, mc)
    except Exception:                                   # noqa: BLE001
        return False, ""
    if ok_l:
        return False, ""
    return True, why_l or "加载器没装全"


def fill_all(progress_cb=None, cancel_flag=None, deep: bool = False,
             game_dir: str | None = None, only=None) -> list:
    """把本启动器装过的整合包挨着核对一遍，返回人类可读说明列表。

    扫两处：`installed` 记录里的整合包，以及 `<游戏目录>/versions/*`（实例
    目录就是版本目录，别处启动器搬过来的包也能认）。

    `only`：只补这些实例目录（第一百零九轮「先选整合包再补全」的弹窗用）；
    不传就照旧扫全部。
    """
    from . import installer as _inst
    dirs, seen = [], set()

    def add(p):
        p = str(p or "")
        if p and os.path.isdir(p) and p not in seen and read_index(p):
            seen.add(p)
            dirs.append(p)

    if only:
        for p in only:
            add(p)
    else:
        for rec in (CONFIG.get("installed") or {}).values():
            if isinstance(rec, dict) and str(rec.get("kind") or "") == "modpack":
                add(rec.get("path"))
        try:
            gd = game_dir or _inst.detect_game_dir()
            vroot = os.path.join(gd, "versions")
            if os.path.isdir(vroot):
                for n in sorted(os.listdir(vroot)):
                    add(os.path.join(vroot, n))
        except Exception:
            pass

    msgs = []
    total = max(1, len(dirs))
    for i, d in enumerate(dirs):
        if cancel_flag and cancel_flag[0]:
            msgs.append("已取消")
            break
        name = os.path.basename(d.rstrip("\\/")) or d
        # 进度：一台实例占一整段（0~95），剩下的留给收尾
        base = int(95 * i / total)

        def _pc(done, _t, text="", _b=base, _n=total):
            if not progress_cb:
                return
            try:
                share = 95.0 / _n
                pct = _b + share * max(0.0, min(1.0, float(done or 0) / max(1, _t or 100)))
                progress_cb(int(pct), 95, f"「{name}」{text}" if text else f"「{name}」")
            except Exception:                           # noqa: BLE001
                pass

        if progress_cb:
            _pc(0, 100, "核对清单…")
        try:
            _mv = repair_misplaced(d)       # 老包：材质包被塞在 mods/ 里的，挪回去
        except Exception:                   # noqa: BLE001
            _mv = 0
        if _mv:
            msgs.append(f"「{name}」把 {_mv} 个放错位置的资源包挪回 resourcepacks/")
        r = fill(d, progress_cb=_pc, cancel_flag=cancel_flag, deep=deep)
        if not r.get("ok"):
            continue
        left = list(r.get("still_missing") or [])
        zleft = list(r.get("zip_missing") or [])
        if r["filled"]:
            msgs.append(f"「{name}」补回 {r['filled']} 个文件"
                        + (f"，还缺 {len(left)} 个" if left else ""))
        elif left:
            head = "、".join(left[:3])
            more = f" 等 {len(left)} 个" if len(left) > 3 else ""
            msgs.append(f"「{name}」还缺 {len(left)} 个文件：{head}{more}")
        elif zleft:
            head = "、".join(zleft[:3])
            more = f" 等 {len(zleft)} 个" if len(zleft) > 3 else ""
            msgs.append(f"「{name}」有 {len(zleft)} 个文件没解压出来：{head}{more}"
                        f" —— 重新导入一次整合包即可")
        elif r["unknown"]:
            msgs.append(f"「{name}」有 {r['unknown']} 项只有编号、无法核对（跳过）")
        # 运行版本：**只在真有问题时**修（第一百零九轮）。
        # 第九十六轮这里是无条件调 repair_runtime —— 一个「补 mods」的入口
        # 越界重建运行版本，父版本被写成瘦壳，开局 NoClassDefFoundError。
        try:
            from . import installer as _inst2
            _need, _why = _runtime_state(d, game_dir)
            if _need:
                if progress_cb:
                    _pc(0, 100, f"运行版本要修：{_why}…")
                _ok, _note = _inst2.repair_runtime(d, game_dir=game_dir)
                if _ok:
                    msgs.append(f"「{name}」运行版本修好了：{_note}")
                else:
                    msgs.append(f"「{name}」仍缺运行版本：{_note}")
        except Exception as _e:                     # noqa: BLE001
            msgs.append(f"「{name}」补齐运行版本失败：{str(_e)[:70]}")
        # 齐全就什么也不说：界面靠「msgs 为空」提示「整合包都是完整的」
    if progress_cb:
        try:
            progress_cb(98, 100, "收尾…")
        except Exception:                           # noqa: BLE001
            pass
    return msgs


# ---------------------------------------------------------------- 清单落地
def write_index(instance_dir: str, raw, filename: str = MANIFEST,
                records=None) -> str:
    """把整合包自带清单落到实例目录，返回落盘路径（失败返回空串）。

    `raw` 是 zip 里那份 JSON 的原始字节/文本 —— 内容一字节不改，
    只多一个 `installed` 段（本次实际下到的文件：路径 / 大小 / sha1 / 地址）。
    没有这份记录，清单核对时连文件名
    都不知道，也就谈不上补全了。
    """
    data = raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            data = json.loads(bytes(raw).decode("utf-8-sig"))
        except Exception:
            return ""
    if not isinstance(data, dict):
        return ""
    recs = []
    for r in (records or []):
        if not isinstance(r, dict) or not r.get("path"):
            continue
        recs.append({k: r[k] for k in ("path", "size", "sha1", "url") if r.get(k)})
    if recs:
        data["installed"] = recs
    name = INDEX if str(filename) == INDEX else MANIFEST
    dest = os.path.join(instance_dir, name)
    tmp = dest + ".tmp"
    try:
        os.makedirs(instance_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, dest)
        return dest
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return ""
