"""墨枢 MOSHU — 模型定价同步器

按渠道的实际上游抓取官方价格，换算成人民币 ¥/百万 tokens，
写入 New API 的 ModelRatio/CompletionRatio/CacheRatio（内部倍率）。

每个上游写一个 fetch_xxx() 抓取器，注册到 SOURCES。
新渠道只要标 price_source = 某个 key，即可自动同步。
"""
import re
import json
import sqlite3

import requests as http

PRICE_RATE = 14.6          # 1 ratio = ¥14.6/百万tokens（New API 基准）
USD_TO_CNY = 7.3           # 美元→人民币汇率


# ── 模型名归一化 ──
def norm_name(n):
    """Claude Opus 4.8 → claude-opus-4-8"""
    if not n:
        return ''
    n = n.strip()
    # 去掉尾部 " API"
    n = re.sub(r'\s+api$', '', n, flags=re.I)
    # 大小写归一：保留数字字母，空格→连字符，连续连字符合并
    n = re.sub(r'[\s/]+', '-', n)
    n = re.sub(r'-+', '-', n)
    return n.lower()


def match_key(n):
    """匹配用键：把点和连字符都统一成连字符，便于 4.8==4-8 容错匹配。"""
    n = norm_name(n)
    return re.sub(r'[.\-]+', '-', n)


# ── 抓取器 ──
def fetch_derouter():
    """DeRouter 官网 pricing 页，价格内联在 JSON-LD Offer 结构里。"""
    url = 'https://derouter.ai/pricing'
    resp = http.get(url, timeout=20)
    resp.raise_for_status()
    s = resp.text
    offers = re.findall(r'\{"@type":"Offer".*?\}', s, re.S)
    out = {}
    for o in offers:
        name = re.search(r'"name":"([^"]+)"', o)
        desc = re.search(r'"description":"([^"]+)"', o)
        if not (name and desc):
            continue
        nm = norm_name(name.group(1))
        d = desc.group(1)
        inp = re.search(r'input:\s*\$?([0-9.]+)', d, re.I)
        outp = re.search(r'output:\s*\$?([0-9.]+)', d, re.I)
        cache = re.search(r'cache[^:]*:\s*\$?([0-9.]+)', d, re.I)
        if inp:
            out[nm] = {
                'input_usd': float(inp.group(1)),
                'output_usd': float(outp.group(1)) if outp else None,
                'cache_usd': float(cache.group(1)) if cache else None,
            }
    return out


def fetch_deepseek():
    """DeepSeek 官网 pricing 页，HTML 表格三行两列：flash / pro。
    行序固定：CACHE HIT → CACHE MISS → OUTPUT，每行两列对应 flash,pro。"""
    url = 'https://api-docs.deepseek.com/quick_start/pricing'
    resp = http.get(url, timeout=20)
    resp.raise_for_status()
    s = re.sub(r'\s+', ' ', resp.text)  # 压平空白便于匹配
    hit = re.search(r'CACHE HIT\)</td><td>\$([0-9.]+)</td><td>\$([0-9.]+)</td>', s, re.I)
    miss = re.search(r'CACHE MISS\)</td><td>\$([0-9.]+)</td><td>\$([0-9.]+)</td>', s, re.I)
    output = re.search(r'OUTPUT TOKENS</td><td>\$([0-9.]+)</td><td>\$([0-9.]+)</td>', s, re.I)
    if not (hit and miss and output):
        raise RuntimeError('DeepSeek 价格表结构变化，解析失败')
    names = ['deepseek-v4-flash', 'deepseek-v4-pro']
    out = {}
    for i, nm in enumerate(names):
        out[nm] = {
            'input_usd': float(miss.group(i + 1)),   # cache miss = 输入价
            'cache_usd': float(hit.group(i + 1)),    # cache hit = 缓存价
            'output_usd': float(output.group(i + 1)),
        }
    return out


# ── 上游注册表 ──
# 无参抓取器：抓官网公开定价页
SOURCES = {
    'derouter': fetch_derouter,
    'deepseek': fetch_deepseek,
}

# 带渠道参数的抓取器：用渠道自己的 base_url+key 调上游 API
CHANNEL_SOURCES = set()  # 在下方填充


def fetch_openai_compatible(base_url, api_key, models=None):
    """通用抓取器：调上游 /v1/models，尝试从响应里提取价格。
    兼容多种价格字段命名（pricing/input_price/per_input_price 等）。
    models: 可选，只保留这些模型。"""
    base = (base_url or '').rstrip('/')
    if base.endswith('/v1'):
        url = base + '/models'
    else:
        url = base + '/v1/models'
    resp = http.get(url, headers={'Authorization': 'Bearer ' + api_key}, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f'上游 /v1/models 返回 {resp.status_code}')
    data = resp.json()
    items = data.get('data') if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise RuntimeError('上游 /v1/models 响应无 data 列表')
    out = {}
    for m in items:
        if not isinstance(m, dict):
            continue
        mid = m.get('id')
        if not mid:
            continue
        if models and mid not in models:
            continue
        # 尝试多种价格字段命名
        pricing = m.get('pricing') or m.get('price') or {}
        if not isinstance(pricing, dict):
            pricing = {}
        inp = _first_float(pricing, ['input', 'input_price', 'per_input_price', 'prompt'])
        outp = _first_float(pricing, ['output', 'output_price', 'per_output_price', 'completion'])
        cache = _first_float(pricing, ['cache_read', 'cache_read_input_price', 'cached_tokens', 'cache'])
        if inp is None and outp is None:
            continue  # 该模型没价格字段，跳过
        out[mid] = {'input_usd': inp, 'output_usd': outp, 'cache_usd': cache}
    return out


def _first_float(d, keys):
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return None


CHANNEL_SOURCES = {'openai_compatible'}

# base_url → price_source 自动识别
BASEURL_SOURCE_MAP = [
    ('deepseek.com', 'deepseek'),
    ('derouter.ai', 'derouter'),
]


def detect_source_by_baseurl(base_url):
    """根据渠道 base_url 自动判断价格来源。返回 source 或 None。"""
    u = (base_url or '').lower()
    for needle, src in BASEURL_SOURCE_MAP:
        if needle in u:
            return src
    return None


def usd_to_cny(v):
    return round(v * USD_TO_CNY, 4) if v is not None else None


def apply_prices(prices, conn, dry_run=False):
    """把抓到的价格换算并写入 New API options 表。prices: {model: {input_usd, output_usd, cache_usd}}"""
    mr = _load_option_map(conn, 'ModelRatio')
    cr = _load_option_map(conn, 'CompletionRatio')
    kar = _load_option_map(conn, 'CacheRatio')

    updated = []
    skipped = []
    for nm, p in prices.items():
        inp = p.get('input_usd')
        outp = p.get('output_usd')
        cache = p.get('cache_usd')
        if inp is None:
            skipped.append((nm, '缺输入价'))
            continue
        # ¥/百万tokens
        input_cny = inp * USD_TO_CNY
        output_cny = (outp or 0) * USD_TO_CNY
        cache_cny = (cache or 0) * USD_TO_CNY
        # 反算 New API 倍率
        model_ratio = input_cny / PRICE_RATE
        completion_ratio = output_cny / input_cny if input_cny > 0 else 0
        cache_ratio = cache_cny / input_cny if input_cny > 0 else 0

        mr[nm] = round(model_ratio, 8)
        cr[nm] = round(completion_ratio, 8)
        kar[nm] = round(cache_ratio, 8)
        updated.append({
            'model_name': nm,
            'input_price': round(input_cny, 4),
            'output_price': round(output_cny, 4),
            'cache_price': round(cache_cny, 4),
            'source': 'usd:%.4f/%.4f/%.4f' % (inp, outp or 0, cache or 0),
        })

    if not dry_run:
        _save_option_map(conn, 'ModelRatio', mr)
        _save_option_map(conn, 'CompletionRatio', cr)
        _save_option_map(conn, 'CacheRatio', kar)
        conn.commit()
    return updated, skipped


def _load_option_map(conn, key):
    row = conn.execute('SELECT value FROM options WHERE key=?', (key,)).fetchone()
    if not row or not row[0]:
        return {}
    try:
        data = json.loads(row[0])
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_option_map(conn, key, data):
    val = json.dumps(data, ensure_ascii=False, indent=2)
    conn.execute('INSERT OR REPLACE INTO options (key, value) VALUES (?, ?)', (key, val))


# ── 渠道→price_source 映射（存在 options 表，key=MoshuChannelPriceSource）──
CHANNEL_SOURCE_KEY = 'MoshuChannelPriceSource'


def get_channel_sources(conn):
    """返回 {channel_id: price_source}"""
    return _load_option_map(conn, CHANNEL_SOURCE_KEY)


def set_channel_source(conn, cid, source):
    m = _load_option_map(conn, CHANNEL_SOURCE_KEY)
    m[str(cid)] = source
    _save_option_map(conn, CHANNEL_SOURCE_KEY, m)
    conn.commit()


def sync_channel(conn, cid, source=None):
    """同步单个渠道。source 为 None 时自动检测。返回报告 dict。"""
    row = conn.execute('SELECT models, base_url, key FROM channels WHERE id=?', (cid,)).fetchone()
    if not row:
        return {'success': False, 'message': '渠道不存在'}
    models_str, base_url, api_key = row[0], row[1], row[2]
    try:
        ch_models = [m.strip() for m in (models_str or '').split(',') if m.strip()]
    except Exception:
        ch_models = []

    if not source:
        source = detect_source_by_baseurl(base_url)
        if not source:
            source = 'openai_compatible'  # 兜底：试通用 API 抓取

    # 渠道参数型抓取器
    if source == 'openai_compatible':
        if not api_key:
            return {'success': False, 'message': '通用抓取需要渠道 API Key', 'source': source}
        try:
            prices = fetch_openai_compatible(base_url, api_key, ch_models)
        except Exception as e:
            return {'success': False, 'message': f'通用抓取失败: {e}', 'source': source}
        if not prices:
            return {'success': False, 'message': '上游 /v1/models 未返回价格字段，需手动配价', 'source': source, 'channel_models': ch_models}
        updated, skipped = apply_prices(prices, conn)
        return {
            'success': True, 'source': source, 'channel_models': ch_models,
            'matched': [u['model_name'] for u in updated], 'updated': updated,
            'skipped': skipped, 'missing': [m for m in ch_models if m not in prices],
        }

    # 官网页无参抓取器
    fetcher = SOURCES.get(source)
    if not fetcher:
        return {'success': False, 'message': f'未知价格来源: {source}'}
    prices = fetcher()
    # 容错匹配：4.8 == 4-8。建 match_key 索引，再按渠道原名写入
    key_index = {match_key(nm): p for nm, p in prices.items()}
    target = {}
    missing = []
    for cm in ch_models:
        p = key_index.get(match_key(cm))
        if p:
            target[cm] = p  # 用渠道里的原名写入，保证 New API 计费能对上
        else:
            missing.append(cm)
    updated, skipped = apply_prices(target, conn)
    return {
        'success': True, 'source': source, 'channel_models': ch_models,
        'matched': [u['model_name'] for u in updated], 'updated': updated,
        'skipped': skipped, 'missing': missing,
    }


def sync_all(conn):
    """同步所有渠道。已显式配置来源的用配置值，否则按 base_url 自动检测/兜底通用抓取。"""
    sources = get_channel_sources(conn)
    channels = conn.execute('SELECT id, name FROM channels WHERE status=1').fetchall()
    report = []
    for cid, cname in channels:
        src = sources.get(str(cid))
        if src == 'manual':
            report.append({'channel_id': cid, 'channel_name': cname, 'source': 'manual', 'success': False, 'message': '手动，跳过'})
            continue
        r = sync_channel(conn, cid, src)  # src 为 None 时内部自动检测
        r['channel_id'] = cid
        r['channel_name'] = cname
        report.append(r)
    return report


if __name__ == '__main__':
    # 命令行测试：python pricing_sync.py [source]
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else 'derouter'
    fetcher = SOURCES.get(src)
    if not fetcher:
        print('可用来源:', list(SOURCES.keys()))
        sys.exit(1)
    prices = fetcher()
    print(f'== {src} ==')
    for nm, p in sorted(prices.items()):
        print(f"{nm:30} input=${p['input_usd']} output=${p.get('output_usd')} cache=${p.get('cache_usd')}"
              f"  => ¥{usd_to_cny(p['input_usd'])}/{usd_to_cny(p.get('output_usd'))}/{usd_to_cny(p.get('cache_usd'))}/百万")
