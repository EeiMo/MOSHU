"""墨枢 MOSHU — API 代理转发（核心）"""
import time, json, uuid, requests as http
from flask import Blueprint, request, jsonify, Response, stream_with_context, g
from db import query, execute
from billing import calc_quota, deduct_user, deduct_token, get_pricing, find_channel
from auth import login_required
from config import QUOTA_PER_UNIT, USD_EXCHANGE_RATE, PROXY_TIMEOUT

proxy = Blueprint('proxy', __name__)

# ── 系统状态 ──

@proxy.route('/api/status')
def status():
    return jsonify({
        'success': True,
        'data': {
            'quota_per_unit': QUOTA_PER_UNIT,
            'usd_exchange_rate': USD_EXCHANGE_RATE,
            'display_in_currency': True,
        }
    })

# ── 请求日志 ──

@proxy.route('/api/log/self', methods=['GET'])
@login_required
def log_self():
    page = int(request.args.get('p', 1))
    page_size = int(request.args.get('page_size', 50))
    log_type = request.args.get('type', '')
    offset = (page - 1) * page_size

    where = "WHERE user_id=?"
    params = [g.user_id]
    if log_type:
        where += " AND type=?"
        params.append(int(log_type))

    items = query(
        f"SELECT * FROM logs {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [page_size, offset]
    )
    return jsonify({'success': True, 'data': {'items': items}})

# ── 模型列表 ──

@proxy.route('/v1/models', methods=['GET'])
def list_models():
    pricing = query("SELECT model_name FROM pricing WHERE enabled=1 ORDER BY model_name")
    data = [{'id': p['model_name'], 'object': 'model', 'owned_by': 'moshu'} for p in pricing]
    return jsonify({'object': 'list', 'data': data})

# ── 核心：Chat Completions 代理 ──

@proxy.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    # 1. 验证 API Key
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'error': {'message': 'Missing API key', 'type': 'auth_error'}}), 401

    api_key = auth_header[7:]
    tk = query("SELECT * FROM tokens WHERE key=? AND status=1", (api_key,), one=True)
    if not tk:
        return jsonify({'error': {'message': 'Invalid API key', 'type': 'auth_error'}}), 401

    user_id = tk['user_id']
    token_id = tk['id']

    # 检查令牌额度
    if not tk['unlimited_quota'] and tk['remain_quota'] <= 0:
        return jsonify({'error': {'message': 'Token quota exhausted', 'type': 'quota_error'}}), 429

    # 检查令牌过期
    if tk['expired_time'] > 0 and tk['expired_time'] < time.time():
        return jsonify({'error': {'message': 'Token expired', 'type': 'auth_error'}}), 401

    # 检查用户状态
    user = query("SELECT * FROM users WHERE id=? AND status=1", (user_id,), one=True)
    if not user:
        return jsonify({'error': {'message': 'User disabled', 'type': 'auth_error'}}), 403

    # 2. 解析请求
    body = request.get_json() or {}
    model_name = body.get('model', '')
    if not model_name:
        return jsonify({'error': {'message': 'Model not specified', 'type': 'invalid_request_error'}}), 400

    is_stream = body.get('stream', False)

    # 3. 获取定价
    pricing = get_pricing(model_name)

    # 4. 查找渠道
    channel = find_channel(model_name)
    if not channel:
        return jsonify({'error': {'message': f'Model {model_name} not available', 'type': 'invalid_request_error'}}), 404

    # 5. 模型名映射
    model_mapping = json.loads(channel.get('model_mapping', '{}'))
    upstream_model = model_mapping.get(model_name, model_name)

    # 6. 构建上游请求
    forward_body = dict(body)
    forward_body['model'] = upstream_model

    upstream_url = channel['base_url'].rstrip('/') + '/chat/completions'
    upstream_headers = {
        'Authorization': f"Bearer {channel['api_key']}",
        'Content-Type': 'application/json',
    }

    # 预检查用户余额（非流式可精确检查，流式则预估算）
    if user['quota'] <= 0 and not (user['quota'] >= 2_000_000_000):
        return jsonify({'error': {'message': 'Insufficient balance', 'type': 'quota_error'}}), 429

    request_id = str(uuid.uuid4())[:16]
    start_time = time.time()
    client_ip = request.headers.get('X-Real-IP', request.remote_addr or '')

    # 7. 转发请求
    try:
        if is_stream:
            return _handle_stream(
                upstream_url, upstream_headers, forward_body, PROXY_TIMEOUT,
                user_id, token_id, model_name, channel['id'],
                pricing, start_time, request_id, client_ip
            )
        else:
            return _handle_sync(
                upstream_url, upstream_headers, forward_body, PROXY_TIMEOUT,
                user_id, token_id, model_name, channel['id'],
                pricing, start_time, request_id, client_ip
            )
    except http.exceptions.Timeout:
        _log_request(user_id, token_id, model_name, channel['id'], 0, 0, 0,
                     time.time() - start_time, 0, 'upstream timeout', request_id, client_ip)
        return jsonify({'error': {'message': 'Upstream timeout', 'type': 'upstream_error'}}), 504
    except Exception as e:
        _log_request(user_id, token_id, model_name, channel['id'], 0, 0, 0,
                     time.time() - start_time, 0, str(e), request_id, client_ip)
        return jsonify({'error': {'message': str(e), 'type': 'upstream_error'}}), 502


def _handle_sync(upstream_url, headers, body, timeout,
                 user_id, token_id, model_name, channel_id,
                 pricing, start_time, request_id, client_ip):
    """非流式请求"""
    resp = http.post(upstream_url, headers=headers, json=body, timeout=timeout)
    elapsed = time.time() - start_time

    if resp.status_code != 200:
        _log_request(user_id, token_id, model_name, channel_id, 0, 0, 0,
                     elapsed, 0, f'upstream {resp.status_code}', request_id, client_ip)
        return jsonify({'error': {'message': f'Upstream error {resp.status_code}', 'type': 'upstream_error'}}), resp.status_code

    data = resp.json()
    usage = data.get('usage', {})
    pt = usage.get('prompt_tokens', 0)
    ct = usage.get('completion_tokens', 0)

    # 计费
    quota_cost = calc_quota(pt, ct, pricing['input_price'], pricing['output_price'])
    deduct_user(user_id, quota_cost)
    deduct_token(token_id, quota_cost)

    _log_request(user_id, token_id, model_name, channel_id, pt, ct, quota_cost,
                 elapsed, 0, '', request_id, client_ip)

    return jsonify(data)


def _handle_stream(upstream_url, headers, body, timeout,
                   user_id, token_id, model_name, channel_id,
                   pricing, start_time, request_id, client_ip):
    """流式请求"""
    resp = http.post(upstream_url, headers=headers, json=body, timeout=timeout, stream=True)

    if resp.status_code != 200:
        elapsed = time.time() - start_time
        error_msg = resp.text[:500]
        _log_request(user_id, token_id, model_name, channel_id, 0, 0, 0,
                     elapsed, 1, f'upstream {resp.status_code}: {error_msg}', request_id, client_ip)
        return jsonify({'error': {'message': f'Upstream error {resp.status_code}', 'type': 'upstream_error'}}), resp.status_code

    def generate():
        total_content = ''
        usage_data = None
        try:
            for line in resp.iter_lines():
                if not line:
                    continue
                decoded = line.decode('utf-8') if isinstance(line, bytes) else line
                yield decoded + '\n\n'

                # 提取 usage 和内容
                if decoded.startswith('data: ') and decoded != 'data: [DONE]':
                    try:
                        chunk = json.loads(decoded[6:])
                        if 'usage' in chunk and chunk['usage']:
                            usage_data = chunk['usage']
                        for choice in chunk.get('choices', []):
                            delta = choice.get('delta', {})
                            if 'content' in delta:
                                total_content += delta['content']
                    except (json.JSONDecodeError, KeyError):
                        pass
        finally:
            # 流结束，计费和日志
            elapsed = time.time() - start_time
            pt = 0
            ct = 0
            if usage_data:
                pt = usage_data.get('prompt_tokens', 0)
                ct = usage_data.get('completion_tokens', 0)
            else:
                # 粗估：中文约 2 字符/token，英文约 4 字符/token
                ct = len(total_content) // 2 if total_content else 0

            quota_cost = calc_quota(pt, ct, pricing['input_price'], pricing['output_price'])
            deduct_user(user_id, quota_cost)
            deduct_token(token_id, quota_cost)
            _log_request(user_id, token_id, model_name, channel_id, pt, ct, quota_cost,
                         elapsed, 1, '', request_id, client_ip)

    return Response(
        stream_with_context(generate()),
        content_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


def _log_request(user_id, token_id, model_name, channel_id,
                 prompt_tokens, completion_tokens, quota,
                 use_time, is_stream, content, request_id, ip):
    """写入请求日志"""
    execute(
        "INSERT INTO logs (user_id, token_id, model_name, channel_id, prompt_tokens, completion_tokens, quota, use_time, is_stream, content, request_id, ip, type, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, token_id, model_name, channel_id,
         prompt_tokens, completion_tokens, quota,
         round(use_time, 2), is_stream, content, request_id, ip, 2, time.time())
    )
