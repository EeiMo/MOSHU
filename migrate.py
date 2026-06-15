"""墨枢 MOSHU — 从 New API 迁移数据"""
import sqlite3, json, time, sys, os

SRC = sys.argv[1] if len(sys.argv) > 1 else '/root/moshu/data/one-api.db'
DST = sys.argv[2] if len(sys.argv) > 2 else '/root/moshu/backend/data/moshu.db'

print(f'[MOSHU] migrate: {SRC} -> {DST}')
os.makedirs(os.path.dirname(DST) or '.', exist_ok=True)

src = sqlite3.connect(SRC)
src.row_factory = sqlite3.Row

def rg(row, key, default=None):
    try:
        val = row[key]
        return val if val is not None else default
    except (IndexError, KeyError):
        return default

# 初始化目标库
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import init_db, get_db
init_db()

dst = get_db()
stats = {}

# ── 读取 New API options ──
opts = {}
for row in src.execute("SELECT key, value FROM options").fetchall():
    opts[row['key']] = row['value']

model_ratio = json.loads(opts.get('ModelRatio', '{}'))
completion_ratio = json.loads(opts.get('CompletionRatio', '{}'))

DEFAULT_RATIO = 0.014

# ── 1. 用户 ──
print('[1/5] users...')
users = src.execute("SELECT * FROM users").fetchall()
count = 0
for u in users:
    existing = dst.execute("SELECT id FROM users WHERE username=?", (u['username'],)).fetchone()
    if existing:
        if u['id'] != 1:
            dst.execute(
                "UPDATE users SET quota=?, used_quota=?, display_name=? WHERE username=?",
                (u['quota'] or 0, u['used_quota'] or 0, u['display_name'] or '', u['username'])
            )
        continue
    dst.execute(
        "INSERT INTO users (username, password_hash, display_name, role, quota, used_quota, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (u['username'], u['password'], u['display_name'] or '',
         'admin' if (u['role'] or 0) >= 10 else 'user',
         u['quota'] or 0, u['used_quota'] or 0,
         u['status'] or 1, rg(u, 'created_time', time.time()))
    )
    count += 1
dst.commit()
stats['users'] = count
print(f'  users: {count}')

# ── 2. 渠道 ──
print('[2/5] channels...')
CHANNEL_TYPES = {
    1: 'https://api.openai.com/v1',
    3: 'https://api.azure.com',
    14: 'https://api.deepseek.com/v1',
    24: 'https://ark.cn-beijing.volces.com/api/v3',
    40: 'https://api.anthropic.com/v1',
}

channels = src.execute("SELECT * FROM channels").fetchall()
count = 0
for ch in channels:
    existing = dst.execute("SELECT id FROM channels WHERE name=?", (ch['name'],)).fetchone()
    if existing:
        continue

    base_url = (ch['base_url'] or '').strip().rstrip('/')
    if not base_url:
        base_url = CHANNEL_TYPES.get(ch['type'], 'https://api.openai.com/v1')

    models_str = ch['models'] or ''
    models = [m.strip() for m in models_str.split(',') if m.strip()]

    model_mapping = {}
    other = rg(ch, 'other', '')
    if other:
        try:
            other_data = json.loads(other)
            model_mapping = other_data.get('model_mapping', {})
        except (json.JSONDecodeError, TypeError):
            pass

    dst.execute(
        "INSERT INTO channels (name, type, base_url, api_key, models, model_mapping, status, priority, weight, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ch['name'], ch['type'] or 1, base_url, ch['key'] or '',
         json.dumps(models), json.dumps(model_mapping),
         ch['status'] or 1, rg(ch, 'priority', 0), rg(ch, 'weight', 1),
         rg(ch, 'created_time', time.time()))
    )
    count += 1
dst.commit()
stats['channels'] = count
print(f'  channels: {count}')

# ── 3. 定价 ──
print('[3/5] pricing...')
model_rows = src.execute("SELECT * FROM models").fetchall()
count = 0
for m in model_rows:
    mn = m['model_name']
    existing = dst.execute("SELECT id FROM pricing WHERE model_name=?", (mn,)).fetchone()
    if existing:
        continue
    mr = model_ratio.get(mn, DEFAULT_RATIO)
    cr = completion_ratio.get(mn, 1)
    dst.execute(
        "INSERT INTO pricing (model_name, model_ratio, completion_ratio, enabled, created_at) VALUES (?,?,?,?,?)",
        (mn, mr, cr, 1, rg(m, 'created_time', time.time()))
    )
    count += 1
dst.commit()
stats['pricing'] = count
print(f'  pricing: {count}')

# ── 4. 令牌 ──
print('[4/5] tokens...')
tk_rows = src.execute("SELECT * FROM tokens").fetchall()
count = 0
for tk in tk_rows:
    raw_key = tk['key'] or ''
    if raw_key and not raw_key.startswith('sk-'):
        full_key = 'sk-' + raw_key
    else:
        full_key = raw_key

    existing = dst.execute("SELECT id FROM tokens WHERE `key`=?", (full_key,)).fetchone()
    if existing:
        continue

    remain = tk['remain_quota'] or 0
    unlimited = 1 if (tk['unlimited_quota'] or 0) == 1 else 0
    expired = tk['expired_time'] or 0
    if expired == -1:
        expired = 0

    dst.execute(
        "INSERT INTO tokens (user_id, name, `key`, status, remain_quota, unlimited_quota, expired_time, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (tk['user_id'], tk['name'] or 'migrated', full_key,
         tk['status'] or 1, remain, unlimited,
         expired, rg(tk, 'created_time', time.time()))
    )
    count += 1
dst.commit()
stats['tokens'] = count
print(f'  tokens: {count}')

# ── 5. 日志 ──
print('[5/5] logs...')
logs = src.execute("SELECT * FROM logs ORDER BY id ASC").fetchall()
count = 0
batch = []
for lg in logs:
    log_type = lg['type'] or 2
    content = lg['content'] or ''
    batch.append((
        lg['user_id'], rg(lg, 'token_id', 0),
        lg['model_name'] or '', rg(lg, 'channel_id', 0),
        lg['prompt_tokens'] or 0, lg['completion_tokens'] or 0,
        lg['quota'] or 0, lg['use_time'] or 0,
        1 if rg(lg, 'is_stream') else 0, content,
        rg(lg, 'request_id', ''), lg['ip'] or '',
        log_type, lg['created_at'] or time.time()
    ))
    if len(batch) >= 500:
        dst.executemany(
            "INSERT INTO logs (user_id, token_id, model_name, channel_id, prompt_tokens, completion_tokens, quota, use_time, is_stream, content, request_id, ip, type, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            batch
        )
        dst.commit()
        count += len(batch)
        batch = []

if batch:
    dst.executemany(
        "INSERT INTO logs (user_id, token_id, model_name, channel_id, prompt_tokens, completion_tokens, quota, use_time, is_stream, content, request_id, ip, type, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        batch
    )
    dst.commit()
    count += len(batch)

stats['logs'] = count
print(f'  logs: {count}')

dst.close()
src.close()

print('\n=== DONE ===')
for k, v in stats.items():
    print(f'  {k}: {v}')
