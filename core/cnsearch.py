# -*- coding: utf-8 -*-
"""中文搜索支持：把中文关键词翻成 Modrinth 认的英文查询。

为什么要有这一层：两个站都是**英文全文检索**，直接送「机械动力」只有一个结果 ——
0 条，页面上就是「没有找到相关内容」，玩家还以为整页坏了。

翻译分三步（都在 `translate()` 里）：
  1. **贪心最长匹配**：中文没有空格，用户常写「机械动力整合包」，得从长到短试着
     啃掉词头，不能只按空格切；
  2. **类型词丢弃**：在「整合包」页搜「优化 整合包」，`整合包→modpack` 只是把
     facets 已经限定过的东西又说一遍，留着反而挤掉了真正的关键词；
  3. **未命中的 ASCII 段照留**（「优化 mod」→ `optimization mod`），纯中文未命中段
     丢掉 —— 英文站不可能靠一段没收录的中文命中。

翻不出来不算失败：`candidates()` 的**末位永远是原词**，界面会拿它再试一轮，
站上确实有中文标题的项目（国内作者投稿的那批）。
"""
import re

# 汉字 + 假名 + 全角片假名：判断「这句需不需要翻译」
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff66-\uff9f]")
_ASCII_WORD = re.compile(r"[A-Za-z]")

# 目标类型的中文叫法。搜这一页时，这些词直接丢掉（同义反复）
_TYPE_WORD = {
    "整合包": "modpack", "模组": "mod", "模组包": "modpack",
    "数据包": "datapack", "光影": "shader", "着色器": "shader",
    "材质包": "resourcepack", "材质": "resourcepack", "资源包": "resourcepack",
}

# ---------------------------------------------------------------- 词表
# 键 = 中文（可多个别名指向同一译名），值 = 送给搜索接口的英文查询。
# 只收录「玩家真会用中文搜」的东西；宁缺毋滥，乱译比不译更糟（会把结果带偏）。
_ALIAS = {
    # ---- 分类 / 通用词 ----
    "优化": "optimization", "性能": "performance", "帧数": "fps",
    "实用": "utility", "工具": "utility", "科技": "technology",
    "魔法": "magic", "冒险": "adventure", "世界生成": "worldgen",
    "地形生成": "worldgen", "地形": "worldgen", "多人": "multiplayer",
    "联机": "multiplayer", "服务器": "server", "任务": "quests",
    "存储": "storage", "储存": "storage", "食物": "food",
    "农业": "farming", "农耕": "farming", "种田": "farming",
    "装备": "equipment", "战斗": "combat", "经济": "economy",
    "玩法": "gameplay", "交通": "transportation", "装饰": "decoration",
    "美化": "decoration", "生物": "mobs", "怪物": "mobs",
    "探索": "exploration", "加工": "processing", "前置": "library",
    "前置库": "library", "依赖": "library", "大杂烩": "kitchen sink",
    "小地图": "minimap", "地图": "map", "背包": "backpack",
    "合成": "crafting", "配方": "recipe", "管道": "pipe",
    "自动化": "automation", "能源": "energy", "电力": "energy",
    "机械": "machine", "机器": "machine", "铁路": "railway",
    "火车": "train", "载具": "vehicle", "飞机": "aircraft",
    "汽车": "car", "船": "boat", "钓鱼": "fishing",
    "天气": "weather", "季节": "seasons", "温度": "temperature",
    "口渴": "thirst", "饥饿": "hunger", "附魔": "enchantment",
    "技能": "skills", "等级": "leveling", "属性": "attributes",
    "成就": "advancement", "进度": "advancement", "战利品": "loot",
    "刷怪": "spawn", "刷怪笼": "spawner", "传送门": "portal",
    "家具": "furniture", "建筑": "building", "武器": "weapon",
    "护甲": "armor", "盾牌": "shield", "枪械": "gun",
    "地牢": "dungeon", "迷宫": "maze", "村庄": "village",
    "城市": "city", "遗迹": "ruins", "宠物": "pet",
    "龙": "dragon", "精灵": "fairy", "音效": "audio",
    "界面": "gui", "字体": "font", "光照": "lighting",
    # ---- 内容类型本身（在「别的页」搜它们才有意义；对本页会被丢掉）----
    "整合包": "modpack", "模组": "mod", "模组包": "modpack",
    "数据包": "datapack", "光影": "shader", "着色器": "shader",
    "材质包": "resource pack", "材质": "resource pack",
    "资源包": "resource pack",
    # ---- 画风 / 风格（光影、材质包最常这么搜）----
    "真实": "realistic", "写实": "realistic", "超真实": "realistic",
    "卡通": "cartoon", "清新": "clean", "极简": "minimal",
    "简约": "simplistic", "中世纪": "medieval", "未来": "futuristic",
    "赛博": "cyberpunk", "东方": "oriental", "日式": "japanese",
    "和风": "japanese", "二次元": "anime", "可爱": "cute",
    "像素": "pixel", "高清": "hd", "恐怖": "horror",
    "原版": "vanilla", "原版风": "vanilla-like", "奇幻": "fantasy",
    # ---- 知名模组（中文名 → 项目名，这才是「用中文搜整合包」的关键）----
    "机械动力": "create", "创造": "create",
    "应用能源": "applied energistics", "应用能源2": "applied energistics",
    "工业时代": "industrial craft", "工业": "industrial",
    "匠魂": "tinkers construct", "植物魔法": "botania",
    "林业": "forestry", "热力膨胀": "thermal expansion",
    "热力": "thermal", "沉浸工程": "immersive engineering",
    "沉浸": "immersive", "通用机械": "mekanism",
    "龙之研究": "draconic evolution", "神秘时代": "thaumcraft",
    "血魔法": "blood magic", "星辉魔法": "astral sorcery",
    "额外实用设备": "extra utilities", "精妙背包": "sophisticated backpacks",
    "精妙核心": "sophisticated core", "旅行者背包": "travelers backpack",
    "精妙存储": "functional storage", "功能存储": "functional storage",
    "储物抽屉": "storage drawers", "抽屉": "drawers",
    "铁箱子": "iron chests", "末影存储": "ender storage",
    "建筑手杖": "construction wand", "建筑棒": "building gadget",
    "快速建造": "building gadgets",
    "史诗战斗": "epic fight", "更好的战斗": "better combat",
    "农夫乐事": "farmers delight", "潘马斯": "pam harvestcraft",
    "咖啡": "coffee", "蜜蜂": "productive bees", "养蜂": "productive bees",
    "超多生物群系": "biomes o plenty", "生物群系": "biomes",
    "冰与火": "ice and fire", "虚无世界": "nevermine",
    "深渊国度": "abyssalcraft", "天境": "aether",
    "暮色森林": "twilight forest", "交错次元": "betweenlands",
    "宁静季节": "serene seasons", "季节模组": "serene seasons",
    "真实季节": "ecliptic seasons", "口渴模组": "thirst was taken",
    "寒冷": "cold sweat", "温度模组": "cold sweat",
    "旅程地图": "journeymap", "旅行地图": "journeymap",
    "苹果皮": "apple skin", "耐久显示": "durability",
    "任务书": "ftb quests", "指南书": "guide book",
    "百科全书": "encyclopedia", "教程": "tutorial",
    "老鼠": "rats", "传说": "legend",
    "箱子": "chest", "熔炉": "furnace", "酿造": "brewing",
    # ---- 光影（玩家基本只用中文名/中文叫法）----
    "补充光影": "complementary", "补充": "complementary",
    "光追": "path tracing", "路径追踪": "path tracing",
    "体积云": "volumetric", "体积光": "volumetric",
    "雾气": "fog", "夜景": "night", "水面反射": "water reflection",
    "极光": "aurora", "卡普": "chocapic", "西尔杜": "sildur",
    "卡帕": "kappa", "超真实光影": "realistic shader",
    # ---- 材质包 ----
    "忠实": "faithful", "写实材质": "patrix", "真实材质": "patrix",
    "高清材质": "hd texture", "简约材质": "simplistic",
    "像素风": "pixel", "圆润": "round",
}

# 长词优先：贪心扫描时先试更长的键，否则「机械动力」会被「机械」先啃掉
_KEYS = sorted(_ALIAS, key=len, reverse=True)


def has_chinese(text) -> bool:
    """这句里有没有中日文字（纯英文/数字不用翻译，原样发更快也更准）"""
    return bool(_CJK.search(str(text or "")))


def _norm(text) -> str:
    """全角→半角、去零宽、压空白。玩家从网页复制粘贴常带全角空格/顿号。"""
    s = str(text or "")
    out = []
    for ch in s:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:          # ！-～ → !-~
            ch = chr(o - 0xFEE0)
        elif ch == "\u3000":               # 全角空格
            ch = " "
        elif ch in "\u200b\u200c\u200d\ufeff":
            continue
        out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _scan(ptype: str, text: str):
    """贪心最长匹配扫描，返回 `(英文词列表, 没认出来的中文残渣)`。

    每步从当前位置试最长的键，啃掉就继续；啃不掉就把这个字并进「未命中段」。
    未命中段里只要还有英文字母就留着（玩家常写「优化 mod」）；纯中文残渣
    单独返回 —— 调用方靠它区分「整句都是类型词」和「压根没收录」：
    前者应该搜全部，后者只能拿原词去碰运气。
    """
    s = _norm(text)
    terms, junk, lost = [], [], []

    def flush():
        seg = "".join(junk).strip()
        junk.clear()
        # 只留「有字母」的段：`尘封古纪 1.9` 里的 1.9 是版本号不是关键词，
        # 拿它去搜只会把一堆 1.9 的东西倒出来（版本该用下拉筛，不是搜索词）。
        if seg and _ASCII_WORD.search(seg):
            terms.append(seg)
        elif seg:
            lost.append(seg)

    i = 0
    while i < len(s):
        for k in _KEYS:
            if s.startswith(k, i):
                flush()
                # 类型词：这一页本来就是这种东西，留着等于没说
                if _TYPE_WORD.get(k) != ptype:
                    terms.append(_ALIAS[k])
                i += len(k)
                break
        else:
            junk.append(s[i])
            i += 1
    flush()

    seen, out = set(), []
    for t in terms:
        for w in t.split():
            lw = w.lower()
            if lw and lw not in seen:
                seen.add(lw)
                out.append(w)
    return out, "".join(lost)


def translate(ptype: str, text: str) -> str:
    """把一句中文翻成英文查询（翻不出/全是类型词都返回空串）"""
    s = _norm(text)
    if not s or not has_chinese(s):
        return s
    terms, _lost = _scan(ptype, s)
    return " ".join(terms)


def candidates(ptype: str, text: str) -> list:
    """有序列候选查询：第一个最可能命中，**末位一定是原词**（回退用）。

    `do_search` 拿它逐个试，前一个 0 条才发下一个。
    整句都是「这一页本来就有的东西」（在光影页搜「光影」）时，第一位给**空查询**
    —— 那正是「看全部光影」的意思，比拿「光影」两个字去撞英文接口合理得多。
    """
    raw = _norm(text)
    if not raw:
        return []
    if not has_chinese(raw):
        return [raw]
    terms, lost = _scan(ptype, raw)
    q = " ".join(terms)
    out = []
    if q:
        if q.lower() != raw.lower():
            out.append(q)
    elif not lost:
        out.append("")                 # 全被类型词吃掉 → 该类型的全部
    if raw not in out:
        out.append(raw)
    return out


def explain(ptype: str, text: str) -> str:
    """给界面用：真翻译了才返回译名，否则空串（别在没翻译时也说一句废话）"""
    raw = _norm(text)
    if not raw or not has_chinese(raw):
        return ""
    q = translate(ptype, raw)
    return q if q and q.lower() != raw.lower() else ""
