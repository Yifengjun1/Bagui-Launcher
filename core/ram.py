# -*- coding: utf-8 -*-
"""运行内存：本机内存探测 + 「自动配置」的分配算法。

第四十四轮新增「自动配置」：不再让玩家自己猜该给多少，而是按
**装了多少模组** 和 **系统还剩多少内存** 算一个值。

为什么是这两个量：
  · 模组越多，堆越吃紧（每个 mod 的静态数据、模型烘焙、配方都在堆里）；
  · 但给再多也不能超过「当前剩的」—— 否则游戏一起来就把系统挤爆，
    表现是整台机器发卡、甚至 java 直接被系统杀掉（退出码 -1073741819 那种）。

算法（可解释、可测试、单调）：
    need  = 4096 + min(4096, mods * 16)      # 50 个 mod +800MB，250 个封顶 +4096
    capA  = 剩余物理内存 * 0.6               # 给系统和其他程序留 40%
    capT  = 物理内存 * 0.75                  # 别把整机内存都划给 JVM
    mb    = clamp(need, 4096, min(capA, capT, 16384))
    再向下取整到 256 的倍数（JVM 喜欢整数，界面上也好看）

第六十七轮：下限与基数都提到 **4 GB**（用户要求「自动分配在 4G 以上」）。
以前没模组给 2048MB，1.21 之后原版自己就要 1.5~2G，再叠上光影 / 内置资源包
基本一进世界就卡 GC；现在起手就是 4G，模组多再往上加。
只有一种例外：机器本身太小（`cap` 不足 4G，比如 4G 内存的笔记本），那时按物理
上限给 —— 硬给 4096 会让 Windows 自己去用页面文件，反而更卡。
"""
import ctypes
import os


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def stats() -> dict:
    """{total_mb, avail_mb, load}（拿不到就给保守值，绝不抛异常）"""
    try:
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return {"total_mb": int(st.ullTotalPhys // (1024 * 1024)),
                    "avail_mb": int(st.ullAvailPhys // (1024 * 1024)),
                    "load": int(st.dwMemoryLoad)}
    except Exception:
        pass
    return {"total_mb": 8192, "avail_mb": 4096, "load": 50}


BASE_MB = 4096          # 第六十七轮：起手 4 GB（原版 1.21 自己就要 1.5~2G）
PER_MOD_MB = 16
MOD_BONUS_CAP = 4096
MIN_MB = 4096           # 自动分配的**下限**：4 GB 以上
HARD_MAX_MB = 16384

# ---- 第一百二十六轮：套用 PCL2「模组感知 + 四阶段渐进式分配」 ----
# 用户反馈旧的「4096 + 模组数×16」不合理：模组一多就撞到 +4096 的天花板，
# 大型整合包（200+ mod）只给 8G，进世界 GC 卡死甚至崩。改用 PCL2 的算法：
#   先按游戏类型/模组数算 4 个目标值（t1 勉强 / t2 流畅 / t3 高载），
#   再按当前可用物理内存分四阶段以 100% / 70% / 40% / 15% 递减比例投喂。
# 关键：PCL2 原版起手才 0.5~1.5G，直接套用会破坏第六十七轮「自动 ≥ 4G」的
# 硬要求（_probe_r67 锁定）。所以最后仍用 MIN_MB 兜底——比 PCL2 更慷慨，
# 但符合本项目的用户约定；机器物理内存本身不足 4G 时才按物理上限给。
# 32 位 Java 的 1G 上限不在这里，由 cap_for_java() / heap_args() 处理。
PCL2_STAGES = (1.0, 0.7, 0.4, 0.15)   # 四阶段递减比例


def count_mods(*dirs) -> int:
    """统计 mods 目录里的模组数量（.jar / .zip / 已解压文件夹都算一个）"""
    seen = set()
    for d in dirs:
        if not d:
            continue
        try:
            if not os.path.isdir(d):
                continue
            for n in os.listdir(d):
                p = os.path.join(d, n)
                low = n.lower()
                if os.path.isdir(p):
                    seen.add(os.path.normcase(p))
                elif low.endswith((".jar", ".zip", ".mrpack")):
                    seen.add(os.path.normcase(p))
        except OSError:
            continue
    return len(seen)


def _pcl2_targets(mods: int) -> tuple[float, float, float]:
    """PCL2 第一步：按游戏类型/模组数算三个目标值（GB）。

    本项目不单独识别 OptiFine，把它归到「普通版本」这一档（差别不大）。
    Modded 的三个系数 90 / 50 / 25 取自 PCL2 表 1：越往高目标，模组数影响越大。
    """
    if mods <= 0:
        return 1.5, 2.5, 4.0
    return (1.5 + mods / 90.0,
            2.7 + mods / 50.0,
            4.5 + mods / 25.0)


def pcl2_stage_sum(mods: int) -> float:
    """PCL2 第二步：四阶段投喂的原始总量（GB），不受可用内存限制。

        t1×100% + (t2−t1)×70% + (t3−t2)×40% + (t3×2−t3)×15%
    第四阶段区间宽度正好是 t3，化简为 t3×15%。60 模组示例算得 ≈5.6G，
    与 PCL2 文档一致。
    """
    t1, t2, t3 = _pcl2_targets(mods)
    return (t1 * PCL2_STAGES[0]
            + (t2 - t1) * PCL2_STAGES[1]
            + (t3 - t2) * PCL2_STAGES[2]
            + t3 * PCL2_STAGES[3])


def auto_alloc(mods: int = 0, st: dict | None = None) -> tuple[int, str]:
    """按 PCL2「模组感知 + 四阶段渐进」算内存，返回 (MB, 中文理由)。

    第六十七轮起的 **4 GB 下限**仍然保留：PCL2 原版起手才 1.5G，直接套用
    会破坏用户的硬约定（`_probe_r67` 锁定「无模组正好 4G」）。所以先用
    PCL2 算需求，再夹到 [4G, 可用/总量/硬顶] 里——大模组包拿到的是 PCL2
    的渐进值（比旧的 8G 天花板高），小包仍是 4G。
    """
    st = st or stats()
    try:
        mods = max(0, int(mods))
    except Exception:
        mods = 0
    total = int(st.get("total_mb") or 0)
    avail = int(st.get("avail_mb") or 0)
    need = int(pcl2_stage_sum(mods) * 1024)
    cap_a = int(max(0, avail) * 0.6)          # 给系统/其它程序留 40%
    cap_t = int(max(0, total) * 0.75)         # 不把整机内存都划给 JVM
    cap = min([c for c in (cap_a, cap_t, HARD_MAX_MB) if c > 0] or [BASE_MB])
    # 下限看**物理总量**而不是当前剩余：`-Xmx` 只是上限、不是开机就预占，
    # 拿「剩余」判下限的话，后台开着一堆程序时会自动掉到 1G（正是第 67 轮要治的）。
    total_cap = min([c for c in (cap_t, HARD_MAX_MB) if c > 0] or [BASE_MB])
    floor = MIN_MB if total_cap >= MIN_MB else max(1024, (total_cap // 256) * 256)
    mb = max(floor, min(need, cap))
    mb = max(floor, (mb // 256) * 256)
    total_gb = total / 1024
    avail_gb = avail / 1024
    why = (f"{mods} 个模组 · 本机 {total_gb:.0f} GB / 剩余 {avail_gb:.1f} GB"
           f" → 建议 {mb / 1024:.1f} GB（PCL2 四阶段）")
    if floor < MIN_MB:
        why += "（物理内存不够 4G，已按上限给）"
    return mb, why


def fmt(mb: int) -> str:
    mb = int(mb or 0)
    if mb % 1024 == 0:
        return f"{mb // 1024} GB"
    return f"{mb / 1024:.1f} GB"


# ---------------------------------------------------------------- 第一百零四轮：位宽 / 堆参数
# PCL-CE 的 GetRam 自动算法（0.5~1.5 GB 起手、按实例类型分档）**不照搬**：
# 第六十七轮用户硬性要求自动分配 4 GB 起，1.21 之后原版自己就要 1.5~2G。
# 这里只引用三件与算法无关、但真能避免启动失败 / 提升手感的东西：
#   ① 32 位 Java 的堆上限（超过可寻址空间，JVM 直接起不来）；
#   ② -Xmn = 15% Xmx、LockMemory(Xms=Xmx)、独立可调初始堆；
#   ③ 按实例类型分档的额外建议值（可选，不改 auto_alloc 本身）。
JAVA_32_MAX_MB = 1024


def java_is_32bit(exe: str):
    """读 PE 头的 Machine 字段判位数：True=32 位，False=64 位，None=判不出。

    `javamanager` 扫描 Java 时只看 `release` 文件和目录名（不启动 java -version，
    那是主线程 345ms 卡顿的来源），不带位数信息。读 PE 头是纯本地、毫秒级，
    与 `javamanager.is_valid_java()` 的路径来源一致，绝不为判位数去起进程。
    """
    if not exe or not os.path.isfile(exe):
        return None
    try:
        with open(exe, "rb") as f:
            head = f.read(0x400)
            if head[:2] != b"MZ" or len(head) < 0x40:
                return None
            off = int.from_bytes(head[0x3c:0x40], "little")
            if off + 6 > len(head):
                f.seek(off)
                pe = f.read(6)
            else:
                pe = head[off:off + 6]
        if pe[:4] != b"PE\x00\x00" or len(pe) < 6:
            return None
        machine = int.from_bytes(pe[4:6], "little")
        if machine == 0x014C:            # IMAGE_FILE_MACHINE_I386
            return True
        if machine in (0x8664, 0xAA64):  # AMD64 / ARM64
            return False
    except Exception:                    # noqa: BLE001
        pass
    return None


def cap_for_java(mb: int, java_exe: str = "") -> tuple[int, str]:
    """32 位 Java 把 -Xmx 压到 1 GB 以内，返回 (MB, 说明)。

    判不出位数就**不动**原值（宁可少一事，也不能凭路径猜成 32 位把 8G 砍到 1G）。
    """
    try:
        mb = int(mb)
    except Exception:                    # noqa: BLE001
        mb = BASE_MB
    if mb <= 0 or java_is_32bit(java_exe) is not True:
        return mb, ""
    if mb <= JAVA_32_MAX_MB:
        return mb, ""
    return JAVA_32_MAX_MB, (f"检测到 32 位 Java：堆最多只能给 {fmt(JAVA_32_MAX_MB)}，"
                            f"已从 {fmt(mb)} 自动下调，否则 JVM 根本起不来")


def heap_args(mb: int, *, lock_memory: bool = False, init_mb: int = 0,
              auto_min: bool = True, xmn: bool = True,
              java_exe: str = "") -> tuple[list, str]:
    """拼 -Xmx / -Xms / -Xmn，返回 (参数列表, 中文说明)。

    - `lock_memory`（PCL-CE LockMemory）：Xms=Xmx 锁住整块堆，利 ZGC，也免去
      运行中扩堆那一下卡顿；开启时不再单独加 -Xms / -Xmn。
    - `init_mb`：独立可调初始堆；0 = 沿用原来的 `max(512, Xmx/2)`。
    - `xmn`：年轻代固定为 Xmx 的 15%（PCL-CE ArgumentsJvm），256MB 对齐。
    """
    mb, why = cap_for_java(mb, java_exe)
    mb = max(1, int(mb))
    out = [f"-Xmx{mb}M"]
    if lock_memory:
        out.append(f"-Xms{mb}M")
    else:
        try:
            init = int(init_mb or 0)
        except Exception:                # noqa: BLE001
            init = 0
        if init > 0:
            out.append(f"-Xms{min(max(128, init), mb)}M")
        elif auto_min:
            out.append(f"-Xms{max(512, mb // 2)}M")
        if xmn and mb >= 2048:
            out.append(f"-Xmn{max(256, (int(mb * 0.15) // 64) * 64)}M")
    return out, why


def tier_hint(item) -> tuple[str, str]:
    """可选的按实例类型分档建议（只给设置页显示，不参与 auto_alloc）。

    PCL-CE 按 Modable / OptiFine / 普通三档给不同基准；我们的自动算法只看
    模组数量与剩余内存，这里补一句人话提示，绝不改 4 GB 下限。
    """
    it = item or {}
    name = str(it.get("name") or it.get("id") or "").lower()
    loader = str(it.get("loader") or "").lower()
    if "optifine" in name:
        return "optifine", "OptiFine 对堆更敏感，建议别超过 8 GB"
    if loader or any(k in name for k in ("forge", "fabric", "neoforge", "quilt")):
        return "modable", "可装模组的实例：模组越多越吃堆，自动配置已按数量上浮"
    return "plain", "普通实例：4 GB 起手足够原版与少量光影"
