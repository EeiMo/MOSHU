"""墨枢 MOSHU — 令牌管理"""
import time, secrets, json
from flask import Blueprint, request, jsonify, g
from db import query, execute
from auth import login_required

tokens = Blueprint('tokens', __name__)

def generate_key():
    return 'sk-' + secrets.token_urlsafe(32)

@tokens.route('/api/token/', methods=['GET'])
@login_required
def list_tokens():
    page = int(request.args.get('p', 1))
    page_size = int(request.args.get('page_size', 50))
    offset = (page - 1) * page_size

    items = query(
        "SELECT * FROM tokens WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (g.user_id, page_size, offset)
    )
    # 脱敏密钥
    for t in items:
        k = t['key']
        if k.startswith('sk-') and len(k) > 8:
            t['key'] = k[:5] + '***' + k[-4:]

    total = query("SELECT COUNT(*) as c FROM tokens WHERE user_id=?", (g.user_id,), one=True)
    return jsonify({'success': True, 'data': {
        'items': items,
        'total': total['c'] if total else 0,
        'page': page,
        'page_size': page_size,
    }})

@tokens.route('/api/token/', methods=['POST'])
@login_required
def create_token():
    d = request.get_json() or {}
    name = (d.get('name') or '').strip() or 'default'
    key = generate_key()
    remain_quota = int(d.get('remain_quota', 0))
    unlimited = 1 if remain_quota <= 0 else 0
    expired_time = int(d.get('expired_time', 0))

    tid = execute(
        "INSERT INTO tokens (user_id, name, key, status, remain_quota, unlimited_quota, expired_time, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (g.user_id, name, key, 1, remain_quota, unlimited, expired_time, time.time())
    )
    return jsonify({'success': True, 'data': {'id': tid, 'key': key, 'name': name}})

@tokens.route('/api/token/<int:tid>', methods=['DELETE'])
@login_required
def delete_token(tid):
    tk = query("SELECT * FROM tokens WHERE id=? AND user_id=?", (tid, g.user_id), one=True)
    if not tk:
        return jsonify({'success': False, 'message': '令牌不存在'}), 404
    execute("DELETE FROM tokens WHERE id=?", (tid,))
    return jsonify({'success': True, 'message': '已删除'})

@tokens.route('/api/token/batch/keys', methods=['POST'])
@login_required
def batch_keys():
    """批量获取完整密钥（仅限本人令牌）"""
    d = request.get_json() or {}
    ids = d.get('ids', [])
    if not ids:
        return jsonify({'success': True, 'data': {'keys': {}}})

    keys = {}
    for tid in ids:
        tk = query("SELECT key FROM tokens WHERE id=? AND user_id=?", (int(tid), g.user_id), one=True)
        if tk:
            keys[str(tid)] = tk['key']

    return jsonify({'success': True, 'data': {'keys': keys}})
