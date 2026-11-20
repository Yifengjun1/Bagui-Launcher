# -*- coding: utf-8 -*-
"""游戏分类：给「游戏版本」和「整合包」各建一套收放式分类（第一百零五轮）。

**为什么另起一份而不复用 `added_groups`**：那个是「已导入」外部文件夹的分组，
键语义（id -> {name, path, time}）与每条 `added_dirs.group` 的挂法已经被
`paths._rebase_after_move`、`installer.heal_rebased_metadata` 认死了；往里塞
`kind` / `members` 会让第 99/101 轮那套「语义字段黑名单 + 自愈」误判，搬目录时
还可能把分类名改成总路径。新键独立，搬目录只要加进重绑键表即可。

四条纪律：
1. 分类只存「名字 + 指向游戏的标识」，**绝不存游戏文件**；删分类不动硬盘；
2. `kind` 必须匹配：整合包（instance）只能进 `pack` 分类，游戏版本只能进
   `version` 分类 —— 玩家把整合包拖到「游戏版本」分类一律拒绝并说清原因；
3. 配置坏了（旧值 / 手改 / 缺字段 / 未来的新档位）一律退回空，不抛异常：
   这个模块会被界面、安装完成回调、启动选择同时读，抛异常等于装完就崩；
4. 成员键与 `launchprefs.game_key()` 同口径（normcase(realpath)），
   已在 `paths._rebase_after_move` 里跟着总路径重绑，搬家不会变成死键。
"""
import os
import time
import uuid
import weakref

from .config import CONFIG

# ---------------------------------------------------------------- 常量
KINDS = (
    ("version", "游戏版本", "放「游戏管理 → 游戏版本」里的原版 / 加载器版本"),
    ("pack", "整合包", "放本启动器下载或导入的整合包实例"),
)
KIND_KEYS = tuple(k for k, _n, _d in KINDS)
KIND_CN = dict((k, _n) for k, _n, _d in KINDS)
KEY = "game_categories"

# 「按 kind 归类」时的显示名，界面与提示共用一份，别各写各的
KIND_FOR_ITEM = {"version": "version", "instance": "pack", "pack": "pack"}

# ---------------------------------------------------------------- 变更通知
# 第 106 轮：游戏管理与「选择要启动的游戏」是**两个独立的 QWidget**，谁在里面
# 建/删/拖分类，另一边必须当场跟着变。core 层不许 import Qt，所以这里只存
# 回调（界面侧用 weakref.WeakMethod 订阅，popup 关了自动失效）。
# 回调一律同步执行，且每个都单独 try —— 一个视图炸了不能挡住另一个刷新。
_SUBS = []


def subscribe(cb):
    if cb is not None and cb not in _SUBS:
        _SUBS.append(cb)


def unsubscribe(cb):
    """WeakMethod 每造一次都是新对象，靠 __eq__ 认同一个绑定方法。"""
    keep = []
    for ref in _SUBS:
        if ref == cb:
            continue
        # 顺手清掉已经死了的引用
        if isinstance(ref, weakref.ReferenceType):
            try:
                if ref() is None:
                    continue
            except Exception:
                continue
        keep.append(ref)
    _SUBS[:] = keep


def subscriber_count() -> int:
    return len(_SUBS)


def notify_changed():
    for ref in list(_SUBS):
        try:
            fn = ref() if isinstance(ref, weakref.ReferenceType) else ref
        except Exception:
            fn = None
        if fn is None:
            # WeakMethod 指向的对象已销毁：顺手把死引用摘掉，别越攒越多
            try:
                _SUBS.remove(ref)
            except (ValueError, TypeError):
                pass
            continue
        try:
            fn()
        except Exception:
            pass


def kind_cn(kind) -> str:
    return KIND_CN.get(str(kind or ""), "分类")


def item_kind(item) -> str:
    """从游戏条目推它属于哪种分类。"""
    if isinstance(item, str):
        return "pack"
    it = item or {}
    k = str(it.get("kind") or "").lower()
    if k in KIND_KEYS:
        return k
    if k in ("instance", "modpack", "pack"):
        return "pack"
    # 没写 kind 时：带 .mcassist 标记 / 导入标记 / 明确 is_pack 的都是整合包
    if it.get("is_pack") or it.get("imported") or it.get("has_meta"):
        return "pack"
    return "version"


def game_key(item) -> str:
    """成员标识：与 `launchprefs.game_key` 完全同一口径。"""
    from . import launchprefs
    return launchprefs.game_key(item)


# ---------------------------------------------------------------- 底层读写
def _raw() -> dict:
    try:
        d = CONFIG.get(KEY)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def _write(d: dict):
    CONFIG.set(KEY, d)


def _clean_name(name) -> str:
    s = "".join(ch for ch in str(name or "")
                if ch not in '\\/:*?"<>|').strip().strip(" ._")
    return s[:40] or "未命名分类"


def _norm(rec, cid) -> dict | None:
    """一条分类记录 → 干净结构；坏记录直接丢掉，不让界面拿到脏数据。"""
    if not isinstance(rec, dict):
        return None
    kind = str(rec.get("kind") or "")
    if kind not in KIND_KEYS:
        return None
    mem = rec.get("members")
    if not isinstance(mem, (list, tuple, set)):
        mem = []
    try:
        t = float(rec.get("time") or 0)
    except (TypeError, ValueError):
        t = 0.0
    return {
        "id": str(cid),
        "name": _clean_name(rec.get("name")),
        "kind": kind,
        "members": [str(m) for m in mem if isinstance(m, str) and m],
        "time": t,
    }


# ---------------------------------------------------------------- 查询
def categories(kind=None) -> list[dict]:
    """分类列表（按创建时间升序）。kind 传 None = 全要。"""
    out = []
    for cid, rec in _raw().items():
        c = _norm(rec, cid)
        if c is None:
            continue
        if kind is not None and c["kind"] != str(kind):
            continue
        out.append(c)
    out.sort(key=lambda c: (c["time"], c["id"]))
    return out


def has_any(kind=None) -> bool:
    return bool(categories(kind))


def get(cid) -> dict | None:
    rec = _raw().get(str(cid or ""))
    return _norm(rec, cid) if rec is not None else None


def categories_of(key, kind=None) -> list[dict]:
    """这个游戏当前在哪些分类里。"""
    k = str(key or "")
    if not k:
        return []
    return [c for c in categories(kind) if k in c["members"]]


def members_of(cid) -> list[str]:
    c = get(cid)
    return list(c["members"]) if c else []


def count_members(cid) -> int:
    return len(members_of(cid))


# ---------------------------------------------------------------- 增删改
def create(name, kind, notify=True) -> str:
    """建一个**空**分类，返回 id。重名自动加 (2)(3)…，不覆盖已有分类。"""
    kind = str(kind or "")
    if kind not in KIND_KEYS:
        kind = "pack"
    base = _clean_name(name)
    rec = dict(_raw())
    used = {(_norm(v, k) or {}).get("name") for k, v in rec.items()
            if (_norm(v, k) or {}).get("kind") == kind}
    cand, i = base, 2
    while cand in used:
        cand = f"{base} ({i})"
        i += 1
    cid = "cat-" + uuid.uuid4().hex[:12]
    rec[cid] = {"name": cand, "kind": kind, "members": [],
                "time": time.time()}
    _write(rec)
    if notify:
        notify_changed()
    return cid


def rename(cid, name, notify=True) -> bool:
    rec = dict(_raw())
    c = _norm(rec.get(str(cid or "")), cid)
    if c is None:
        return False
    c["name"] = _clean_name(name)
    rec[str(cid)] = c
    _write(rec)
    if notify:
        notify_changed()
    return True


def remove(cid, notify=True) -> bool:
    """只删分类本身；里面的游戏只是回到「未分类」，硬盘一个字节都不动。"""
    rec = dict(_raw())
    if str(cid or "") not in rec:
        return False
    rec.pop(str(cid), None)
    _write(rec)
    if notify:
        notify_changed()
    return True


def unassign(key, kind=None, notify=True) -> int:
    """把某个游戏从所有（或指定 kind 的）分类里摘出来。返回摘掉几处。"""
    k = str(key or "")
    if not k:
        return 0
    rec = dict(_raw())
    n = 0
    for cid, raw in list(rec.items()):
        c = _norm(raw, cid)
        if c is None:
            continue
        if kind is not None and c["kind"] != str(kind):
            continue
        if k in c["members"]:
            c["members"] = [m for m in c["members"] if m != k]
            rec[cid] = c
            n += 1
    if n:
        _write(rec)
        if notify:
            notify_changed()
    return n


def assign(cid, key, kind="", exclusive=True, notify=True) -> tuple[bool, str]:
    """把一个游戏放进分类。返回 (是否成功, 说明)。

    · `kind` 是**游戏**的种类，不是分类的种类；类型不匹配直接拒绝；
    · `exclusive=True`（默认）表示「拖过去」= 移动：先从同 kind 的其它分类摘掉，
      一个游戏只待在一个分类里，界面上不会同一条出现两次。
    """
    c = get(cid)
    if c is None:
        return False, "这个分类已经不存在了"
    k = str(key or "")
    if not k:
        return False, "没认出这个游戏"
    gk = str(kind or "")
    if gk in KIND_FOR_ITEM:
        gk = KIND_FOR_ITEM[gk]
    if gk not in KIND_KEYS:
        gk = c["kind"]
    if gk != c["kind"]:
        return False, (f"「{KIND_CN.get(gk, gk)}」只能放进"
                       f"「{kind_cn(gk)}」分类，这个是「{kind_cn(c['kind'])}」")
    if exclusive:
        unassign(k, kind=gk, notify=False)
        c = get(cid)
        if c is None:
            return False, "这个分类已经不存在了"
    if k not in c["members"]:
        c["members"].append(k)
    rec = dict(_raw())
    rec[str(cid)] = c
    _write(rec)
    if notify:
        notify_changed()
    return True, f"已放入「{c['name']}」"


# ---------------------------------------------------------------- 安装完成后的归类
def context_from_install(res) -> dict:
    """把「下载 / 导入整合包完成」的结果转成归类上下文。

    整合包固定是 `pack`：玩家装的就是整合包，没有第二种可能。
    `key` 优先用实例目录（搬目录时会跟着重绑），退而求其次用名字做稳定 id。
    """
    r = res or {}
    path = str(r.get("instance_dir") or r.get("dest_dir") or "")
    key = game_key({"path": path}) if path else ""
    if not key or key == "id:":
        nm = str(r.get("name") or "整合包")
        key = "name:" + os.path.normcase(nm.strip().lower())
    return {"key": key, "name": str(r.get("name") or "整合包"),
            "kind": "pack"}


def apply_choice(ctx, choice) -> tuple[bool, str]:
    """落地「装完弹窗」里玩家的归类选择。

    choice = {"mode": "none" | "existing" | "new", "cid": ..., "name": ...}
    · `none`（玩家点「否」）→ 什么都不做，**绝不影响下载 / 导入成功**；
    · 任何异常都吞掉并回一句说明，不许把安装结果对话框带崩。
    """
    try:
        c = choice or {}
        mode = str(c.get("mode") or "none")
        ctx = ctx or {}
        key = str(ctx.get("key") or "")
        kind = str(ctx.get("kind") or "pack")
        if mode == "none" or not key:
            return True, "未创建分类"
        if mode == "new":
            name = _clean_name(c.get("name") or ctx.get("name") or "新分类")
            cid = create(name, kind, notify=False)
            ok, msg = assign(cid, key, kind, notify=False)
            notify_changed()          # 建 + 存只刷一次界面
            return ok, (f"已创建「{name}」并存入" if ok else msg)
        if mode == "existing":
            cid = str(c.get("cid") or "")
            if not cid:
                return True, "没有选择分类"
            ok, msg = assign(cid, key, kind)
            return ok, msg
        return True, "未创建分类"
    except Exception as e:                                  # noqa: BLE001
        return False, f"分类没建成（不影响安装）：{str(e)[:60]}"
