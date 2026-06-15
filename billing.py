"""墨枢 MOSHU — 计费与余额"""
from db import query, execute
from config import QUOTA_PER_UNIT, USD_EXCHANGE_RATE

def calc_quota(prompt_tokens, completion_tokens, input_price, output_price):
    """根据输入/输出价格计算 quota 内部单位
    input_price / output_price: CNY per million tokens
    """
    cost_cny = prompt_tokens / 1_000_000 * input_price + completion_tokens / 1_000_000 * output_price
    return int(cost_cny / USD_EXCHANGE_RATE * QUOTA_PER_UNIT)

def quota_to_cny(quota):
    """quota → 人民币"""
    return quota / QUOTA_PER_UNIT * USD_EXCHANGE_RATE

def cny_to_quota(cny):
    """人民币 → quota"""
    return int(cny / USD_EXCHANGE_RATE * QUOTA_PER_UNIT)

def deduct_user(user_id, quota_cost):
    """扣除用户余额，返回是否成功"""
    if quota_cost <= 0:
        return True
    user = query("SELECT quota, used_quota FROM users WHERE id=? AND status=1", (user_id,), one=True)
    if not user:
        return False
    new_quota = user['quota'] - quota_cost
    if new_quota < 0:
        return False
    execute(
        "UPDATE users SET quota=?, used_quota=? WHERE id=?",
        (new_quota, user['used_quota'] + quota_cost, user_id)
    )
    return True

def deduct_token(token_id, quota_cost):
    """扣除令牌余额（如果是有限额的），返回是否成功"""
    if quota_cost <= 0:
        return True
    tk = query("SELECT remain_quota, unlimited_quota FROM tokens WHERE id=? AND status=1", (token_id,), one=True)
    if not tk:
        return False
    if tk['unlimited_quota']:
        return True
    new_remain = tk['remain_quota'] - quota_cost
    if new_remain < 0:
        return False
    execute("UPDATE tokens SET remain_quota=? WHERE id=?", (new_remain, token_id))
    return True

def get_pricing(model_name):
    """获取模型定价，不存在则返回默认值"""
    p = query("SELECT * FROM pricing WHERE model_name=? AND enabled=1", (model_name,), one=True)
    if p:
        return p
    return {'model_name': model_name, 'input_price': 2.0, 'output_price': 2.0}

def find_channel(model_name):
    """找到支持该模型的可用渠道（按优先级、权重）"""
    channels = query("SELECT * FROM channels WHERE status=1 ORDER BY priority DESC")
    import json
    for ch in channels:
        models = json.loads(ch['models'])
        if model_name in models:
            return ch
    return None
