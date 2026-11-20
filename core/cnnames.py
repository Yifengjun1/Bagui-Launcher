# -*- coding: utf-8 -*-
"""内置中文名库：给 Modrinth 的项目配中文名与中文简介。

为什么要有这个：两个站都是英文站，`title` / `description` 全英文。PCL2、HMCL
这些启动器之所以「有中文名」，靠的都是**自己维护的汉化库**（不是接口给的），
我们也得有一份。

匹配顺序：`slug` → `title` → `name`，都先归一化成「只留字母数字的小写串」再查 ——
同一个项目在不同站的写法差得远（`applied-energistics-2` / `appliedenergistics2` /
`Applied Energistics 2`），归一化之后才是同一个 key。

**查不到就如实显示英文**，绝不机翻、不猜 —— 宁可没有中文名，也不能给玩家一个
错名字（装错模组的代价比看不懂英文大得多）。

和 `cnsearch.py` 的分工：那个负责「中文 → 英文查询词」（搜索用），
这个负责「英文项目 → 中文名/简介」（显示用）。两份表刻意分开，别互相 import。
"""
import re

# key = 归一化后的 slug / 英文标题（小写、只留字母数字）
# value = (中文名, 一句话中文简介)
DATA = {
    # ---------------- 性能优化 ----------------
    "sodium": ("钠", "重写渲染管线，帧数大幅提升"),
    "lithium": ("锂", "实体、AI、物理的通用性能优化"),
    "phosphor": ("磷", "光照引擎优化，减少卡顿"),
    "starlight": ("星光", "重写光照引擎，区块加载更快"),
    "iris": ("虹膜", "光影加载器，Fabric 上的 OptiFine 替代"),
    "oculus": ("眼", "NeoForge 版 Iris 光影加载器"),
    "optifine": ("高清修复", "老牌优化与光影支持"),
    "indium": ("铟", "让 Fabric 渲染模组兼容钠"),
    "entityculling": ("实体剔除", "看不见的实体不渲染，省大量帧"),
    "ferritecore": ("铁氧体核心", "压缩模型数据，显著降内存"),
    "krypton": ("氪", "网络协议栈优化，联机更顺"),
    "modernfix": ("现代修复", "启动更快、内存更省、修一堆原版毛病"),
    "canary": ("金丝雀", "区块生成与 AI 性能优化"),
    "saturn": ("土星", "内存压缩，降低占用"),
    "memoryleakfix": ("内存泄漏修复", "堵住原版与模组的内存泄漏"),
    "c2me": ("并发区块引擎", "多线程生成区块"),
    "nvidium": ("英伟达渲染", "N 卡专用的钠渲染后端"),
    "embeddium": ("嵌入", "NeoForge 版钠渲染优化"),
    "radium": ("镭", "NeoForge 版锂性能优化"),
    "rubidium": ("铷", "Forge 版钠渲染优化"),
    "sodiumextra": ("钠：扩展", "给钠补上动画、粒子等开关"),
    "reeses-sodium-options": ("钠设置增强", "把钠的设置页做成更好用的滚轮列表"),
    "flywheel": ("飞轮", "机械动力的渲染优化库"),
    "betterfps": ("更好的帧数", "算法级优化，提升帧率"),
    "dynamicfps": ("动态帧率", "窗口在后台时自动降帧省电"),
    "smoothboot": ("平滑启动", "优化启动阶段的线程调度"),
    "fastload": ("快速加载", "跳过部分加载等待"),
    "chunky": ("区块预生成", "提前生成区块，减少探索卡顿"),
    "bobby": ("区块缓存", "把服务器区块缓存到本地，渲染距离拉满"),
    "distanthorizons": ("远处的地平线", "超远渲染距离的 LOD 地形"),
    "spark": ("火花", "性能分析器，找卡顿元凶"),
    "observable": ("可观测", "服务端卡顿定位工具"),
    "clearlag": ("清理掉落物", "定时清理掉落物与多余实体"),

    # ---------------- 科技 / 自动化 ----------------
    "create": ("机械动力", "齿轮、传送带与自动化机械"),
    "createaddition": ("机械动力：电力", "给机械动力加上电与充电"),
    "createintegratedfarming": ("机械动力：集成农业", "用机械动力做全自动农场"),
    "createsteamnrails": ("机械动力：蒸汽与铁路", "蒸汽机车与列车系统"),
    "createconfectionery": ("机械动力：糖果", "巧克力与甜点生产线"),
    "createdeco": ("机械动力：装饰", "成套的建筑装饰方块"),
    "createcraftsadditions": ("机械动力：更多配方", "补齐常用合成"),
    "createenchantmentindustry": ("机械动力：附魔工业", "批量附魔与经验处理"),
    "createoreexcavation": ("机械动力：矿脉开采", "自动采矿机"),
    "createnewage": ("机械动力：新纪元", "大型工业机器"),
    "appliedenergistics2": ("应用能源2", "数字存储与自动合成网络"),
    "ae2": ("应用能源2", "数字存储与自动合成网络"),
    "ae2wtlib": ("AE2 无线终端", "把终端揣在身上用"),
    "refinedstorage": ("精致存储", "网格化存储与自动合成"),
    "industrialcraft": ("工业时代2", "电力、机器与核能的老牌科技"),
    "ic2": ("工业时代2", "电力、机器与核能"),
    "mekanism": ("通用机械", "矿石五倍处理与多方块机器"),
    "thermalexpansion": ("热力膨胀", "机器、管道与能源"),
    "thermal": ("热力系列", "热力膨胀的机器与物流"),
    "immersiveengineering": ("沉浸工程", "重型机械与高压电"),
    "enderio": ("末影接口", "导管物流与机器"),
    "gregtech": ("格雷科技", "硬核长篇工业化"),
    "modernindustrialization": ("现代工业化", "从蒸汽到核聚变的进阶产线"),
    "immersivepetroleum": ("沉浸石油", "原油开采与炼化"),
    "pneumaticcraft": ("气动工艺", "压缩空气驱动的机器"),
    "rftools": ("RF 工具", "维度、建造机与护盾"),
    "powah": ("能量", "发电与储能设备"),
    "extremereactors": ("极限反应堆", "大型反应堆与涡轮"),
    "nuclearcraft": ("核电工艺", "核裂变与核聚变"),
    "computercraft": ("电脑工坊", "用 Lua 写程序控制机器"),
    "opencomputers": ("开放式电脑", "可编程电脑与机器人"),
    "integrateddynamics": ("集成动力", "逻辑线与自动化逻辑门"),
    "botania": ("植物魔法", "用花朵产能的魔法科技"),
    "bloodmagic": ("血魔法", "以血为燃料的仪式魔法"),
    "thaumcraft": ("神秘时代", "研究、要素与法杖"),
    "astralsorcery": ("星辉魔法", "观星与星座仪式"),
    "arsnouveau": ("新生魔艺", "符文法术与魔宠"),
    "occultism": ("神秘学", "召唤灵体替你干活"),
    "draconicevolution": ("龙之研究", "混沌龙与超强装备"),
    "avaritia": ("无尽贪婪", "极限合成与无限装备"),
    "projecte": ("等价交换", "物质转换与 EMC"),
    "extrautils2": ("额外实用设备2", "一系列便利机器"),
    "mekanismgenerators": ("通用机械：发电", "配套的发电机组"),
    "mekanismtools": ("通用机械：工具", "锇制工具与装备"),
    "ae2things": ("AE2 扩展", "给应用能源补上小工具"),

    # ---------------- 存储 / 物流 ----------------
    "sophisticatedbackpacks": ("精妙背包", "可升级的随身背包"),
    "sophisticatedstorage": ("精妙存储", "可升级的箱子与桶"),
    "sophisticatedcore": ("精妙核心", "精妙系列的前置库"),
    "functionalstorage": ("功能存储", "带抽屉与升级的存储"),
    "storagedrawers": ("储物抽屉", "一格装一堆的抽屉"),
    "ironchest": ("铁箱子", "更大容量的箱子"),
    "enderstorage": ("末影存储", "跨维度共享的箱子与袋子"),
    "colossalchests": ("巨型箱子", "多方块巨型仓储"),
    "tombstone": ("墓碑", "死亡后原地留下墓碑保住物品"),
    "corpse": ("尸体", "死亡后把掉落物装进尸体"),
    "gravestone": ("墓碑模组", "死亡不掉落"),
    "waystones": ("路石", "可传送的路标石"),
    "comforts": ("睡袋", "白天也能睡，不重置重生点"),

    # ---------------- 冒险 / 世界生成 ----------------
    "biomesoplenty": ("超多生物群系", "几十种全新群系与植物"),
    "terralith": ("泰拉瑞斯地形", "用原版方块做大地形"),
    "tectonic": ("构造地形", "更夸张的山脉与河谷"),
    "ohthebiomesyoullgo": ("你将去的生物群系", "大量群系与方块"),
    "regionsunexplored": ("未探索区域", "分区域的群系与地貌"),
    "aether": ("天境", "天空维度的冒险"),
    "twilightforest": ("暮色森林", "森林维度与 Boss 连战"),
    "thebetweenlands": ("交错次元", "沼泽维度的生存挑战"),
    "iceandfire": ("冰与火之歌", "驯龙与怪物"),
    "alexsmobs": ("亚历克斯的生物", "几十种有行为的生物"),
    "mowziesmobs": ("更多 Boss 生物", "带机制的 Boss 战"),
    "cataclysm": ("灾变", "高难度 Boss 与地牢"),
    "alexscaves": ("亚历克斯的洞穴", "多个主题洞穴维度"),
    "deeperdarker": ("更深的黑暗", "深暗之域的扩展维度"),
    "when-dungeons-arise": ("地牢崛起", "随机生成的大型地牢"),
    "yungs-better-dungeons": ("更好的地牢", "重做原版地牢结构"),
    "yungs-better-mineshafts": ("更好的矿井", "重做废弃矿井"),
    "structory": ("结构扩充", "给世界加更多小结构"),
    "repurposedstructures": ("复用结构", "把原版结构搬到别的群系"),
    "mowzies-mobs": ("更多生物", "带机制的 Boss"),

    # ---------------- 农业 / 食物 / 生存 ----------------
    "farmersdelight": ("农夫乐事", "料理、刀具与厨具"),
    "pamsharvestcraft": ("潘马斯农场", "上百种作物与食物"),
    "croptopia": ("作物盛景", "大量作物与料理"),
    "sereneseasons": ("宁静季节", "四季更替，作物分季节"),
    "eclipticseasons": ("黄道季节", "四季与气候系统"),
    "coldsweat": ("冷汗", "温度系统，会中暑也会冻伤"),
    "thirstwastaken": ("口渴", "需要喝水，水质有区别"),
    "aquaculture": ("水产养殖", "新鱼类与钓具"),
    "cookingforblockheads": ("厨房大师", "多方块厨房与食谱书"),
    "spiceoflife": ("生活调味料", "重复吃同一种食物会腻"),
    "diet": ("饮食", "营养均衡系统"),

    # ---------------- 实用 / 界面 ----------------
    "jei": ("JEI 物品管理器", "查配方、看用途"),
    "rei": ("REI 物品管理器", "配方查询与合成计划"),
    "emi": ("EMI 物品管理器", "更现代的配方查询"),
    "roughlyenoughitems": ("REI 物品管理器", "配方查询"),
    "jade": ("玉", "准星指向方块时显示信息"),
    "wthit": ("WTHIT", "方块与实体信息显示"),
    "hwyla": ("HWYLA", "方块信息显示（Jade 的前身）"),
    "appleskin": ("苹果皮", "显示饱食度与饱和度"),
    "journeymap": ("旅程地图", "小地图与全屏地图"),
    "xaerosminimap": ("Xaero 小地图", "轻量小地图"),
    "xaerosworldmap": ("Xaero 世界地图", "全屏世界地图"),
    "curios": ("饰品栏", "额外的装备槽位"),
    "trinkets": ("饰品", "Fabric 版饰品栏"),
    "patchouli": ("帕秋莉手册", "游戏内可翻阅的说明书"),
    "ftbquests": ("FTB 任务", "章节式任务与奖励"),
    "ftbteams": ("FTB 队伍", "任务模组的组队支持"),
    "ftblibrary": ("FTB 前置库", "FTB 系列的前置"),
    "ftbultimine": ("FTB 连锁挖矿", "一次挖一片同种方块"),
    "betteradvancements": ("更好的进度", "进度界面加分类与搜索"),
    "controlling": ("按键冲突查询", "按冲突筛选按键绑定"),
    "toastcontrol": ("提示控制", "屏蔽多余的成就弹窗"),
    "mousetweaks": ("鼠标微调", "拖拽分堆更顺手"),
    "inventoryprofilesnext": ("物品整理", "一键整理背包与箱子"),
    "inventorysorter": ("背包排序", "按类型排序"),
    "craftingtweaks": ("合成调整", "一键补齐/清空合成格"),
    "fastworkbench": ("快速工作台", "记住上次配方，少刷新"),
    "fastfurnace": ("快速熔炉", "熔炉逻辑优化"),
    "apotheosis": ("神化", "附魔、词缀与刷怪笼改造"),
    "passiveskilltree": ("被动技能树", "升级点天赋"),
    "pmmo": ("PMMO 技能", "挖矿、战斗等分项等级"),
    "levelz": ("等级限制", "按等级解锁内容"),
    "minecolonies": ("我的殖民地", "招募村民建一座城"),
    "structurize": ("建筑蓝图", "殖民地的前置建筑工具"),
    "domumornamentum": ("装饰构件", "殖民地的建筑部件"),
    "easyvillagers": ("简单村民", "把村民装进兜里"),
    "guardvillagers": ("守卫村民", "会反击的村民守卫"),
    "simplevoicechat": ("简单语音", "游戏内 proximity 语音"),
    "corpse-mod": ("尸体", "死亡后掉落物进尸体"),
    "backups": ("自动备份", "定时备份存档"),
    "kubejs": ("KubeJS", "用脚本改配方与内容"),
    "crafttweaker": ("CraftTweaker", "脚本改配方"),
    "bookshelf": ("书架库", "很多模组的前置"),
    "puzzleslib": ("谜题库", "前置库"),
    "architectury": ("建筑 API", "跨加载器的抽象层"),
    "clothconfig": ("布料配置", "统一的模组设置界面"),
    "balm": ("香脂", "前置库"),
    "collective": ("集合库", "前置库"),
    "geckolib": ("壁虎库", "3D 动画实体前置"),
    "citadel": ("城堡库", "亚历克斯系列的前置"),
    "playeranimator": ("玩家动画", "动作与姿势前置"),
    "fabricapi": ("Fabric API", "Fabric 模组的必备前置"),
    "modmenu": ("模组菜单", "在游戏里管理模组与设置"),
    "yacl": ("YACL 配置库", "带界面的配置库"),
    "midnightlib": ("午夜库", "配置前置"),
    "konkrete": ("Konkrete 库", "UI 前置"),
    "iceberg": ("冰山库", "UI 前置"),
    "placebo": ("安慰剂库", "前置库"),
    "attributefix": ("属性修复", "修原版属性上限"),
    "connectivity": ("连接修复", "修登录与超时问题"),
    "fastip": ("快速 IP", "加速服务器解析"),
    "login-protection": ("登录保护", "防同名顶号"),
    "nochatreports": ("关闭聊天举报", "移除聊天举报入口"),
    "cull-leaves": ("树叶剔除", "不渲染看不见的树叶面"),
    "cullparticles": ("粒子剔除", "少画看不见的粒子"),
    "particle-core": ("粒子核心", "性能与自定义粒子"),
    "smooth-swapping": ("平滑切换", "切换物品更顺滑"),
    "wi-zoom": ("平滑缩放", "按住键放大视野"),
    "zoomify": ("缩放", "可配置的望远镜"),
    "ok-zoomer": ("缩放镜头", "动画缩放"),

    # ---------------- 光影包 ----------------
    "complementary": ("补充光影", "原版风味的全面光影"),
    "complementaryreimagined": ("补充光影：重构", "补充光影的画面重构版"),
    "complementaryunbound": ("补充光影：无界", "更自由的补充光影"),
    "bsl": ("BSL 光影", "偏写实、配置友好"),
    "seus": ("SEUS 光影", "经典写实系"),
    "seusrenewed": ("SEUS Renewed", "SEUS 的延续版"),
    "chocapic13": ("巧克力光影", "轻量写实"),
    "sildursvibrant": ("西尔杜鲜艳光影", "色彩浓郁"),
    "kappa": ("Kappa 光影", "柔和自然"),
    "nostalgia": ("怀旧光影", "复刻老版本的味道"),
    "rethinkingvoxels": ("重新思考体素", "高质量体积光"),
    "bliss": ("极乐光影", "写实天空与光照"),
    "makeupultrafast": ("MakeUp 极速光影", "低配也能开"),
    "photon": ("光子光影", "细腻的全局光照"),
    "solas": ("Solas 光影", "偏电影感"),
    "superdupervanilla": ("超棒原版光影", "原版风格强化"),
    "iterationshaders": ("迭代光影", "轻量高帧数"),
    "eclipseshader": ("日蚀光影", "氛围感强"),
    "astralex": ("Astralex 光影", "星空与极光"),
    "voxel": ("体素光影", "方块化的光影风格"),
    "teashaders": ("茶光影", "柔和清新"),

    # ---------------- 材质包 ----------------
    "faithful": ("忠实材质", "原版风格的高清化"),
    "patrix": ("帕特里克斯写实", "极其写实的材质"),
    "bdcraft": ("方块世界", "圆润卡通风"),
    "soartex": ("索尔材质", "干净平涂风"),
    "barebones": ("极简材质", "只留轮廓的清爽风"),
    "staytrue": ("保持原版", "原版风格微调"),
    "bettervanillabuilding": ("更好的原版建筑", "补齐建筑方块变体"),
    "freshanimations": ("生动动画", "让实体动作更自然"),
    "freshmoves": ("生动移动", "玩家动作重做"),
    "betterleaves": ("更好的树叶", "树叶更立体"),
    "motschensbetterleaves": ("更好的树叶", "树叶更立体"),
    "visuality": ("视觉细节", "加粒子与小细节"),
    "lowfire": ("低火焰", "火焰不挡视线"),
    "clearcraft": ("清爽材质", "无噪点、易辨认"),
    "cubictextures": ("立方材质", "立体方块风"),
    "vanillatweaks": ("原版微调", "大量可选的小改动"),
    "defaultdarkmode": ("暗色界面", "把界面改成暗色"),
    "darkmode": ("暗色模式", "暗色 UI"),

    # ---------------- 整合包 ----------------
    "allofthefabric": ("全民 Fabric", "Fabric 全家桶"),
    "alloftheforge": ("全民 Forge", "Forge 全家桶"),
    "bettermc": ("更好的 MC", "优化与体验并重的整合包"),
    "bettermc4": ("更好的 MC 4", "更好的 MC 第四代"),
    "rlcraft": ("真实生存", "硬核生存，处处要命"),
    "dawncraft": ("黎明工艺", "RPG 化的冒险整合包"),
    "vault-hunters": ("金库猎人", "RPG 刷宝整合包"),
    "cobblemon": ("宝可梦", "把宝可梦搬进 MC"),
    "pixelmon": ("宝可梦", "老牌宝可梦模组"),
    "atm9": ("一切皆可 9", "All the Mods 第九代"),
    "atm10": ("一切皆可 10", "All the Mods 第十代"),
    "createaboveandbeyond": ("机械动力：超越", "围绕机械动力的任务包"),
    "ftbskies": ("FTB 空岛", "经典空岛生存"),
    "medievalmc": ("中世纪 MC", "中世纪风格的整合包"),
    "prominence": ("卓越", "画质与玩法兼顾"),
    "deceasedcraft": ("亡者工艺", "丧尸末日生存"),
    "fabulously-optimized": ("极致优化", "只做优化的轻量包"),
}


def _norm(s) -> str:
    """归一化成「只留字母数字的小写串」：applied-energistics-2 → appliedenergistics2"""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


# ---------------------------------------------------------------- 玩家可扩充
# 词库再大也不可能盖全（站上几十万个项目）。留一个口子：数据目录下放一份
# `cn_names.json`（{"sodium": "钠|重写渲染管线…"} 或 {"sodium": ["钠", "…"]}），
# 就能自己补词，不用改代码、不用等更新。格式错的那一条直接忽略，绝不炸启动器。
_extra = None


def _extra_map() -> dict:
    global _extra
    if _extra is not None:
        return _extra
    _extra = {}
    try:
        import json
        import os

        from .config import app_data_dir
        p = os.path.join(app_data_dir(), "cn_names.json")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k, v in raw.items():
                    key = _norm(k)
                    if not key:
                        continue
                    if isinstance(v, (list, tuple)) and v:
                        cn = str(v[0] or "")
                        ds = str(v[1] if len(v) > 1 else "")
                    else:
                        cn, _, ds = str(v or "").partition("|")
                    if cn.strip():
                        _extra[key] = (cn.strip(), ds.strip())
    except Exception:                              # noqa: BLE001
        _extra = {}
    return _extra


def reload_extra():
    """丢掉缓存（设置里改完文件后可以让它立刻生效）"""
    global _extra
    _extra = None
    return _extra_map()


def lookup(hit) -> tuple:
    """查中文名与中文简介，返回 (中文名, 中文简介)；查不到返回 ("", "")。

    依次试 `slug` / `title` / `name`：Modrinth 的搜索命中项带 slug，
    两边都能命中。

    三层来源，前面的永远优先（内置词库是人审过的，比联网结果可信）：

    1. `DATA`    —— 内置词库（这个文件里）
    2. `cn_names.json` —— 玩家自己放的（数据目录）
    3. **BBSMC 缓存** —— 第七十六轮接的。联网补齐后落盘在
       `<数据目录>/bbsmc_cn.json`，查不到就是查不到（**绝不机翻**）。
       这一层是懒 import 的：`bbsmc` 里没有反向依赖，但启动阶段没必要为它多
       加载一个 requests 会话。
    """
    if not isinstance(hit, dict):
        return "", ""
    extra = _extra_map()
    for key in ("slug", "title", "name"):
        v = _norm(hit.get(key))
        if not v:
            continue
        if v in DATA:
            return DATA[v]
        if v in extra:
            return extra[v]
    try:
        from . import bbsmc
        got = bbsmc.lookup(hit)
        if got and got[0]:
            return got
    except Exception:                              # noqa: BLE001
        pass
    return "", ""


def cn_name(hit) -> str:
    """只有中文名（查不到返回空串）"""
    return lookup(hit)[0]


def cn_desc(hit) -> str:
    """只有中文简介（查不到返回空串）"""
    return lookup(hit)[1]


def title_of(hit) -> str:
    """展示用标题：有中文名就用中文名，否则用原名 —— **绝不编**。"""
    return cn_name(hit) or str(hit.get("title") or hit.get("name") or "")


def desc_of(hit) -> str:
    """展示用简介：中文优先，退回接口给的英文简介。"""
    return cn_desc(hit) or str(hit.get("description") or hit.get("summary") or "")


# 文件名里不能出现的字符（Windows 全量）；中文名里的方括号也要去掉，
# 否则 `[机械动力[电力]]xxx.jar` 会被游戏/解压工具当成嵌套括号
_BAD = re.compile(r'[\\/:*?"<>|\[\]\r\n\t]')


def prefixed_name(hit, filename: str) -> str:
    """把文件名改成 `[中文名]原名`（查不到中文名就原样返回）。

    模组/数据包的 jar 文件名不影响加载（加载器认的是 jar 里的 modid），
    所以加前缀是安全的；光影/材质包不在这条路上（它们的文件名被自身内容引用）。
    """
    name = str(filename or "")
    cn = cn_name(hit)
    if not cn or not name:
        return name
    safe = _BAD.sub("_", cn).strip(" .")[:32]
    if not safe:
        return name
    return f"[{safe}]{name}"
