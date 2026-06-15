"""墨枢 MOSHU — 渠道管理（管理员）"""
import time, json
from flask import Blueprint, request, jsonify, g
from db import query, execute
from auth import admin_required

channels = Blueprint('channels', __name__)

@channels.route('/api/channel/', methods=['GET'])
@admin_required
def list_channels():
    items = query("SELECT * FROM channels ORDER BY priority DESC, id ASC")
    # 隐藏 api_key 的中间部分
    for ch in items:
        ak = ch.get('api_key', '')
        if len(ak) > 8:
            ch['api_key'] = ak[:4] + '***' + ak[-4:]
    return jsonify({'success': True, 'data': {'items': items}})

@channels.route('/api/channel/', methods=['POST'])
@admin_required
def create_channel():
    d = request.get_json() or {}
    name = (d.get('name') or '').strip()
    base_url = (d.get('base_url') or '').strip().rstrip('/')
    api_key = (d.get('api_key') or '').strip()
    models = d.get('models', [])
    model_mapping = d.get('model_mapping', {})
    priority = int(d.get('priority', 0))

    if not name or not base_url or not api_key:
        return jsonify({'success': False, 'message': '名称、地址、密钥不能为空'}), 400

    cid = execute(
        "INSERT INTO channels (name, type, base_url, api_key, models, model_mapping, status, priority, weight, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (name, d.get('type', 1), base_url, api_key,
         json.dumps(models), json.dumps(model_mapping),
         1, priority, d.get('weight', 1), time.time())
    )
    return jsonify({'success': True, 'data': {'id': cid}})

@channels.route('/api/channel/<int:cid>', methods=['PUT'])
@admin_required
def update_channel(cid):
    ch = query("SELECT * FROM channels WHERE id=?", (cid,), one=True)
    if not ch:
        return jsonify({'success': False, 'message': '渠道不存在'}), 404

    d = request.get_json() or {}
    updates = []
    params = []

    for field in ['name', 'type', 'base_url', 'api_key', 'status', 'priority', 'weight']:
        if field in d:
            val = d[field]
            if field == 'base_url':
                val = str(val).strip().rstrip('/')
            updates.append(f"{field}=?")
            params.append(val)

    if 'models' in d:
        updates.append("models=?")
        params.append(json.dumps(d['models']))
    if 'model_mapping' in d:
        updates.append("model_mapping=?")
        params.append(json.dumps(d['model_mapping']))

    if updates:
        params.append(cid)
        execute(f"UPDATE channels SET {','.join(updates)} WHERE id=?", params)

    return jsonify({'success': True, 'message': '已更新'})

@channels.route('/api/channel/<int:cid>', methods=['DELETE'])
@admin_required
def delete_channel(cid):
    execute("DELETE FROM channels WHERE id=?", (cid,))
    return jsonify({'success': True, 'message': '已删除'})

# ── 定价管理 ──

@channels.route('/api/pricing', methods=['GET'])
def get_pricing():
    """公开接口：获取所有模型定价"""
    items = query("SELECT model_name, input_price, output_price FROM pricing WHERE enabled=1 ORDER BY model_name")
    return jsonify({
        'success': True,
        'data': items,
        'group_ratio': {'default': 1},
    })

@channels.route('/api/pricing', methods=['POST'])
@admin_required
def set_pricing():
    """管理员：设置模型定价"""
    d = request.get_json() or {}
    model_name = (d.get('model_name') or '').strip()
    if not model_name:
        return jsonify({'success': False, 'message': '模型名不能为空'}), 400

    input_price = float(d.get('input_price', 0))
    output_price = float(d.get('output_price', 0))

    existing = query("SELECT id FROM pricing WHERE model_name=?", (model_name,), one=True)
    if existing:
        execute(
            "UPDATE pricing SET input_price=?, output_price=?, enabled=? WHERE model_name=?",
            (input_price, output_price, d.get('enabled', 1), model_name)
        )
    else:
        execute(
            "INSERT INTO pricing (model_name, input_price, output_price, enabled, created_at) VALUES (?,?,?,?,?)",
            (model_name, input_price, output_price, 1, time.time())
        )
    return jsonify({'success': True, 'message': '定价已更新'})

@channels.route('/api/pricing/<model_name>', methods=['DELETE'])
@admin_required
def delete_pricing(model_name):
    execute("DELETE FROM pricing WHERE model_name=?", (model_name,))
    return jsonify({'success': True, 'message': '已删除'})

@channels.route('/api/channel/fetch_models', methods=['POST'])
@admin_required
def fetch_models():
    """代理：用渠道的 base_url + api_key 拉取 /v1/models"""
    import requests as http
    d = request.get_json() or {}
    base_url = (d.get('base_url') or '').strip().rstrip('/')
    api_key = (d.get('api_key') or '').strip()
    if not base_url or not api_key:
        return jsonify({'success': False, 'message': '请先填写 Base URL 和 API Key'}), 400

    try:
        url = base_url + '/models'
        resp = http.get(url, headers={'Authorization': f'Bearer {api_key}'}, timeout=15)
        if resp.status_code != 200:
            return jsonify({'success': False, 'message': f'上游返回 {resp.status_code}'}), 502
        data = resp.json()
        models = [m['id'] for m in data.get('data', []) if m.get('id')]
        return jsonify({'success': True, 'data': {'models': models}})
    except http.exceptions.Timeout:
        return jsonify({'success': False, 'message': '请求超时'}), 504
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)[:200]}), 500
