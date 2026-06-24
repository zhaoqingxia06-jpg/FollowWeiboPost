#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""交互式微博抓取 Web 服务

复用 weibo_spider 中的 IndexParser / PageParser，提供两种抓取模式：
- 按时间：指定 since_date / end_date 日期范围
- 按数量：指定最多抓取的微博条数

注意：本服务硬编码为【仅抓取原创微博】，不提供原创/非原创切换。
每条微博附带按 weiboPhoneMonitor.md 规则生成的结构化摘要。
"""
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.request

from flask import Flask, jsonify, request, send_from_directory

# 将项目根目录加入 sys.path，便于导入 weibo_spider 包
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from weibo_spider.parser import IndexParser, PageParser

app = Flask(__name__, static_folder='static', static_url_path='')

CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

# ============ 摘要持久化缓存 ============
# 存储路径：web_app/summary_cache.json
_SUMMARY_CACHE_PATH = os.path.join(BASE_DIR, 'web_app', 'summary_cache.json')
# TTL：7天（秒），超期自动失效防止缓存膨胀
_SUMMARY_TTL = 7 * 24 * 3600
# 内存镜像：{ cache_key: { 'summary': {...}, 'ts': float } }
_summary_cache = {}
# 读写锁，防止并发重复生成
_summary_lock = threading.Lock()
# 正在生成中的 key 集合（防止同一 key 并发重入）
_summary_inflight = set()
_summary_inflight_cond = threading.Condition(_summary_lock)


def _sc_key(weibo_id: str = '', content: str = '') -> str:
    """生成缓存键：优先用 weibo_id，否则用内容 hash"""
    if weibo_id:
        return 'id:' + str(weibo_id)
    h = hashlib.sha256(content.encode('utf-8', errors='replace')).hexdigest()[:16]
    return 'hash:' + h


def _sc_load():
    """从磁盘加载缓存到内存（仅启动时调用一次）"""
    global _summary_cache
    try:
        if os.path.exists(_SUMMARY_CACHE_PATH):
            with open(_SUMMARY_CACHE_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                now = time.time()
                # 过滤已过期条目
                _summary_cache = {k: v for k, v in data.items()
                                  if isinstance(v, dict) and now - v.get('ts', 0) < _SUMMARY_TTL}
    except Exception:
        _summary_cache = {}


def _sc_flush():
    """将内存缓存写入磁盘（持有锁时调用）"""
    try:
        os.makedirs(os.path.dirname(_SUMMARY_CACHE_PATH), exist_ok=True)
        tmp = _SUMMARY_CACHE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_summary_cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _SUMMARY_CACHE_PATH)
    except Exception:
        pass


def sc_get(weibo_id: str = '', content: str = ''):
    """查询摘要缓存，命中返回 summary dict，未命中返回 None"""
    key = _sc_key(weibo_id, content)
    with _summary_lock:
        entry = _summary_cache.get(key)
        if entry and time.time() - entry.get('ts', 0) < _SUMMARY_TTL:
            return entry['summary']
    return None


def sc_set(summary: dict, weibo_id: str = '', content: str = ''):
    """写入摘要缓存并持久化"""
    key = _sc_key(weibo_id, content)
    with _summary_lock:
        _summary_cache[key] = {'summary': summary, 'ts': time.time()}
        _sc_flush()


def sc_delete(weibo_id: str = '', content: str = ''):
    """删除单条缓存（帖子内容更新时调用）"""
    key = _sc_key(weibo_id, content)
    with _summary_lock:
        if key in _summary_cache:
            del _summary_cache[key]
            _sc_flush()


def sc_clear():
    """清空全部摘要缓存"""
    with _summary_lock:
        _summary_cache.clear()
        _sc_flush()


def build_summary_cached(content: str, weibo_id: str = '') -> dict:
    """带缓存的摘要生成：命中直接返回，否则生成后写缓存。
    并发同一 key 时，第二个请求等待第一个完成后直接读缓存。"""
    # 1. 快速读缓存（无锁路径）
    hit = sc_get(weibo_id, content)
    if hit is not None:
        return hit

    key = _sc_key(weibo_id, content)

    with _summary_inflight_cond:
        # 2. 若相同 key 正在生成，等待其完成
        while key in _summary_inflight:
            _summary_inflight_cond.wait(timeout=35)
        # 3. 再次检查缓存（等待期间可能已生成完毕）
        entry = _summary_cache.get(key)
        if entry and time.time() - entry.get('ts', 0) < _SUMMARY_TTL:
            return entry['summary']
        # 4. 标记为生成中
        _summary_inflight.add(key)

    try:
        # 5. 生成摘要（在锁外执行，避免阻塞其他请求）
        result = build_summary(content)
    except Exception:
        result = {'type': '', 'models': [], 'features': '', 'slogan': '', 'one_line': '生成失败', '_via': 'error'}
    finally:
        with _summary_inflight_cond:
            _summary_inflight.discard(key)
            _summary_inflight_cond.notify_all()

    # 6. 写缓存
    sc_set(result, weibo_id, content)
    return result


# 启动时加载磁盘缓存
_sc_load()

# ============ 结构化摘要提取（基于 weiboPhoneMonitor.md 规则） ============

def _extract_hashtag_content(text):
    """Fix1: 提取话题标签内的有效文字（过滤纯广告词），用于辅助功能点提取"""
    tags = re.findall(r'#([^#]+)#', text)
    # 过滤掉过长（>10字）或明显是话题名的标签（含"活动""话题"等）
    noise = {'活动', '话题', '官方', '微博', '转发', '抽奖', '福利', '投票'}
    result = []
    for t in tags:
        t = t.strip()
        if 2 <= len(t) <= 12 and not any(n in t for n in noise):
            result.append(t)
    return ' '.join(result)


def _clean_text(text):
    """清洗正文：去掉话题标签（含后紧跟标点）、@用户、URL、多余空白"""
    t = re.sub(r'\s*#[^#]+#\s*[，、：；,]?\s*', ' ', text)
    t = re.sub(r'@\S+', '', t)
    t = re.sub(r'https?://\S+', '', t)
    t = re.sub(r'[\u200b\u3000\xa0]+', ' ', t)
    t = re.sub(r'^[^\w\u4e00-\u9fff]+', '', t.strip())
    t = re.sub(r'[ \t]+', ' ', t)
    return t.strip()


# Fix2: 帖子分类——每类有基础关键词和高权重词，高权重词命中×3
# 格式：(类型名, [普通词列表], [高权重词列表])
_TYPE_RULES = [
    ('新品动态', [
        '上市', '预售', '开售', '新机', '配色', '新色', '亮相', '登场',
        '正式上市', '现货', '预约', '开启预约', '即将发布',
    ], [
        '发布会', '新品发布', '首发', '预热', '首秀', '正式发布', '售价',  # 命中任一 ×3
    ]),
    ('技术官宣', [
        '芯片', '自研', '算法', '传感器', '专利', '架构', 'Hi-Fi',
        '降噪', '潜望', '主摄', '长焦', '折叠屏', '铰链', '大底', '圈铁', '超声波',
    ], []),
    ('系统/功能迭代', [
        '系统更新', '版本更新', '内测', '公测', '推送', '新功能',
        '功能上线', '修复', '下放', '优化升级',
    ], []),
    ('营销活动', [
        '优惠', '折扣', '赠品', '以旧换新', '换新补贴', '促销', '降价',
        '满减', '大促', '加赠', '立减', '直降', '返现', '免息',
        '特价', '秒杀', '抢购', '购机', '限时', '618', '双11',
    ], []),
    ('品牌合作/代言', ['代言人', '品牌大使', '联名', '合拍', 'IP合作', '跨界合作'], []),
    ('用户运营/答疑', ['使用教程', '使用指南', '小技巧', '答疑', '怎么用'], []),
    ('行业战略发声',  ['行业趋势', '品牌愿景', '年度战略', '未来规划'], []),
    ('市场策略',      ['产品线调整', '价格体系', '市场定位'], []),
]

# 机型正则（覆盖主流品牌）
_MODEL_PATTERNS = [
    r'vivo\s+X\s*Fold\s*\d+',
    r'vivo\s+X\s*Flip\s*\d*',
    r'vivo\s+X\d+\s*(?:Ultra|Pro|Plus|SE|T)?(?:\s*系列)?',
    r'vivo\s+S\d+\s*(?:Pro|SE)?(?:\s*系列)?',
    r'vivo\s+Y\d+(?:\s*系列)?',
    r'vivo\s+TWS\s*\d+\s*(?:Pro|SE)?',
    r'vivo\s+Pad\s*\d+\s*(?:Pro|SE)?',
    r'iQOO\s+(?:Neo\s*)?\d+\s*(?:Ultra|Pro|Plus|SE|GT)?(?:\s*系列)?',
    r'小米\s*\d+\s*(?:Ultra|Pro|Plus|SE|Max)?(?:\s*系列)?',
    r'Redmi\s+(?:Note\s+)?\d+\s*(?:Ultra|Pro|Plus|Turbo)?(?:\s*系列)?',
    r'OPPO\s+Find\s+[NX]\d+\s*(?:Ultra|Pro)?(?:\s*系列)?',
    r'OPPO\s+Reno\s*\d+\s*(?:Ultra|Pro|Plus|F)?(?:\s*系列)?',
    r'荣耀\s+Magic\s*\d+\s*(?:Pro|Ultra|Ultimate|RS|Lite)?(?:\s*系列)?',
    r'三星\s+Galaxy\s+[SZA]\d+\s*\w*(?:\s*系列)?',
    r'iPhone\s+\d+\s*(?:Pro\s*Max|Pro|Plus|e)?',
    r'华为\s+(?:Mate|P|nova)\s*\d+\s*\w*(?:\s*系列)?',
    r'一加\s+\d+\s*(?:Ultra|Pro|T)?(?:\s*系列)?',
]
_COMPILED_MODELS = [re.compile(p, re.IGNORECASE) for p in _MODEL_PATTERNS]

_TECH_RE = re.compile(
    r'[^\n。！？，]{2,20}'
    r'(?:\d+(?:mAh|W|mm|MP|GHz|GB|TB|fps|Hz|英寸|nm|倍|亿|颗)'
    r'|芯片|主摄|长焦|超广角|快充|续航|降噪|防水|折叠|铰链'
    r'|处理器|GPU|NPU|圈铁|Hi-Fi|立体声)'
    r'[^\n。！？，]{0,20}'
)

# Fix3: 宣传语识别——无引号短句候选（不含购买动作词/数字/链接）
_BUY_WORDS = {'购', '买', '订', '抢', '补贴', '元', '折', '赠', '链接', '扫码', '点击', '预约入口'}
_SLOGAN_ADJ = ['超', '旗舰', '极致', '全新', '突破', '震撼', '纯粹', '完美', '领先',
               '无限', '开启', '未来', '超能', '全面', '卓越', '强悍']


def _classify_post(text):
    """Fix2: 高权重词命中×3，确保新品/技术不被营销词压制"""
    scores = {}
    for rule in _TYPE_RULES:
        type_name, normal_kws, high_kws = rule[0], rule[1], rule[2]
        score = sum(1 for kw in normal_kws if kw in text)
        score += sum(3 for kw in high_kws if kw in text)
        if score > 0:
            scores[type_name] = score
    return max(scores, key=scores.get) if scores else '其他'


def _extract_models(text):
    models, seen = [], set()
    for regex in _COMPILED_MODELS:
        for m in regex.finditer(text):
            model = re.sub(r'\s+', ' ', m.group().strip())
            if model not in seen:
                seen.add(model)
                models.append(model)
    return models


def _extract_features(text, post_type, tag_text=''):
    """Fix1: 将话题标签文字也纳入功能点搜索范围"""
    if post_type in ('营销活动', '品牌合作/代言', '用户运营/答疑', '其他'):
        return ''
    # 合并原文 + 话题标签内容一起搜索
    search_text = text + ' ' + tag_text
    parts, seen = [], set()
    for m in _TECH_RE.finditer(search_text):
        feat = _clean_text(m.group()).strip('，。！？、 ')
        if feat and feat not in seen and 4 <= len(feat) <= 40:
            seen.add(feat)
            parts.append(feat)
    # 话题标签中的短功能词单独补充（如 "OriginOS 6"、"AI轻办公"）
    for tag in re.findall(r'#([^#]{2,12})#', text):
        tag = tag.strip()
        tech_kws = ['OS', 'AI', '芯片', '屏', '摄', '充', '快', '降噪', 'Pro', 'Ultra']
        if any(kw in tag for kw in tech_kws) and tag not in seen:
            seen.add(tag)
            parts.append(tag)
    return '；'.join(parts[:3]) if parts else ''


def _extract_slogan(text):
    """Fix3: 引号宣传语 + 无引号感叹短句双路识别"""
    # 路径1：引号包裹
    for pattern in [
        u'[\u300c\u201c\u2018]([^\u300c\u201c\u2018\u300d\u201d\u2019]{4,35})[\u300d\u201d\u2019]',
        r'"([^"]{4,35})"',
    ]:
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    # 路径2：无引号——独立短句（不含数字/购买动作词，含形容词/感叹号）
    cleaned = _clean_text(text)
    candidates = []
    for seg in re.split(r'[，,\n]', cleaned):
        seg = seg.strip().rstrip('！')
        if not (5 <= len(seg) <= 22):
            continue
        if re.search(r'\d', seg):
            continue
        if any(w in seg for w in _BUY_WORDS):
            continue
        if any(kw in seg for kw in _SLOGAN_ADJ):
            candidates.append(seg + ('！' if '！' not in seg else ''))
    return candidates[0] if candidates else ''


def _build_one_line(cleaned, models):
    model0 = models[0] if models else ''
    best = ''
    for s in re.split(r'[。！？\n]+', cleaned):
        s = re.sub(r'^[\u4e00-\u9fff]{1,2}[\s，、：；,]+', '', s.strip().lstrip('，、：；'))
        if not s or len(s) < 6:
            continue
        if model0 and model0 in s and len(s) <= 60:
            best = s
            break
        if not best and len(s) >= 8:
            best = s
    if not best:
        best = cleaned[:58]
    if len(best) > 60:
        best = best[:58].rstrip('，、：；—…') + '…'
    return best


# ============ LLM 摘要（智谱 GLM 优先，降级到规则引擎） ============

_GLM_SYSTEM_PROMPT = """# 角色
你是一名资深的消费电子行业竞品情报分析师，专门负责从复杂的品牌官方微博中剔除营销水份，压榨出硬核的技术、产品与营销动向。

## 任务
对微博帖子原文逐字段执行以下提取流程，输出结构化竞品动态信息。

## 全局规则
- 必须基于原文，严禁推断、概括、润色、捏造。
- 若某字段在原文中完全没有对应内容，该字段整行直接填：/
- 严格按照输出格式返回，不要附带任何首尾解释、问候或多余空格。

---

## 字段提取流程

### 第一步：判断【事件类型】
从以下标准类型中选择唯一最符合的一个填入：
- 新品预热：产品未发布/未开售，发布倒计时或卖点造势。
- 新品发布：产品正式发布、上市或首销。
- 参数曝光：单纯公布具体软硬件配置、技术参数、性能跑分。
- 促销活动：降价、降价预告、补贴、以旧换新、节日优惠。
- 线下活动：展会（如MWC/AWE）、快闪店、线下发布会现场、粉丝见面会。
- 品牌宣传：无明确单品，纯企业形象、代言人官宣、财报、公益。

### 第二步：识别【产品】
1. 找出帖子中作为宣发主体的具体硬件型号（如 vivo X Fold6）或核心软件系统（如 OriginOS 6）。
2. 如果同一篇帖子中出现了技术底座/处理器/配件（如"搭载骁龙8Gen3"或"由XX提供"），技术底座本身不填入此列。
3. 多个主推产品用逗号隔开。若无具体产品则填 /。

### 第三步：提取【产品卖点】

【门槛过滤（首要步骤）】：
检查原文是否包含描述该产品的"具体软硬件功能、技术规格、核心配置、实际使用体验"的句子？
如果是以下情况，本字段直接填 /，严禁强行提炼：
- 纯活动通知或链接引导（如：点击链接、锁定直播间、欢迎来现场体验）。
- 纯感性赞美词（如：引领风尚、卓越体验、展开无限可能）。

【提取规则（通过门槛后执行）】：
格式必须为：`卖点名称：卖点描述`，每条单独一行。
1. 卖点名称：必须是原文中出现的具体功能词、技术短语。优先取#话题#（去掉#符号）、引号、或冒号前的名词短语。
2. 卖点描述：紧跟该名词短语的具体功能效果或技术原话。
3. 注意事项：严禁将营销口号（如"做你的双机好搭子"）或产品定位标签（如"AI轻办公神器"）单独拆为卖点名称和描述。必须提取其背后支撑的具体技术功能。

### 第四步：写【一句话摘要】
根据【事件类型】严格套用以下模版，总字数控制在60字以内：
- 新品预热/发布：[产品名]将于[时间]发布/上市，主打[按原文顺序提取的前1-3个核心卖点名称]。
- 参数曝光：[产品名]公布/搭载[按原文顺序提取的前1-3个核心参数]。
- 促销活动：[产品名]开展[核心优惠政策]，时限[时间范围/截止时间]。
- 线下活动：[产品名]于[时间]在[地点]开展[活动名称]。
- 品牌宣传：[品牌名][核心品牌动向事件]。"""

def _load_llm_config():
    """从 config.json 读取 LLM 配置"""
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            c = json.load(f)
        return {
            'api_key': c.get('llm_api_key', ''),
            'base_url': c.get('llm_base_url', 'https://open.bigmodel.cn/api/paas/v4/chat/completions'),
            'model': c.get('llm_model', 'glm-4-flash'),
        }
    except Exception:
        return {}


def _call_glm(text):
    """调用智谱 GLM API，返回模板格式文本或 None"""
    cfg = _load_llm_config()
    api_key = cfg.get('api_key', '')
    if not api_key:
        return None
    # few-shot 示例1：多卖点提取（功能词作为卖点名称）
    _FS1_USER = '请提取以下帖子的关键信息：\n#vivo X Fold6# 做你的双机好搭子。 ✅全新上线电脑模式，折叠手机秒变mini主机。手机连上大屏后，会进入一套更接近PC的桌面界面。 ✅车机一碰传。出门前在手机查好的导航路线，上车轻轻一碰，就能直接流转到车机大屏。 ✅双机随心编。小屏上的文件、一键推送到大屏上编辑。 💡6月26日19:00，#AI轻办公神器##vivo X Fold6# 全新发布，敬请期待！#OriginOS 6#'
    _FS1_ASST = '【事件类型】\n新品预热\n\n【产品】\nvivo X Fold6, OriginOS 6\n\n【产品卖点】\n电脑模式：折叠手机秒变mini主机。手机连上大屏后，会进入一套更接近PC的桌面界面\n车机一碰传：出门前在手机查好的导航路线，上车轻轻一碰，就能直接流转到车机大屏\n双机随心编：小屏上的文件、一键推送到大屏上编辑\n\n【一句话摘要】\nvivo X Fold6将于6月26日19:00发布，主打电脑模式、车机一碰传和双机随心编。'

    # few-shot 示例2：纯营销引流帖，卖点填/
    _FS2_USER = '请提取以下帖子的关键信息：\n当从容松弛的先锋气质，遇上科技美学的巅峰之作。跟着vivo X Fold6先锋大使@朱珠ZhuZhu 感受 #vivo X Fold6# 带来的轻盈与自在！🎁 惊喜福利掉落：即刻打开京东APP，搜索关键词【朱珠】，就有机会赢取#vivo X Fold6#新品手机、朱珠亲笔签名照等丰厚好礼！👉 预约新品→网页链接'
    _FS2_ASST = '【事件类型】\n新品预热\n\n【产品】\nvivo X Fold6\n\n【产品卖点】\n/\n\n【一句话摘要】\nvivo X Fold6进行新品预热，开展搜索关键词赢手机与签名照活动。'

    payload = json.dumps({
        'model': cfg.get('model', 'glm-4-flash'),
        'messages': [
            {'role': 'system', 'content': _GLM_SYSTEM_PROMPT},
            {'role': 'user', 'content': _FS1_USER},
            {'role': 'assistant', 'content': _FS1_ASST},
            {'role': 'user', 'content': _FS2_USER},
            {'role': 'assistant', 'content': _FS2_ASST},
            {'role': 'user', 'content': '请提取以下帖子的关键信息：\n' + text},
        ],
        'temperature': 0.1,
    }).encode('utf-8')
    try:
        req = urllib.request.Request(
            cfg.get('base_url', 'https://open.bigmodel.cn/api/paas/v4/chat/completions'),
            data=payload,
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + api_key,
            },
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode('utf-8'))
        return result['choices'][0]['message']['content'].strip()
    except Exception:
        return None


def _parse_glm_output(raw):
    """将 GLM 输出的模板文本解析为结构化 dict
    
    兼容两种格式：
    1. 值在标签同行：【字段】：值
    2. 值在标签下一行：**【字段】**\n值（新格式）
    """
    _EMPTY = {'/', '无', '--', '暂无', 'N/A', '**', ''}

    def get_field(labels, text):
        """提取单行或下一行的单值字段"""
        for label in (labels if isinstance(labels, list) else [labels]):
            # 格式1：同行值 【字段】：值
            m = re.search(r'【' + label + r'】[*\s：:]*([^\n*【]{2,})', text)
            if m:
                val = m.group(1).strip().strip('*').strip()
                if val not in _EMPTY:
                    return val
            # 格式2：下一行值  **【字段】**\n值
            m = re.search(r'【' + label + r'】[^\n]*\n\s*([^\n*-【]{2,})', text)
            if m:
                val = m.group(1).strip().strip('*').strip()
                if val not in _EMPTY:
                    return val
        return ''

    def get_block(labels, text):
        """提取多行块（标签行到下一个标签行之间）"""
        for label in (labels if isinstance(labels, list) else [labels]):
            m = re.search(
                r'\*{0,2}【' + label + r'】\*{0,2}[^\n]*\n([\s\S]*?)(?=\n\*{0,2}【|\Z)',
                text
            )
            if m:
                block = m.group(1).strip()
                if block and block not in _EMPTY:
                    return block
        return ''

    post_type = get_field(['事件类型', '帖子类型', '类型'], raw)
    models_str = get_field(['产品', '机型', '产品名称', '型号'], raw)
    models = [x.strip() for x in re.split(r'[、，,；;]', models_str) if x.strip() and x.strip() not in _EMPTY] if models_str else []
    features = get_block(['产品卖点', '核心卖点', '功能点', 'FABE分析'], raw)
    slogan = get_block(['宣传话术', '原文宣传语'], raw)
    one_line = get_field(['一句话摘要', '一句话总结', '总结', '一句话'], raw)

    return {
        'type': post_type,
        'models': models,
        'features': features,
        'slogan': slogan,
        'fabe': '',
        'one_line': one_line,
        'raw_text': raw,
        '_via': 'llm',
    }


def build_summary(content):
    """优先用智谱 GLM 提取摘要，不可用时降级到规则引擎"""
    text = (content or '').strip()
    if not text:
        return {'type': '', 'models': [], 'features': '', 'slogan': '', 'one_line': '无正文内容'}

    # LLM 路径
    raw = _call_glm(text)
    if raw:
        return _parse_glm_output(raw)

    # 降级：规则引擎
    tag_text = _extract_hashtag_content(text)
    cleaned = _clean_text(text)
    post_type = _classify_post(text)
    models = _extract_models(text)
    features = _extract_features(text, post_type, tag_text)
    slogan = _extract_slogan(text)
    one_line = _build_one_line(cleaned, models)
    return {
        'type': post_type,
        'models': models,
        'features': features,
        'slogan': slogan,
        'one_line': one_line,
        '_via': 'rule',
    }


# ============ 帖子持久化缓存 ============
# 存储路径：web_app/weibo_cache.json
_WEIBO_CACHE_PATH = os.path.join(BASE_DIR, 'web_app', 'weibo_cache.json')
# 内存结构：{ user_id: { weibo_id: item_dict, ... } }
_cache = {}
# 用户昵称缓存：{ user_id: nickname }
_user_cache = {}
# 每个用户最近抓取时间：{ user_id: iso_string }
_fetch_time = {}
# 帖子缓存读写锁
_cache_lock = threading.Lock()


def _weibo_cache_load():
    """启动时从磁盘加载帖子缓存"""
    global _cache, _user_cache, _fetch_time
    try:
        if os.path.exists(_WEIBO_CACHE_PATH):
            with open(_WEIBO_CACHE_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            _cache = data.get('cache', {})
            _user_cache = data.get('user_cache', {})
            _fetch_time = data.get('fetch_time', {})
    except Exception:
        _cache, _user_cache, _fetch_time = {}, {}, {}


def _weibo_cache_flush():
    """将帖子缓存写入磁盘（调用方需持有 _cache_lock）"""
    try:
        os.makedirs(os.path.dirname(_WEIBO_CACHE_PATH), exist_ok=True)
        tmp = _WEIBO_CACHE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({
                'cache': _cache,
                'user_cache': _user_cache,
                'fetch_time': _fetch_time,
            }, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _WEIBO_CACHE_PATH)
    except Exception:
        pass


def cache_get(user_id):
    """返回该用户所有已缓存的 weibo 列表（按发布时间倒序）"""
    store = _cache.get(user_id, {})
    return sorted(store.values(), key=lambda x: x.get('publish_time', ''), reverse=True)


def cache_merge(user_id, new_weibos, nickname=''):
    """将新抓取的 weibos 合并入缓存，返回 (新增条数, 合并后总条数, 新增id集合)"""
    with _cache_lock:
        if user_id not in _cache:
            _cache[user_id] = {}
        store = _cache[user_id]
        new_ids = set()
        for item in new_weibos:
            wid = str(item.get('id', ''))
            if wid and wid not in store:
                store[wid] = item
                new_ids.add(wid)
        if nickname:
            _user_cache[user_id] = nickname
        _fetch_time[user_id] = time.strftime('%Y-%m-%d %H:%M:%S')
        _weibo_cache_flush()
    return len(new_ids), len(store), new_ids


def cache_clear(user_id=None):
    """清空指定用户或全部缓存，并同步到磁盘"""
    with _cache_lock:
        if user_id:
            _cache.pop(user_id, None)
            _fetch_time.pop(user_id, None)
        else:
            _cache.clear()
            _fetch_time.clear()
        _weibo_cache_flush()


# 启动时加载持久化帖子缓存
_weibo_cache_load()


# ============ 抓取逻辑（硬编码仅原创） ============

def crawl_weibos(cookie, user_id, since_date, end_date, limit):
    """按页抓取微博，仅原创（filter 硬编码为 1）"""
    filter_flag = 1  # 硬编码：仅抓取原创微博，不可切换
    user_config = {
        'user_uri': user_id,
        'since_date': since_date,
        'end_date': end_date,
    }

    index_parser = IndexParser(cookie, user_id)
    user = index_parser.get_user()
    page_num = index_parser.get_page_num() or 1

    weibos = []
    weibo_id_list = []
    page = 1
    while page <= page_num:
        parser = PageParser(cookie, user_config, page, filter_flag)
        result = parser.get_one_page(weibo_id_list)
        if not result:
            break
        page_weibos, weibo_id_list, to_continue = result
        if page_weibos:
            for wb in page_weibos:
                item = wb.to_dict()
                # 不在抓取阶段生成摘要，由前端异步请求 /api/summary
                item['summary'] = None
                weibos.append(item)
                if limit and len(weibos) >= limit:
                    return user, weibos[:limit]
        if not to_continue:
            break
        page += 1

    return user, weibos


def user_to_dict(user):
    """User 对象转字典"""
    if user is None:
        return {}
    fields = ['id', 'nickname', 'gender', 'location', 'birthday',
              'description', 'verified_reason', 'talent', 'education',
              'work', 'weibo_num', 'following', 'followers']
    result = {}
    for f in fields:
        if hasattr(user, f):
            result[f] = getattr(user, f)
    return result


@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


@app.route('/api/user_info', methods=['GET'])
def api_user_info():
    user_id = str(request.args.get('user_id', '')).strip()
    if not user_id:
        return jsonify({'success': False, 'message': '缺少 user_id'}), 400
    cookie = load_cookie()
    if not cookie or cookie == 'your cookie':
        return jsonify({'success': False, 'message': '未配置 cookie'}), 400
    try:
        index_parser = IndexParser(cookie, user_id)
        user = index_parser.get_user()
        return jsonify({'success': True, 'user': user_to_dict(user)})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/summary', methods=['POST'])
def api_summary():
    """单条帖子摘要生成接口，供前端异步调用（带缓存）"""
    data = request.get_json(force=True)
    content = str(data.get('content', '')).strip()
    weibo_id = str(data.get('weibo_id', '')).strip()
    if not content:
        return jsonify({'success': False, 'message': '缺少 content'}), 400
    try:
        summary = build_summary_cached(content, weibo_id)
        cached = sc_get(weibo_id, content) is not None
        return jsonify({'success': True, 'summary': summary, 'cached': cached})
    except Exception as e:
        # fallback：直接调用原始生成，不写缓存
        try:
            summary = build_summary(content)
            return jsonify({'success': True, 'summary': summary, 'cached': False})
        except Exception as e2:
            return jsonify({'success': False, 'message': str(e2)}), 500


@app.route('/api/fetch', methods=['POST'])
def api_fetch():
    data = request.get_json(force=True)
    user_id = str(data.get('user_id', '')).strip()
    mode = data.get('mode', 'count')

    if not user_id:
        return jsonify({'success': False, 'message': '请填写微博用户 ID'}), 400

    cookie = load_cookie()
    if not cookie or cookie == 'your cookie':
        return jsonify({'success': False,
                        'message': '请先在 config.json 中配置有效的 cookie'}), 400

    try:
        if mode == 'date':
            since_date = data.get('since_date')
            end_date = data.get('end_date') or 'now'
            if not since_date:
                return jsonify({'success': False,
                                'message': '请选择起始日期'}), 400
            user, weibos = crawl_weibos(cookie, user_id, since_date,
                                        end_date, None)
        else:
            limit = int(data.get('limit', 10))
            if limit <= 0:
                return jsonify({'success': False,
                                'message': '数量需为正整数'}), 400
            user, weibos = crawl_weibos(cookie, user_id, '2010-01-01',
                                        'now', limit)

        # 合并入缓存（昵称一并写入）
        nickname = user.nickname if (user and hasattr(user, 'nickname')) else ''
        new_count, total_count, new_ids = cache_merge(user_id, weibos, nickname)
        all_weibos = cache_get(user_id)
        # 给每条帖子标注是否新增
        for w in all_weibos:
            w['_is_new'] = str(w.get('id', '')) in new_ids

        return jsonify({
            'success': True,
            'user': user_to_dict(user),
            'new_count': new_count,
            'count': total_count,
            'fetch_time': _fetch_time.get(user_id, ''),
            'weibos': all_weibos,
        })
    except Exception as e:
        return jsonify({'success': False, 'message': '抓取失败：%s' % str(e)}), 500


@app.route('/api/cache/clear', methods=['POST'])
def api_cache_clear():
    """清除缓存：
    - user_id 清指定用户的帖子列表缓存
    - clear_summary=true 同时清空摘要缓存
    - 不传 user_id 则清全部帖子列表缓存
    """
    data = request.get_json(force=True) or {}
    user_id = str(data.get('user_id', '')).strip() or None
    clear_summary = data.get('clear_summary', False)
    cache_clear(user_id)
    if clear_summary:
        sc_clear()
    return jsonify({'success': True, 'message': '缓存已清除'})


@app.route('/api/compare', methods=['GET'])
def api_compare():
    """竞品对比接口
    按日期×品牌聚合已缓存的帖子摘要，返回结构：
    {
      "dates": ["2026-06-24", ...],          # 升序日期列表
      "brands": [{"id":"xxx","name":"yyy"}, ...],
      "grid": {
        "2026-06-24": {
          "xxx": [{"content":"...","summary":{...}}, ...],
          ...
        }
      }
    }
    可选参数：since=YYYY-MM-DD, until=YYYY-MM-DD
    """
    since_str = request.args.get('since', '')
    until_str = request.args.get('until', '')

    # 收集所有用户缓存
    brands = []
    grid = {}   # { date_str: { user_id: [post,...] } }
    date_set = set()

    for user_id, store in _cache.items():
        name = _user_cache.get(user_id, user_id)
        brands.append({
            'id': user_id,
            'name': name,
            'fetch_time': _fetch_time.get(user_id, ''),
        })

        for item in store.values():
            pt = (item.get('publish_time') or '')[:10]  # 取 YYYY-MM-DD
            if not pt or len(pt) < 10:
                continue
            if since_str and pt < since_str:
                continue
            if until_str and pt > until_str:
                continue
            date_set.add(pt)
            if pt not in grid:
                grid[pt] = {}
            if user_id not in grid[pt]:
                grid[pt][user_id] = []

            # 查摘要缓存
            wid = str(item.get('id', ''))
            content = item.get('content', '')
            summary = sc_get(wid, content)

            grid[pt][user_id].append({
                'id': wid,
                'publish_time': item.get('publish_time', ''),
                'content': content,
                'summary': summary,
            })

    # 每格内按时间排序
    for date_str in grid:
        for uid in grid[date_str]:
            grid[date_str][uid].sort(key=lambda x: x['publish_time'], reverse=True)

    dates = sorted(date_set, reverse=True)  # 最新日期在前
    # 品牌按 ID 稳定排序
    brands.sort(key=lambda b: b['id'])

    return jsonify({
        'success': True,
        'dates': dates,
        'brands': brands,
        'grid': grid,
    })


@app.route('/api/img_proxy')
def api_img_proxy():
    """图片代理：绕过新浪 CDN 防盗链，以 weibo.com 作为 Referer 获取图片"""
    url = request.args.get('url', '').strip()
    if not url or not url.startswith('http'):
        return '', 400
    try:
        req = urllib.request.Request(
            url,
            headers={
                'Referer': 'https://weibo.com/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
            content_type = resp.headers.get('Content-Type', 'image/jpeg')
        from flask import Response
        return Response(data, content_type=content_type)
    except Exception:
        return '', 404


def load_cookie():
    """从 config.json 读取 cookie"""
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            config = json.load(f)
        return config.get('cookie', '')
    except Exception:
        return ''


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)