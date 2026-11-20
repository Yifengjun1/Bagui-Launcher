# -*- coding: utf-8 -*-
"""把其它启动器里的游戏「接管」进官方启动器。

为什么要单独一个模块
--------------------
玩家换启动器最痛的不是重装，而是「我在 PCL2 里调好的这个包，官方启动器里
根本看不到」。官方启动器只认两样东西：

  · `<.minecraft>/versions/<id>/<id>.json`  —— 运行版本（含加载器）
  · `<.minecraft>/launcher_profiles.json`   —— 档案（含 gameDir / lastVersionId）

所以「导入」在这里被拆成两条路径，不能混为一谈：

  · 版本目录（versions/<id>）：直接「采用」进本机 versions，官启版本列表里
    就多一项。不发请求、不搬 mods。
  · 实例目录（带 mods/config/saves 的独立目录）：先保证有同款运行版本，
    再把内容**搬进** `<.minecraft>/versions/<包名>`（PCL2 的版本隔离语义），
    最后写版本 json + 档案。官启的版本列表与「安装」都能看到它。

  早期版本这里是「零拷贝，档案直接指向原目录」。实测下来那个方案是坏的：
  官启只认自己游戏目录里的东西，档案指到 Prism/PCL2 的目录时，那边一清理
  这套包就废了；而且官启重启后档案也常常被它自己吃掉。搬进来才算真的
  「配置进官方启动器」。（几十 G 的包确实要等一会儿，界面有字节进度。）

⚠ `lastVersionId` 绝不能留空（官启会 REQUEST_FAILED / Unable to prepare
assets for download），所以实例这条路径必须先拿到真实存在的版本 id，
拿不到就老实报错，绝不写空值。
"""
import json
import os

from . import installer, packicon, runtime
from .config import CONFIG

LOADER_CN = {"forge": "Forge", "neoforge": "NeoForge",
             "fabric": "Fabric", "quilt": "Quilt"}


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ---------------------------------------------------------------- 环境识别
def mmc_env(inst_dir):
    """Prism / MultiMC / HMCL 共用的 mmc-pack.json → (mc, kind, lver)

    组件 uid 是稳定契约，比猜文件名靠谱：
      net.minecraft / net.fabricmc.fabric-loader /
      net.minecraftforge / net.neoforged / org.quiltmc.quilt-loader
    """
    d = _read_json(os.path.join(inst_dir, "mmc-pack.json"))
    mc = kind = lver = ""
    for c in (d.get("components") or []):
        uid = str(c.get("uid") or "").lower()
        ver = str(c.get("version") or "")
        if uid == "net.minecraft":
            mc = ver
        elif "quilt-loader" in uid:
            kind, lver = "quilt", ver
        elif "fabric-loader" in uid:
            kind, lver = "fabric", ver
        elif "neoforge" in uid:
            kind, lver = "neoforge", ver
        elif "forge" in uid:
            # 顺序要紧：neoforge 的分支必须在前，否则会被当成 forge
            kind, lver = "forge", ver
    return mc, kind, lver


def modrinth_env(inst_dir):
    """Modrinth App 的 profile.json → (mc, kind, lver)"""
    d = _read_json(os.path.join(inst_dir, "profile.json"))
    mc = str(d.get("game_version") or "")
    loader = str(d.get("loader") or "").lower()
    lver = str(d.get("loader_version") or "")
    kind = loader.split("-")[0] if loader else ""
    if kind not in ("forge", "neoforge", "fabric", "quilt"):
        kind = ""
    return mc, kind, lver


_INSTANCE_MARKS = ("mods", "config", "saves", "options.txt", "instance.cfg",
                   "mmc-pack.json", "minecraftinstance.json", "profile.json")


def looks_instance(path):
    """这个目录像不像一个「游戏实例」（而不是 versions/<id> 这种纯版本目录）"""
    if not os.path.isdir(path):
        return False
    low = os.path.basename(path.rstrip("\\/")).lower()
    if low in ("versions", "libraries", "assets", "logs", "crash-reports",
               "screenshots", "resourcepacks", "shaderpacks", "backups"):
        return False
    for m in _INSTANCE_MARKS:
        if os.path.exists(os.path.join(path, m)):
            return True
    return False


def launcher_label(path):
    """按路径猜来源启动器 —— 只用于界面显示，认不出就退回目录名。

    顺序要紧：第三方启动器名必须排在 `.minecraft` 之前，否则
    `D:\\PCL2\\.minecraft` 会被认成「官方启动器」（路径里也含 .minecraft）。
    """
    low = path.replace("/", "\\").lower()
    table = (
        ("prismlauncher", "Prism Launcher"),
        ("multimc", "MultiMC"),
        ("modrinth", "Modrinth App"),
        ("atlauncher", "ATLauncher"),
        ("gdlauncher", "GDLauncher"),
        ("technic", "Technic"),
        ("hmcl", "HMCL"),
        ("pcl", "PCL2"),
    )
    for k, v in table:
        if k in low:
            return v
    if ".minecraft" in low or "minecraft" in low:
        return "官方启动器 / 通用 .minecraft"
    return "其它启动器"


def instance_env(inst_dir):
    """整合式推断：优先读各启动器的元数据，读不到再从 mods 文件名投票"""
    for fn in (mmc_env, modrinth_env):
        mc, kind, lver = fn(inst_dir)
        if mc:
            return mc, kind, lver
    mc, kind = runtime.infer_instance_env(inst_dir)
    lver = ""
    if kind:
        # 从 mods 里翻加载器版本（fabric-loader-0.15.11 / forge-1.20.1-47.2.0）
        mods = os.path.join(inst_dir, "mods")
        try:
            names = os.listdir(mods) if os.path.isdir(mods) else []
        except Exception:
            names = []
        for n in names:
            low = n.lower()
            if kind in low and "-" in low:
                tail = low.split(kind, 1)[1].lstrip("-_")
                tail = tail.split(".jar")[0]
                if kind in ("forge", "neoforge") and mc and tail.startswith(mc + "-"):
                    tail = tail[len(mc) + 1:]
                if tail and any(c.isdigit() for c in tail):
                    lver = tail
                    break
    return mc, kind, lver


# ---------------------------------------------------------------- 已导入判定
def _profile_game_dirs(game_dir):
    out = set()
    for p in installer.list_profiles(game_dir) or []:
        gd = p.get("game_dir") or ""
        if gd:
            out.add(os.path.normcase(os.path.realpath(gd)))
    # 官启会把自定义档案吃掉，光看档案会把「导入过的」显示成「没导入」，
    # 再点一次导入又重建一遍 —— 把本地登记表也算进来。
    for rec in installer.imported_records():
        out.add(os.path.normcase(os.path.realpath(rec["path"])))
    return out


def _version_ids(game_dir):
    vdir = os.path.join(game_dir, "versions")
    try:
        return {n.lower() for n in os.listdir(vdir)
                if os.path.isdir(os.path.join(vdir, n))}
    except Exception:
        return set()


# ---------------------------------------------------------------- 扫描
def scan(game_dir=None, extra_roots=None):
    """扫描本机可导入的游戏。返回 entry 列表（未导入的排在前面）。

    entry = {kind, path, name, mc, loader, loader_ver, source, imported, root,
             icon, icon_kind, icon_src}
    icon 三项由 packicon 探出来：整合包/版本自己带的图标，没有就留空让界面画加载器徽章。
    """
    gd = os.path.realpath(game_dir or installer.detect_game_dir())
    out, seen = [], set()
    have_ver = _version_ids(gd)
    have_dir = _profile_game_dirs(gd)
    done_src = absorbed_sources(gd)

    def add(kind, path, name, mc, loader, lver, source, root=""):
        try:
            rp = os.path.normcase(os.path.realpath(path))
        except Exception:
            return
        if rp in seen or rp == os.path.normcase(gd):
            return
        seen.add(rp)
        if kind == "version":
            imported = (os.path.basename(path.rstrip("\\/")).lower() in have_ver
                        or rp in done_src)
        else:
            # 实例导进来之后目录已经搬走，只能靠「来源目录」登记判断导没导过
            imported = rp in have_dir or rp in done_src
        out.append(packicon.apply_to(
            {"kind": kind, "path": path, "name": name, "mc": mc,
             "loader": loader, "loader_ver": lver, "source": source,
             "imported": imported, "root": root}, path))

    # ① 各 .minecraft 根：版本 + 实例
    roots = list(extra_roots or [])
    for r in runtime.candidate_roots():
        if r not in roots:
            roots.append(r)
    for root in roots:
        try:
            if os.path.normcase(os.path.realpath(root)) == os.path.normcase(gd):
                continue          # 自己的目录不用导
        except Exception:
            continue
        src = launcher_label(root)
        vdir = os.path.join(root, "versions")
        try:
            names = os.listdir(vdir) if os.path.isdir(vdir) else []
        except Exception:
            names = []
        for n in names:
            info = runtime.describe_version(os.path.join(vdir, n))
            if not info:
                continue
            add("version", info["dir"], n, info["mc"], info["loader"],
                info["loader_ver"], src, root)
        try:
            subs = [n for n in os.listdir(root)
                    if looks_instance(os.path.join(root, n))]
        except Exception:
            subs = []
        for n in subs:
            p = os.path.join(root, n)
            mc, kind, lver = instance_env(p)
            add("instance", p, n, mc, kind, lver, src, root)
        # 根目录自己就是个实例：PCL2 / 整合包常见「.minecraft 里直接放 mods」，
        # 命名取上一级目录（D:\界王…\.minecraft → 界王…），比叫 ".minecraft" 有用。
        # 认不出 MC 版本就不列：那种条目点了也导不进去，只是噪音。
        if looks_instance(root):
            mc, kind, lver = instance_env(root)
            if mc:
                add("instance", root,
                    os.path.basename(os.path.dirname(root.rstrip("\\/"))) or "默认实例",
                    mc, kind, lver, src, os.path.dirname(root.rstrip("\\/")))

    # ② 已知启动器的实例目录（Prism / Modrinth …）
    home = os.path.expanduser("~")
    appdata = os.path.expandvars("%APPDATA%")
    local = os.path.expandvars("%LOCALAPPDATA%")
    known = [
        (os.path.join(appdata, "PrismLauncher", "instances"), ""),
        (os.path.join(appdata, "MultiMC", "instances"), ""),
        (os.path.join(appdata, "com.modrinth.theseus", "profiles"), ""),
        (os.path.join(appdata, "ATLauncher", "instances"), ""),
        (os.path.join(local, "Programs", "gdlauncher_next", "instances"), ""),
        (os.path.join(appdata, ".technic", "modpacks"), ""),
    ]
    for base, _ in known:
        try:
            subs = [n for n in os.listdir(base)
                    if looks_instance(os.path.join(base, n))]
        except Exception:
            continue
        src = launcher_label(base)
        for n in subs:
            p = os.path.join(base, n)
            mc, kind, lver = instance_env(p)
            add("instance", p, n, mc, kind, lver, src, base)

    out.sort(key=lambda e: (e["imported"], e["kind"] != "instance",
                            (e["name"] or "").lower()))
    return out


def _is_version_dir(path):
    """<目录名>.json 就在里面 → 这是一个 versions/<id> 版本目录"""
    n = os.path.basename(str(path).rstrip("\\/"))
    return bool(n) and os.path.isfile(os.path.join(path, n + ".json"))


def scan_path(path, game_dir=None):
    """只处理玩家亲手指定的那一个文件夹 —— 对应 PCL2 的「添加已有文件夹」。

    自动扫描未必找得到（PCL2 装在 D 盘、包放在别处、用的是绿色版启动器…），
    所以允许直接指路。四种形态都认：
      · 实例目录：带 mods / config / saves / mmc-pack.json ……（最常用）
      · 版本目录：versions/<id>，里面有同名 json
      · 启动器根目录：下面有 versions/（.minecraft、PCL2 的根都算）
      · 一堆实例的上一级：子目录都像实例（Prism 的 instances 就是这么放的）
    返回 entry 列表，结构与 scan() 一致。
    """
    gd = os.path.realpath(game_dir or installer.detect_game_dir())
    path = os.path.realpath(str(path or "").strip())
    if not path or not os.path.isdir(path):
        return []
    have_ver = _version_ids(gd)
    have_dir = _profile_game_dirs(gd)
    done_src = absorbed_sources(gd)
    out, seen = [], set()

    def norm(p):
        try:
            return os.path.normcase(os.path.realpath(p))
        except Exception:
            return p

    def mk(kind, p, name, mc, loader, lver, source, root=""):
        rp = norm(p)
        if rp in seen or rp == norm(gd):
            return
        seen.add(rp)
        if kind == "version":
            imported = (os.path.basename(p.rstrip("\\/")).lower() in have_ver
                        or rp in done_src)
        else:
            imported = rp in have_dir or rp in done_src
        out.append(packicon.apply_to({
            "kind": kind, "path": p, "name": name or os.path.basename(p),
            "mc": mc, "loader": loader, "loader_ver": lver, "source": source,
            "imported": imported,
            "root": root or os.path.dirname(p.rstrip("\\/"))}, p))

    base = os.path.basename(path.rstrip("\\/"))
    # ① 指到了 versions 文件夹本身 → 当成它上一级是启动器根目录
    if base.lower() == "versions":
        path = os.path.dirname(path)
        base = os.path.basename(path.rstrip("\\/"))

    # ② 带 versions/ 的启动器根目录：里面的版本全列出来
    vdir = os.path.join(path, "versions")
    if os.path.isdir(vdir):
        src = launcher_label(path)
        try:
            names = sorted(os.listdir(vdir))
        except Exception:
            names = []
        for n in names:
            info = runtime.describe_version(os.path.join(vdir, n))
            if info:
                mk("version", info["dir"], n, info["mc"], info["loader"],
                   info["loader_ver"], src, path)
        if looks_instance(path):
            mc, kind, lver = instance_env(path)
            if mc:
                mk("instance", path,
                   os.path.basename(os.path.dirname(path.rstrip("\\/"))) or base,
                   mc, kind, lver, src, os.path.dirname(path.rstrip("\\/")))
        return _sorted(out)

    # ③ 单个版本目录
    if _is_version_dir(path) and not looks_instance(path):
        info = runtime.describe_version(path)
        if info:
            mk("version", info["dir"], base, info["mc"], info["loader"],
               info["loader_ver"], launcher_label(path),
               os.path.dirname(path.rstrip("\\/")))
            return _sorted(out)

    # ④ 单个实例目录（还在用别的启动器、以后不打算再用它 → 直接搬）
    if looks_instance(path):
        mc, kind, lver = instance_env(path)
        mk("instance", path, base, mc, kind, lver, launcher_label(path),
           os.path.dirname(path.rstrip("\\/")))
        return _sorted(out)

    # ⑤ 上级目录：子目录里挑实例 / 版本
    src = launcher_label(path)
    try:
        subs = sorted(os.listdir(path))
    except Exception:
        subs = []
    for n in subs:
        p = os.path.join(path, n)
        if not os.path.isdir(p):
            continue
        if looks_instance(p):
            mc, kind, lver = instance_env(p)
            mk("instance", p, n, mc, kind, lver, src, path)
        elif _is_version_dir(p):
            info = runtime.describe_version(p)
            if info:
                mk("version", info["dir"], n, info["mc"], info["loader"],
                   info["loader_ver"], src, path)
    return _sorted(out)


def _sorted(entries):
    return sorted(entries, key=lambda e: (e["imported"], e["kind"] != "instance",
                                          (e["name"] or "").lower()))


def _mark_imported(entry, vid, pid="", target="", extra_meta=None):
    """给导入的实例打上本软件的标记，并登记到本地表。

    两件事都很重要：
      · `.mcassist.json` 落在实例目录里 —— 「游戏管理」据此认出它是整合包，
        官启把档案吃掉（它自己的已知回归）也不影响本软件显示；
      · `installer.remember_import` 记一份到本地配置 —— 万一实例目录里
        的文件被清掉，或者档案被覆盖，还能照着重写。
    `target` 是搬好之后的实例目录（默认就是原目录），`imported_from` 记来源目录，
    这样同一个源导过一次之后，扫描里就不会再把它显示成「没导入」。
    返回写进标记里的内容（失败返回空 dict）。
    """
    src = str(entry.get("path") or "")
    path = str(target or src)
    if not path or not os.path.isdir(path):
        return {}
    meta = {
        "pack": str(entry.get("name") or os.path.basename(path)),
        "mc": str(entry.get("mc") or ""),
        "loader": str(entry.get("loader") or ""),
        "loader_ver": str(entry.get("loader_ver") or ""),
        "version_id": str(vid or ""),
        "profile_id": str(pid or ""),
        "imported": True,
        "source": str(entry.get("source") or ""),
        "imported_from": src,
        "kind": str(entry.get("kind") or "instance"),
    }
    if extra_meta:
        meta.update({k: v for k, v in extra_meta.items() if v not in (None, "")})
    try:
        dest = os.path.join(path, ".mcassist.json")
        old = _read_json(dest)
        old.update({k: v for k, v in meta.items() if v})
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    installer.remember_import(path, meta)
    return meta


def absorbed_sources(game_dir=None):
    """已经被导进官方启动器的来源目录（realpath 小写集合）。

    导入现在是「搬进来」，源目录和实例目录不再是同一个路径；只看档案 gameDir
    会把导过的照样显示成「没导入」，一点又搬一遍。
    """
    out = set()
    for rec in installer.imported_records():
        for k in ("imported_from", "path"):
            p = str(rec.get(k) or "")
            if p:
                out.add(os.path.normcase(os.path.realpath(p)))
    gd = game_dir or installer.detect_game_dir()
    try:
        names = os.listdir(os.path.join(gd, "versions"))
    except Exception:
        names = []
    for n in names:
        mk = _read_json(os.path.join(gd, "versions", n, ".mcassist.json"))
        p = str(mk.get("imported_from") or "")
        if p:
            out.add(os.path.normcase(os.path.realpath(p)))
    return out


# ---------------------------------------------------------------- 已添加（零拷贝）
def auto_import_enabled():
    """设置里的「自动导入」：加完文件夹是否立刻搬进官方启动器（默认关）"""
    return bool(CONFIG.get("auto_import"))


def launch_ref(entry):
    """这个条目能不能「直接运行」（不复制、原地跑）→ (game_dir, version_id)。

    能直接跑的只有一种形态：**版本目录本身**（`versions/<id>/` 里有同名 json）。
       · 原版/加载器版本：`D:/PCL2/.minecraft/versions/1.20.1-forge-...`
       · PCL2 的版本隔离实例：`D:/PCL2/.minecraft/versions/尘封古纪`（同一形态）
    这种目录的 libraries / assets 都在上两级的 .minecraft 里，直接用它当游戏目录
    就能启动，一个字节都不用复制。

    Prism 那种 `instances/xxx` 的目录不算：版本在它们的公共库里，
    得先 ensure_runtime 生成本地版本 —— 那条走「导入」。
    """
    p = os.path.realpath(str((entry or {}).get("path") or ""))
    if not p or not os.path.isdir(p):
        return "", ""
    name = os.path.basename(p.rstrip("\\/"))
    vdir = os.path.dirname(p.rstrip("\\/"))
    if not name or os.path.basename(vdir).lower() != "versions":
        return "", ""
    if not os.path.isfile(os.path.join(p, name + ".json")):
        return "", ""
    gd = os.path.dirname(vdir)
    if not os.path.isdir(os.path.join(gd, "assets")) and \
            not os.path.isdir(os.path.join(gd, "libraries")):
        return "", ""                      # 光有版本目录、资源在别处，跑不起来
    return gd, name


def remember_added(entries, group=""):
    """记下「添加已有文件夹」加进来的目录（只登记，不复制）。

    单独一张表（CONFIG.added_dirs）而不是复用 imported：imported 是「真的搬进
    官启了」的凭据，这里是「玩家指出来的游戏」，两件事，混在一起会让
    absorbed_sources() 误判成已导入。

    `group` 是收放式文件夹的 id（见 remember_group）；给了就归到这个文件夹下，
    不给则保留这条记录原来的归属（老调用点不传也不会把已有分类打散）。
    """
    from datetime import datetime
    rec = dict(CONFIG.get("added_dirs") or {})
    now = datetime.now().isoformat(timespec="seconds")
    for e in (entries or []):
        p = str(e.get("path") or "")
        if not p:
            continue
        key = os.path.normcase(os.path.realpath(p))
        gd, vid = launch_ref(e)
        rec[key] = {"path": p, "name": e.get("name") or os.path.basename(p),
                    "kind": e.get("kind") or "instance", "mc": e.get("mc") or "",
                    "loader": e.get("loader") or "",
                    "loader_ver": e.get("loader_ver") or "",
                    "source": e.get("source") or "", "game_dir": gd,
                    "version_id": vid, "time": now,
                    "group": str(group or (rec.get(key) or {}).get("group") or ""),
                    "icon": e.get("icon") or "", "icon_kind": e.get("icon_kind") or "",
                    "icon_src": e.get("icon_src") or ""}
    CONFIG.set("added_dirs", rec)
    # 重新添加 = 撤销之前的「移除」，名单里要划掉
    gone = [k for k in (CONFIG.get("added_removed") or [])
            if k not in {os.path.normcase(os.path.realpath(str(e.get("path") or "")))
                         for e in (entries or []) if e.get("path")}]
    if gone != list(CONFIG.get("added_removed") or []):
        CONFIG.set("added_removed", gone)


# ------------------------------------------------- 收放式文件夹（分类）----
# 玩家点「添加已有文件夹」选完目录后，会先弹出「分类文件夹重命名」：
# 默认名取**那个大文件夹的名字**，点勾才建、点 × 就当没加过。
# 文件夹 id 用「选中目录的 realpath」—— 同一个目录重复添加会落进同一个文件夹。
_GENERIC_DIRS = {"versions", "instances", "minecraft", ".minecraft", "mods",
                 "modpacks", "resourcepacks", "shaderpacks", "游戏", "新建文件夹"}


def default_group_name(path):
    """给「分类文件夹」起个默认名：取选中目录的名字。

    选的是 `versions` / `instances` / `.minecraft` 这种通用目录（很多人会直接
    指到这一层）时往上找，直到拿到一个像样的名字，否则一屏全是「versions」。
    """
    p = str(path or "").rstrip("\\/")
    if not p:
        return "未命名文件夹"
    name = os.path.basename(p)
    up = p
    for _ in range(3):                 # 最多往上三层，防止一直走到盘符
        if name and name.lower() not in _GENERIC_DIRS:
            break
        parent = os.path.dirname(up)
        if not parent or parent == up:
            break
        up = parent
        name = os.path.basename(up) or name
    return name or "未命名文件夹"


def group_id_for(path):
    """文件夹 id：同一个目录 → 同一个 id（大小写 / 短路径都归一）"""
    p = str(path or "")
    if not p:
        return ""
    return os.path.normcase(os.path.realpath(p))


def remember_group(gid, name, path=""):
    """登记 / 改名一个分类文件夹（同名文件夹互不影响，各占一个 id）"""
    from datetime import datetime
    gid = str(gid or "")
    if not gid:
        return ""
    rec = dict(CONFIG.get("added_groups") or {})
    old = dict(rec.get(gid) or {})
    rec[gid] = {"name": str(name or "").strip() or old.get("name") or "未命名文件夹",
                "path": str(path or old.get("path") or ""),
                "time": old.get("time") or datetime.now().isoformat(timespec="seconds")}
    CONFIG.set("added_groups", rec)
    return gid


def rename_group(gid, name):
    g = (CONFIG.get("added_groups") or {}).get(str(gid or ""))
    if not g:
        return False
    return bool(remember_group(gid, name, g.get("path") or ""))


def groups():
    """所有分类文件夹（按建的时间排序）→ [{id,name,time,path}]"""
    rec = CONFIG.get("added_groups") or {}
    out = [{"id": gid, "name": g.get("name") or "未命名文件夹",
            "time": g.get("time") or "", "path": g.get("path") or ""}
           for gid, g in rec.items() if isinstance(g, dict)]
    out.sort(key=lambda g: (g["time"], g["id"]))
    return out


def group_name(gid):
    g = (CONFIG.get("added_groups") or {}).get(str(gid or "")) or {}
    return str(g.get("name") or "")


def set_entry_group(path, gid):
    """把某条已登记目录挪到另一个文件夹（gid 为空 = 取消分类）"""
    key = os.path.normcase(os.path.realpath(str(path or "")))
    rec = dict(CONFIG.get("added_dirs") or {})
    if key not in rec:
        return False
    rec[key]["group"] = str(gid or "")
    CONFIG.set("added_dirs", rec)
    return True


def forget_added(path):
    """从「已导入」列表里移除。

    只删登记、绝不动硬盘上的文件。已经搬进官启的那类，光删登记还不够 ——
    `added_entries` 会从 installer.imported_records() 再把它捞回来，用户会觉得
    按钮没反应。所以同时记进 added_removed，合并那张表时跳过。
    """
    key = os.path.normcase(os.path.realpath(str(path or "")))
    rec = dict(CONFIG.get("added_dirs") or {})
    hit = rec.pop(key, None) is not None
    if hit:
        CONFIG.set("added_dirs", rec)
    if os.path.isdir(str(path or "")):
        removed = list(CONFIG.get("added_removed") or [])
        if key not in removed:
            removed.append(key)
            CONFIG.set("added_removed", removed)
    return hit


def retarget_added(path, game_dir, version_id, kind="", lver="", mc=""):
    """把登记里这条游戏**指到另一个版本目录**上（第九十三轮：改完加载器要跟着走）。

    装完新的 Forge/NeoForge/Fabric 之后，磁盘上多了一个 `versions/<新 id>`，
    而登记表里的 `path` 还指在旧版本目录上 —— 不改的话玩家「改完加载器还是老版本」，
    比没这个功能更迷惑。

    只改**登记**（`added_dirs`），硬盘上的旧版本目录一个字节都不动：想回退就再改回来。
    返回是否真的改到（不是我们登记过的游戏返回 False，调用方按「只安装、不切换」提示）。
    """
    p = str(path or "")
    gd = str(game_dir or "")
    vid = str(version_id or "")
    if not p or not gd or not vid:
        return False
    key = os.path.normcase(os.path.realpath(p))
    rec = dict(CONFIG.get("added_dirs") or {})
    old = rec.pop(key, None)
    if old is None:
        return False
    new_path = os.path.join(gd, "versions", vid)
    new_key = os.path.normcase(os.path.realpath(new_path))
    item = dict(old)
    item.update({"path": new_path, "game_dir": gd, "version_id": vid})
    if kind:
        item["loader"] = kind
    if lver:
        item["loader_ver"] = lver
    if mc:
        item["mc"] = mc
    rec[new_key] = item
    CONFIG.set("added_dirs", rec)
    return True


def forget_many(paths) -> int:
    """批量从「已导入」列表移除，返回真的移掉了几条。

    语义跟单条 `forget_added` 一样：**只删登记，绝不动硬盘上的文件**。
    已经进官启的那类（只在 installer.imported_records 里）靠 added_removed 挡住，
    否则下次刷新还会被捞回来。
    """
    n = 0
    for raw in list(paths or []):
        p = str(raw or "")
        if not p:
            continue
        try:
            hit = forget_added(p)
        except Exception:                              # noqa: BLE001
            continue
        # 目录还在（说明是「已配置」那类，靠 added_removed 屏蔽）也算移掉了
        if hit or os.path.isdir(p):
            n += 1
    return n


def forget_group(gid) -> int:
    """整组移除：组里的游戏一起移除，分组登记也删掉（不动硬盘）。"""
    gid = str(gid or "")
    if not gid:
        return 0
    rec = dict(CONFIG.get("added_dirs") or {})
    paths = [str(r.get("path") or "") for r in rec.values()
             if str(r.get("group") or "") == gid]
    n = forget_many([p for p in paths if p])
    groups = dict(CONFIG.get("added_groups") or {})
    if groups.pop(gid, None) is not None:
        CONFIG.set("added_groups", groups)
    return n


def official_game_dir():
    """官方启动器那一份 `.minecraft`：`<安装目录>\\official_assets\\.minecraft`。

    第七十轮：「配置至官方启动器」必须写进这里 —— 以前走的是
    `installer.detect_game_dir()`（那是**应用自己**的游戏目录），版本和档案全落在
    应用那份里，官方启动器在自己的 gameDir 下根本看不到，等于白配。
    """
    try:
        from . import paths
        return paths.official_game_dir()
    except Exception:                                  # noqa: BLE001
        return installer.detect_game_dir()


def mark_configured(path, extra=None, on=True) -> bool:
    """把「这条已经从本软件配进官方启动器了」写进登记表。

    界面「已配置」页签只认这个标记（不看 versions 目录里碰巧有没有同名版本 ——
    官方启动器自己下的游戏不该被算成我们配的）。
    """
    key = os.path.normcase(os.path.realpath(str(path or "")))
    if not key:
        return False
    rec = dict(CONFIG.get("added_dirs") or {})
    r = rec.get(key)
    if not isinstance(r, dict):
        return False
    r = dict(r)
    r["configured"] = bool(on)
    if on:
        from datetime import datetime
        r["configured_time"] = datetime.now().isoformat(timespec="seconds")
        ex = extra or {}
        if ex.get("version_id"):
            r["official_version"] = str(ex["version_id"])
        if ex.get("instance_dir"):
            r["official_dir"] = str(ex["instance_dir"])
    else:
        r.pop("configured_time", None)
    rec[key] = r
    CONFIG.set("added_dirs", rec)
    return True


def added_entries(game_dir=None):
    """「已导入」页的数据：本软件登记过的 + 已经搬进官启的。

    每条带：
      · runnable    能不能直接运行（版本目录形态）
      · in_launcher 那个版本在**官方** versions 目录里（只是现状，不代表是我们配的）
      · configured  是我们点「配置至官方启动器」配过去的 —— 「已配置」页签只认它
      · game_dir / version_id  直接运行要用的两个值
    目录被玩家删掉的记录顺手清掉，免得列表里留一堆死链接。
    """
    gd = game_dir or official_game_dir()
    out, seen = [], set()
    rec = dict(CONFIG.get("added_dirs") or {})
    recs = installer.imported_records()
    # 「配进官启的那份」按来源目录索引：源条目自己不能跑（Prism 的
    # 实例目录）时，要借这条记录变成能「运行」，否则配完就只剩一个没有按钮的行。
    by_src = {}
    for _r in recs:
        _src = str(_r.get("imported_from") or "")
        if _src:
            by_src.setdefault(os.path.normcase(os.path.realpath(_src)), _r)
    dropped = False
    for key, r in list(rec.items()):
        p = str(r.get("path") or "")
        if not p or not os.path.isdir(p):
            rec.pop(key, None)
            dropped = True
            continue
        item = dict(r)
        g, vid = launch_ref({"path": p})
        item["game_dir"] = g or str(r.get("game_dir") or "")
        item["version_id"] = vid or str(r.get("version_id") or "")
        item["runnable"] = bool(g and vid)
        if not item["runnable"]:
            # 借官方那份实例：配过之后它就在 versions/<包名> 里，能直接跑
            hit = by_src.get(key)
            if hit:
                g2, vid2 = launch_ref({"path": str(hit.get("path") or "")})
                if g2 and vid2:
                    item["game_dir"], item["version_id"] = g2, vid2
                    item["runnable"] = True
                    item["official_path"] = str(hit.get("path") or "")
        item["in_launcher"] = _in_launcher(p, item, gd)
        item["imported"] = bool(item["in_launcher"])
        # 老数据没有 configured 字段（1.4.3 之前配的）：按现状推断，别让老玩家
        # 升级后「已配置」一下清空
        flag = r.get("configured")
        item["configured"] = bool(item["in_launcher"] if flag is None else flag)
        item["group"] = str(r.get("group") or "")
        item["group_name"] = group_name(item["group"])
        out.append(item)
        seen.add(key)
    if dropped:
        CONFIG.set("added_dirs", rec)

    # 已经搬进官启、但没在 added 表里的（用「导入选中」导的）也列出来
    removed = set(CONFIG.get("added_removed") or [])
    # 第七十一轮：配置过的那些**会同时**留下一条 imported 记录（path 指
    # `official_assets/.../versions/<包名>`），它的 `imported_from` 就是上面那条
    # 源目录。以前不去重，于是每配一次，列表底部就多出一批「多出来的游戏」。
    # 只要源目录已经在列表里，这条 imported 记录就是同一个人，跳过。
    for r in recs:
        p = str(r.get("path") or "")
        if not p:
            continue
        key = os.path.normcase(os.path.realpath(p))
        if key in seen:
            continue
        src_raw = str(r.get("imported_from") or "")
        src_key = os.path.normcase(os.path.realpath(src_raw)) if src_raw else ""
        if src_key and src_key in seen:
            continue                      # 同一个游戏的重复条目（配置后必有）
        # 玩家点过「移除」的不再捞回来（imported 记录留着，不然官启那边会当没导过）
        if key in removed or src_key in removed:
            continue
        seen.add(key)
        g, vid = launch_ref({"path": p})
        vid = vid or str(r.get("version_id") or "")
        g = g or os.path.realpath(gd)
        out.append({"path": p, "name": r.get("name") or os.path.basename(p),
                    "kind": r.get("kind") or "instance", "mc": r.get("mc") or "",
                    "loader": r.get("loader") or "",
                    "loader_ver": r.get("loader_ver") or "",
                    "source": r.get("source") or "", "time": r.get("time") or "",
                    "group": "", "group_name": "",
                    "icon": r.get("icon") or "", "icon_kind": r.get("icon_kind") or "",
                    "icon_src": r.get("icon_src") or "",
                    "game_dir": g, "version_id": vid,
                    "runnable": bool(vid and os.path.isfile(
                        os.path.join(g, "versions", vid, vid + ".json"))),
                    # 这份记录只可能是本软件搬进官启的 → 天然算「已配置」
                    "in_launcher": True, "imported": True, "configured": True})
    out.sort(key=lambda e: (not e.get("configured"), not e.get("in_launcher"),
                            (e.get("name") or "").lower()))
    return out


def _in_launcher(path, item, game_dir):
    """这个条目是不是已经进官方启动器了"""
    if os.path.normcase(os.path.realpath(path)) in absorbed_sources(game_dir):
        return True
    try:
        names = {n.lower() for n in os.listdir(os.path.join(game_dir, "versions"))}
    except Exception:
        return False
    vid = str(item.get("version_id") or "").lower()
    if vid and vid in names:
        return True
    return str(item.get("name") or "").lower() in names


# ---------------------------------------------------------------- 导入
def import_one(entry, game_dir=None, progress=None, move=False, select=True):
    """导入一条。返回 (ok, 中文说明, extra)。

    extra 里带 version_id / profile_id，界面可以据此再定位。
    move=True 时（只对实例有效）搬完把原实例目录丢进回收站，省一份空间。

    `game_dir` 不传就落到**官方启动器那一份**（`official_assets\\.minecraft`）——
    「配置至官方启动器」要的就是这个；只有本启动器自己用的场景才需要显式传别的目录。
    `select=False` 时**不改玩家当前选中的启动项**（批量配置不动原有游戏）。
    """
    gd = game_dir or official_game_dir()
    if installer.launcher_running():
        return False, "官方启动器正在运行，先关掉它再导入（它会覆盖刚写的档案）", {}
    if entry.get("imported"):
        return True, f"「{entry.get('name')}」已经在官方启动器里了", {}

    if entry.get("kind") == "version":
        return _import_version(entry, gd, progress, select=select)
    return _import_instance(entry, gd, progress, move=bool(move), select=select)


def _import_version(entry, gd, progress, select=True):
    info = runtime.describe_version(entry["path"])
    if not info:
        return False, "这个版本目录不完整（缺少可识别的版本 JSON）", {}
    if progress:
        progress(f"采用版本 {info['id']}…")
    # describe_version 不带 root；adopt 靠它决定去哪共享 libraries/assets，
    # 缺了就退化成「只搬 json+jar」。
    if not info.get("root"):
        info["root"] = entry.get("root") or os.path.dirname(
            os.path.dirname(entry["path"]))
    vid = runtime._safe_id(info["mc"], info["loader"], info["loader_ver"],
                           info["id"])
    if os.path.isfile(os.path.join(gd, "versions", vid, vid + ".json")):
        return True, f"{vid} 已在官方启动器里，无需重复导入", {"version_id": vid}
    notes = []
    try:
        vid = runtime.adopt(gd, info, notes, progress)
    except Exception as e:
        return False, f"采用失败：{e}", {}
    # 加载器版本常常 inheritsFrom 原版，官启要自己去下原版 json
    if info["mc"] and not os.path.isfile(
            os.path.join(gd, "versions", info["mc"], info["mc"] + ".json")):
        try:
            if progress:
                progress(f"补全原版 {info['mc']}…")
            runtime.fetch_vanilla(gd, info["mc"], progress)
        except Exception:
            notes.append(f"原版 {info['mc']} 需要官启自己补下（首次启动会下载）")
    msg = f"已导入版本 {vid}"
    if info["loader"]:
        msg += f"（{LOADER_CN.get(info['loader'], info['loader'])} {info['loader_ver']}）"
    # 只有版本目录还不够：官启的「安装」列表是另一套数据，得给它一条档案。
    # 目录就指到 versions/<vid> 本身（PCL2 的版本隔离语义：版本目录=实例目录）。
    prof_ok, pid = installer._register_profile(
        gd, str(entry.get("name") or vid),
        os.path.join(gd, "versions", vid), vid, select=select)
    if prof_ok:
        installer.remember_import(os.path.join(gd, "versions", vid), {
            "name": str(entry.get("name") or vid), "mc": info["mc"],
            "loader": info["loader"], "loader_ver": info["loader_ver"],
            "version_id": vid, "profile_id": pid,
            "source": str(entry.get("source") or ""), "kind": "version",
            "imported_from": str(entry.get("path") or ""),
        })
    else:
        notes.append(f"启动项没写进去：{pid}")
    if notes:
        msg += "；" + "；".join(notes[:2])
    return True, msg, {"version_id": vid, "profile_id": pid if prof_ok else ""}


def _import_instance(entry, gd, progress, move=False, select=True):
    mc, kind, lver = entry.get("mc") or "", entry.get("loader") or "", \
        entry.get("loader_ver") or ""
    # 第一百零八轮：以前只在「mc 空 或 kind/lver 全空」时重认，于是
    # 「认出了 Forge 但没记下版本号」的实例带着 lver='' 直接进 ensure_runtime，
    # 那里 `and lver` 的安装分支整段跳过 → 导入成功却永远缺加载器。
    # 这里补一次本地识别（三合一识别器还会从 mods 的加载器 jar 名推版本号）。
    if not mc or not kind or not lver:
        mc2, kind2, lver2 = instance_env(entry["path"])
        mc, kind, lver = mc or mc2, kind or kind2, lver or lver2
    if not mc:
        return False, "认不出这个实例的 MC 版本（可在导入窗口里手动选它的 versions 目录）", {}
    if not os.path.isdir(entry["path"]):
        return False, "原实例目录不存在（可能已被删除或移动）", {}

    # 内存分配得在搬之前读：instance.cfg / minecraftinstance.json 属于
    # 别的启动器的元数据，不会被搬进新目录，搬完就查不到了。
    mem_mb, mem_src = installer.pack_declared_memory(entry["path"])

    if progress:
        progress(f"为「{entry['name']}」准备运行版本…")
    vid, notes = runtime.ensure_runtime(gd, mc, kind, lver, progress)
    if not vid:
        return False, "；".join([n for n in notes if n]) or "缺少匹配的运行版本", {}

    # ① 真正搬进官方启动器：versions/<包名>（PCL2 的版本隔离语义）
    target = installer.isolated_dir(gd, entry["name"])
    if progress:
        progress("把实例内容搬进官方启动器…")
    ok_move, why, n_files = installer.absorb_instance(entry["path"], target, progress)
    if not ok_move:
        return False, f"没能把实例搬进官方启动器：{why}", {}

    # ② 版本列表里得有一项跟这个目录同名，官启才认这是一个可启动的实例。
    # 名字取**实际目录名**：撞名时 isolated_dir 会退到「包名-2」，
    # 拿 entry["name"] 会把 json 写进另一个目录。
    base_vid = vid
    pack_vid, vnote = installer.ensure_pack_version(
        gd, base_vid, os.path.basename(target.rstrip("\\/")) or entry["name"])
    if pack_vid:
        vid = pack_vid

    # ③ 档案指向搬好的目录
    ok, pid = installer._register_profile(gd, entry["name"], target, vid,
                                          memory_mb=mem_mb, select=select)
    # 不管档案写没写成，标记与登记都要落下 —— 这样「游戏管理」一定能看到，
    # 下次官启吃掉档案时也能按登记表补回来。
    _mark_imported(entry, vid, pid if ok else "", target=target, extra_meta={
        "memory_mb": mem_mb, "base_version": base_vid,
        "imported_from": str(entry.get("path") or ""),
    })

    # ④ 可选：把原实例丢回收站（先确认搬成功，且走回收站可还原）
    dropped = ""
    if move:
        src = str(entry.get("path") or "")
        done, dmsg = installer.move_to_recycle(src)
        dropped = ("；原实例已移入回收站" if done
                   else f"；原实例没删掉（{dmsg}），可手动清理")

    extra = {"version_id": vid, "profile_id": pid if ok else "",
             "instance_dir": target, "moved": bool(move), "files": n_files}
    if not ok:
        return False, (f"「{entry['name']}」已搬到 versions 里（运行版本 {vid}），"
                       f"但启动项没写进官方启动器：{pid}"), extra
    msg = f"已把「{entry['name']}」配置进官方启动器（versions/{os.path.basename(target)}，运行版本 {vid}）"
    if mem_mb:
        msg += f"，内存 {mem_mb // 1024} GB（{mem_src or '整合包声明'}）"
    if notes:
        msg += "；" + "；".join([n for n in notes if n][:2])
    msg += dropped
    return True, msg, extra
