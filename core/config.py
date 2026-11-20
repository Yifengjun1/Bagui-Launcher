# -*- coding: utf-8 -*-
"""应用配置持久化"""
import contextlib
import json
import os
import shutil
import uuid
from datetime import datetime

APP_NAME = "八桂启动器"
APP_VERSION = "1.9.3"

# 卸载程序的文件名（第五十一轮）：装进 `<安装目录>\<它>.exe`
UNINSTALL_EXE_NAME = "卸载八桂启动器"

# 安装目录用英文名（八桂 = Osmanthus flower）：中文路径在个别 Java / 资源包 /
# 老版本 Forge 上会出问题，玩家自己也更好找。
# 注意：**只有目录名**用它 —— exe 文件名、快捷方式、窗口标题仍然是中文 APP_NAME。
INSTALL_DIR_NAME = "Osmanthus flower"

# 用户数据目录名（`%APPDATA%\<它>`）。第四十八轮起改成纯英文：
# 中文目录在备份软件 / 同步盘 / 杀软里容易出乱码与权限问题，玩家也难在资源管理器里找。
# 改名靠 `_migrate_legacy_dir()` 整体搬（os.replace 瞬时改名，账号不会丢）。
DATA_DIR_NAME = "bgl_datat_config"

# 自定义数据目录的指针文件（第五十五轮）：写在默认目录里，内容就一行绝对路径。
# 玩家把数据目录改到 D 盘后，启动器靠它找到 config.json —— 所以它必须待在
# 一个「无论如何都存在」的位置，也就是默认目录。
ROOT_POINTER = "datadir.txt"

# 改名前的老名字（第三十八轮：「MC官启辅助工具」→「八桂启动器」；第四十八轮 → bgl_datat_config）。
# 顺序 = 从新到旧，先搬最近的那一份。
# %APPDATA% 下的旧数据目录必须整体搬过来 —— 账号、已导入的游戏、下载源设置
# 全在 config.json 里，直接换目录等于让玩家「一更新就什么都丢了」。
LEGACY_APP_NAMES = ("八桂启动器", "MC官启辅助工具")
# 安装目录（%LOCALAPPDATA%\Programs\...）里可能用过的老名字。
# 「八桂启动器」也在里面：第四十一轮起安装目录改叫 Osmanthus flower，
# 老玩家的中文目录要整体改名搬过去，不能两份并存。
LEGACY_INSTALL_NAMES = ("MC assistant tool", "MC官启辅助工具", "八桂启动器")


def next_version(cur: str = "") -> str:
    """版本号递增规则（第三十八轮定）：patch 位从 1 数到 10，数满再进位。

    1.2.0 → 1.2.1 → 1.2.2 … → 1.2.10 → 1.3.0 → 1.3.1 …
    即「每个小版本最多发 10 次补丁版」，第 11 次就升小版本。
    """
    try:
        parts = [int(x) for x in str(cur or APP_VERSION).split(".")]
    except ValueError:
        parts = [1, 0, 0]
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[0], parts[1], parts[2]
    patch += 1
    if patch > 10:
        minor += 1
        patch = 0
    return f"{major}.{minor}.{patch}"


def _merge_newer_files(base: str, d: str):
    """新旧两个数据目录并存时，把**较新**的配置同步过来（只做一次）。

    现场很常见：改名那一刻旧版还开着 → `os.replace` 被占用 → 退化成复制，
    旧目录删不掉，于是玩家继续用旧版往旧目录里写设置。等新版第一次启动，
    如果只认新目录，玩家最近改的东西就凭空消失了。这里比 mtime 取新的那份。
    """
    flag = os.path.join(d, ".merged_v48")
    if os.path.isfile(flag):
        return
    for old in LEGACY_APP_NAMES:
        src = os.path.join(base, old)
        if not os.path.isdir(src):
            continue
        for name in ("config.json", "config.json.pre26.bak"):
            s, t = os.path.join(src, name), os.path.join(d, name)
            try:
                if not os.path.isfile(s):
                    continue
                if os.path.isfile(t) and os.path.getmtime(s) <= os.path.getmtime(t) + 1:
                    continue
                shutil.copy2(s, t)
            except OSError:
                pass
    try:
        with open(flag, "w", encoding="utf-8") as f:
            f.write("1")
    except OSError:
        pass


def _migrate_legacy_dir(base: str, d: str):
    """改名后第一次启动：把 %APPDATA%\\<旧名> 整体搬到新名字下。

    同卷直接 os.replace（瞬时改名）；被占用（旧版还开着 / 文件被锁）就退化成
    复制。复制后**先核对 config.json 能不能打开**，确认没问题才删旧目录 ——
    宁可留一份脏数据，也不能让玩家丢账号。
    """
    if os.path.isdir(d):
        # 目标已经在（上次搬过一半 / 玩家自己建过）→ 至少把较新的配置同步过来
        _merge_newer_files(base, d)
        return
    for old in LEGACY_APP_NAMES:
        src = os.path.join(base, old)
        if not os.path.isdir(src):
            continue
        try:
            os.replace(src, d)           # 同卷改名
            return
        except OSError:
            pass
        try:
            shutil.copytree(src, d, dirs_exist_ok=True)
        except Exception:
            return                        # 复制都失败：保留旧目录，下次再试
        # 复制成功 → 校验一遍再删旧的（新目录里的 config.json 必须能读）
        good = True
        for name in ("config.json", "config.json.pre26.bak"):
            s, t = os.path.join(src, name), os.path.join(d, name)
            if os.path.isfile(s):
                try:
                    good = good and os.path.getsize(s) == os.path.getsize(t)
                except OSError:
                    good = False
        if good:
            shutil.rmtree(src, ignore_errors=True)
        return


def app_data_dir() -> str:
    """当前生效的数据目录（%APPDATA%\\bgl_datat_config 或玩家自己指定的一条路径）。

    第五十五轮起玩家可以在「设置 → 游戏目录 → 数据 / 配置路径」里改它。
    指针文件 `datadir.txt` **永远放在默认目录**里 —— 配置本身不能存在被自己
    指定的那个目录下，否则改一次就再也找不回来了（先有鸡还是先有蛋）。
    """
    # MCASSIST_DATA_DIR 给测试/便携模式用：不设就还是 %APPDATA%\<DATA_DIR_NAME>。
    # 测试必须能改这个，否则跑一遍回归就会把临时目录写进玩家的真实配置。
    d = default_data_dir()
    custom = custom_data_dir()
    if custom and os.path.normcase(os.path.abspath(custom)) != \
            os.path.normcase(os.path.abspath(d)):
        try:
            os.makedirs(custom, exist_ok=True)
            _seed_custom(d, custom)
            return custom
        except OSError:
            return d              # 自定义盘拔了 / 没权限 → 退回默认，别让启动器起不来
    os.makedirs(d, exist_ok=True)
    return d


def default_data_dir() -> str:
    """出厂默认的数据目录（也叫「恢复默认」的去处）"""
    base = os.environ.get("MCASSIST_DATA_DIR") or os.environ.get("APPDATA") \
        or os.path.expanduser("~/.config")
    if os.environ.get("MCASSIST_DATA_DIR"):
        d = base
    else:
        d = os.path.join(base, DATA_DIR_NAME)
        _migrate_legacy_dir(base, d)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _root_pointer() -> str:
    """指针文件路径（始终在默认目录里）"""
    return os.path.join(default_data_dir(), ROOT_POINTER)


def custom_data_dir() -> str:
    """指针里记的自定义数据目录（没设 / 读不到 → 空串）"""
    try:
        with open(_root_pointer(), "r", encoding="utf-8") as f:
            v = f.read().strip()
    except (OSError, UnicodeDecodeError):
        return ""
    if not v:
        return ""
    return os.path.abspath(os.path.expandvars(os.path.expanduser(v)))


def _seed_custom(default: str, custom: str):
    """自定义目录里还没有 config.json 时，把默认目录那份（含账号）拿过去。

    只补 config，不用整目录拷 —— 缓存与日志下次会自己重建。
    """
    for name in ("config.json", "config.json.pre26.bak"):
        s = os.path.join(default, name)
        t = os.path.join(custom, name)
        if os.path.isfile(s) and not os.path.isfile(t):
            try:
                shutil.copy2(s, t)
            except OSError:
                pass


def _keep_newer_config(src: str, dst: str):
    """两份 config.json 并存时保较新的那份（复制方向：src → dst）"""
    s = os.path.join(src, "config.json")
    t = os.path.join(dst, "config.json")
    try:
        if not os.path.isfile(s):
            return
        if os.path.isfile(t) and os.path.getmtime(s) <= os.path.getmtime(t) + 1:
            return
        shutil.copy2(s, t)
    except OSError:
        pass


def set_data_root(path) -> tuple[bool, str]:
    """把数据目录改到 path：先整份拷数据、再写指针，**旧目录一个字节都不删**。

    返回 (ok, 说明)：成功时说明是新的绝对路径。
    """
    path = str(path or "").strip()
    default = default_data_dir()
    cur = app_data_dir()
    if not path:
        return reset_data_root()
    try:
        p = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
    except Exception:
        return False, "路径不合法"
    if os.path.normcase(p) == os.path.normcase(os.path.abspath(default)):
        return reset_data_root()
    if os.path.normcase(p) == os.path.normcase(os.path.abspath(cur)):
        return True, p
    try:
        os.makedirs(p, exist_ok=True)
        probe = os.path.join(p, ".bg_write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
    except OSError as e:
        return False, f"这个目录不能写：{str(e)[:60]}"
    try:
        # 指针文件不跟着搬：它只认默认目录那一份，搬过去反而会让人误会
        shutil.copytree(cur, p, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(ROOT_POINTER))
    except Exception:
        pass                      # 拷不全也认了：至少 config 会在下面再补一次
    _keep_newer_config(cur, p)
    try:
        ptr = _root_pointer()
        tmp = ptr + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(p)
        os.replace(tmp, ptr)
    except OSError as e:
        return False, f"记录新路径失败：{str(e)[:60]}"
    _retarget(p)
    return True, p


def reset_data_root() -> tuple[bool, str]:
    """恢复默认数据目录（把自定义目录里的东西搬回来，然后抹掉指针）"""
    default = default_data_dir()
    cur = app_data_dir()
    try:
        ptr = _root_pointer()
        if os.path.isfile(ptr):
            os.remove(ptr)
    except OSError as e:
        return False, f"清除记录失败：{str(e)[:60]}"
    if os.path.normcase(cur) != os.path.normcase(os.path.abspath(default)):
        try:
            shutil.copytree(cur, default, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(ROOT_POINTER))
        except Exception:
            pass
        _keep_newer_config(cur, default)
    _retarget(default)
    return True, default


def _retarget(d: str):
    """让当前进程立刻改用新目录（不重启也能继续存设置）"""
    global CONFIG
    try:
        if CONFIG is not None:
            CONFIG.path = os.path.join(d, "config.json")
            CONFIG.load()
            CONFIG.save()
    except Exception:
        pass


def icon_cache_dir() -> str:
    d = os.path.join(app_data_dir(), "cache")
    os.makedirs(d, exist_ok=True)
    return d


class Config:
    def __init__(self):
        self.path = os.path.join(app_data_dir(), "config.json")
        # 第九十八轮：批量写盘用（见 batch()）。`set()` 每调一次就把整份 json
        # 重写一遍，实测 6.1ms；设置页「保存」一次要写 30 来个键 = 180ms。
        self._defer = 0
        self._dirty = False
        self.data = {
            "launcher_path": "",            # 官方启动器 exe（可选手动指定）
            "game_dir": "",                 # 应用路径（纯属于本启动器的 .minecraft）
            # ---- 第四十五轮：应用路径 / 官方路径 分开 ----
            "install_dir": "",              # 安装目录（Osmanthus flower），空则按 exe 位置推
            "official_game_dir": "",        # 官方路径（默认 <安装目录>\official_assets\.minecraft）
            # 第四十六轮：空串 = 跟随「独立启动」开关（关=官方，开=应用），
            # 只有玩家亲手点过官方/应用胶囊才记明确值。
            "download_target": "",          # 明确选过的目标：app / official / 空
            "paths_split_v45": False,       # 一次性迁移做过没
            "close_confirm": False,         # 关闭前弹窗询问（默认关：点 × 直接退出）
            "session": None,                # {"name","uuid","mode":"online|offline"}
            "installed": {},                # project_id -> 安装信息
            "platform": "Modrinth",
            "window": None,                 # {"x","y","w","h","max"}
            "memory_mb": 0,                 # 运行内存（0 = 不写入 javaArgs，交给启动器默认）
            "memory_auto_min": True,        # 同时设置 -Xms 为内存的一半
            "mirror_mode": "auto",          # 下载源：auto / always / off（旧键，保留兼容）
            "mirror_sources": {"default": "auto", "items": {}},   # 下载源逐项设置
            # 独立启动：开关 + 当前启动方式（official / standalone）+ 已选的游戏
            "standalone_launch": False,
            "launch_mode": "official",
            "launch_target": None,
            # 导入其它启动器：默认「只登记、不搬」（添加已有文件夹后可直接运行），
            # 打开这个开关才会加完立刻搬进官方启动器
            "auto_import": False,
            # 自动补全（第五十四轮）：默认开 —— 装完整合包照实例里的
            # manifest.json 核对一遍，清单上写着却没下下来的文件自动重新拉
            "auto_complete": True,
            # 自动中文（第五十五轮）：每次启动前把 options.txt 的 lang 写成 zh_cn
            "auto_chinese": True,
            # 第一百二十四轮：启动器自更新。默认开——关程序后静默更新，下次打开即新版。
            "auto_update": True,
            # 仓库地址（空 = 用 update.py 默认 Yifengjin1/Bagui-Launcher；可改）
            "update_repo": "",
            # 上次检查到的最新版本（用于「有新版本」提示，不参与逻辑判断）
            "update_last_seen": "",
            # 设置页的「小字注解」开关（第五十五轮）：默认隐藏，点右上角「?」才显示
            "show_tips": False,
            # 内置文件管理器（第六十轮）：选目录 / 选文件时用软件自己画的窗口，
            # 还是交给 Windows 的系统对话框。默认开 —— 内置的样式统一、拖不出主窗。
            "builtin_fs": True,
            "added_dirs": {},               # 零拷贝登记过的目录（可直接运行）
            "added_removed": [],            # 玩家在「已导入」里主动移除过的目录
            # 「已导入」里的收放式文件夹：id -> {name, path, time}
            # 每条 added_dirs 用 "group" 记自己属于哪个文件夹（空 = 不分类，平铺）
            "added_groups": {},
            # 第一百零五轮：游戏管理 / 启动选择里的「游戏版本」「整合包」分类。
            # id -> {name, kind: version|pack, members: [game_key...], time}
            # 与 added_groups 刻意分开：那边是「已导入」外部目录的分组，键语义被
            # 第 99/101 轮的重绑与自愈认死了。members 是绝对路径，搬目录时由
            # paths._rebase_after_move 一起重绑。
            "game_categories": {},
            # 启动切输入法：每次启动游戏前把系统「新窗口默认输入语言」设成 en-US，
            # 免得中文输入法吃掉 WASD / 聊天框蹦候选词（不写注册表，重启即恢复）
            "ime_english": True,
            # 卡片隐藏：主页大卡片在鼠标停歇 N 秒后自己隐去（顶部状态栏保持不动）；
            # 默认关闭，开启后默认 3 秒
            "card_auto_hide": False,
            "card_hide_secs": 3,
            # 背景幻灯片：默认开启，每 slide_secs 秒淡入淡出换一张背景图；
            # 关掉时用 bg_pick 指定的那一张。两种情况下天空都照现实时间变化。
            "slideshow": True,
            "slide_secs": 15,
            "bg_pick": 0,
            # 下载引擎：线程数 = **总连接预算**（对齐 PCL/PCLCE 的语义，不是
            # 「同时下几个文件」）。文件多时铺满，文件少时给单文件多切几段，
            # 收尾阶段也能吃满带宽。0 表示自动（按 64 处理）。
            "dl_threads": 64,
            # 限速：KB/s，0 = 不限。全局总带宽（不是每条连接），令牌桶实现。
            "dl_speed_kb": 0,
            # ---- 第一百零四轮：启动项 / 运行内存 / 整合包 / 镜像源 ----
            # 默认优化 JVM 参数串默认**关**：这是可关闭的开关，不硬加进
            # 每个玩家的启动命令（改行为要玩家自己点开）。
            "jvm_opt_default": False,
            "jvm_args_global": "", "game_args_global": "",
            "pre_launch_global": "", "pre_launch_wait": True,
            "window_width": 854, "window_height": 480,
            "window_mode": "windowed", "window_title": "",
            "javaw": False, "server_to_enter": "",
            "launcher_visibility": "keep", "gpu_preference": "auto",
            "ip_stack": "auto",
            "ram_lock_memory": False, "ram_init_mb": 0,
            "ram_xmn": True, "ram_32_cap": True,
            # env.client=optional 默认下载（A9 明确默认 + 记账）
            "pack_optional_client": True,
            # MCIMirror 代理与错峰竞速都是**可选非默认**策略（D1/D2）
            "mirror_mcim": False, "mirror_race": False,
        }
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for k, v in saved.items():
                self.data[k] = v
        except Exception:
            pass

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ---- 便捷存取 ----
    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        if self._defer:
            self._dirty = True
            return
        self.save()

    @contextlib.contextmanager
    def batch(self):
        """把一串 `set()` 合并成**一次**落盘。

        第九十八轮：`set()` 每次都整份 json 重写（实测 6.1ms/次），设置页
        「保存」要写 30 来个键 = 180ms，玩家点完保存返回主界面就卡这一下。
        `with CONFIG.batch():` 里的 set 只改内存，出块时写一次。
        """
        self._defer += 1
        try:
            yield self
        finally:
            self._defer -= 1
            if not self._defer and self._dirty:
                self._dirty = False
                self.save()

    # ---- 开源版：不提供第三方 API Key，保留空骨架以兼容调用方 ----
    def cf_key(self) -> str:
        """开源版不提供第三方 API Key，恒为空串。"""
        return ""

    def cf_key_custom(self) -> bool:
        return False

    def session(self):
        s = self.data.get("session")
        return s if isinstance(s, dict) else None

    def mark_installed(self, project_id: str, info: dict):
        self.data.setdefault("installed", {})[project_id] = dict(
            info, time=datetime.now().isoformat(timespec="seconds"))
        self.save()

    def forget_installed(self, project_id: str) -> bool:
        """忘掉一条安装记录（内容已在磁盘上消失时用）"""
        rec = self.data.get("installed") or {}
        if project_id in rec:
            rec.pop(project_id, None)
            self.save()
            return True
        return False

    def installed_record(self, project_id: str):
        rec = (self.data.get("installed") or {}).get(project_id)
        return rec if isinstance(rec, dict) else None

    def is_installed(self, project_id: str, scope=None) -> bool:
        """是否**确实**装着：以磁盘上的文件/目录为准。

        玩家在资源管理器里把整合包目录或模组文件删掉后，这里会自动作废记录，
        下载按钮随即回到「下载」状态（此前只查记录，删了也一直显示已安装）。

        `scope`（第六十一轮）= 当前所选游戏的实例目录。给了就**只认这个游戏里的**：
        同一个模组装在 A 实例里、现在选的是 B 实例 → 这里是 False（按钮回到「下载」），
        切回 A 又变回「已安装」。不给（None）= 老行为，全局一条记录。
        整合包不按游戏算 —— 它自己就是一台实例，装没装只看目录在不在。
        """
        pid = str(project_id or "")
        if not pid:
            return False
        rec = self.installed_record(pid)
        if rec is None:
            return False
        if not self._record_alive(rec):
            self.forget_installed(pid)
            return False
        kind = str(rec.get("kind") or "")
        # 老记录可能没写 kind，认不出是什么内容就别硬按游戏筛（宁可显示已安装）
        if not scope or kind == "modpack" or kind not in self._CONTENT_SUB:
            return True
        return self._record_in_scope(rec, kind, str(scope))

    # 内容类型 → 文件夹名（跟 installer.CONTENT_DIRS 保持一致；这里不能 import
    # installer，config 是它的上游，反着引会成环）
    _CONTENT_SUB = {"mod": "mods", "shader": "shaderpacks",
                    "resourcepack": "resourcepacks", "datapack": "datapacks"}

    def _scope_dirs(self, kind, scope):
        """这个实例下，某种内容可能躺着的文件夹（**只算不建**）"""
        base = os.path.normpath(scope)
        out = []
        sub = self._CONTENT_SUB.get(kind)
        if sub:
            out.append(os.path.join(base, sub))
        if kind == "datapack":
            # 数据包按「最近玩过的存档」放，也可能被我们放在 <实例>/datapacks；
            # 判定时把该实例所有存档的 datapacks 都算进来，别漏。
            saves = os.path.join(base, "saves")
            try:
                for n in os.listdir(saves):
                    p = os.path.join(saves, n, "datapacks")
                    if os.path.isdir(p):
                        out.append(p)
            except OSError:
                pass
        return out

    def _record_in_scope(self, rec, kind, scope):
        dirs = [os.path.normcase(os.path.normpath(d)) for d in
                self._scope_dirs(kind, scope)]
        if not dirs:
            return False
        name = str(rec.get("name") or "").strip()
        for d in dirs:
            if name and os.path.exists(os.path.join(d, name)):
                return True
        path = str(rec.get("path") or "").strip()
        if path:
            real = os.path.normcase(os.path.normpath(path))
            for d in dirs:
                if real == d or real.startswith(d + os.sep):
                    return True
        return False

    def _record_alive(self, rec: dict) -> bool:
        path = str(rec.get("path") or "").strip()
        if path:
            return os.path.exists(path)
        # 老记录没存路径：按名字在游戏目录里找（整合包=目录，模组/光影/材质=文件）
        name = str(rec.get("name") or "").strip()
        if not name:
            return True                      # 无法判定，保持原状，避免误清
        gd = self.data.get("game_dir") or os.path.expandvars(r"%APPDATA%\.minecraft")
        cands = [os.path.join(gd, name)]
        for sub in ("mods", "shaderpacks", "resourcepacks"):
            cands.append(os.path.join(gd, sub, name))
        return any(os.path.exists(c) for c in cands)

    def profile_id(self):
        return uuid.uuid4().hex[:12]


CONFIG = Config()
