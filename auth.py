"""墨枢 MOSHU — 用户认证"""
import time, jwt
from flask import Blueprint, request, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash

def verify_password(pw_hash, password):
    """兼容 New API 的 bcrypt 哈希和 werkzeug 哈希"""
    if pw_hash.startswith('$2a$') or pw_hash.startswith('$2b$'):
        import bcrypt
        return bcrypt.checkpw(password.encode('utf-8'), pw_hash.encode('utf-8'))
    return check_password_hash(pw_hash, password)
from db import query, execute
from config import JWT_SECRET, JWT_EXPIRE

auth = Blueprint('auth', __name__)

def make_token(user_id, role):
    return jwt.encode({'uid': user_id, 'role': role, 'exp': time.time() + JWT_EXPIRE * 3600},
                       JWT_SECRET, algorithm='HS256')

def verify_token(token):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

def login_required(f):
    """装饰器：验证 JWT cookie 或 Authorization header，并检查用户状态"""
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        # 1. JWT cookie
        token = request.cookies.get('moshu_token')
        payload = None
        if token:
            payload = verify_token(token)
        # 2. Authorization: Bearer <jwt>
        if not payload:
            auth_header = request.headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                payload = verify_token(auth_header[7:])
        if not payload:
            return jsonify({'success': False, 'message': '未登录或会话已过期'}), 401
        # 检查用户是否被禁用
        user = query("SELECT status, role FROM users WHERE id=?", (payload['uid'],), one=True)
        if not user or user['status'] != 1:
            return jsonify({'success': False, 'message': '账号已被禁用'}), 403
        g.user_id = payload['uid']
        g.user_role = user['role']
        return f(*args, **kwargs)
    return wrapper

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        res = login_required(f)(*args, **kwargs)
        if isinstance(res, tuple) and res[1] == 401:
            return res
        if g.get('user_role') != 'admin':
            return jsonify({'success': False, 'message': '需要管理员权限'}), 403
        return res
    return wrapper

# ── 路由 ──

@auth.route('/api/user/login', methods=['POST'])
def login():
    d = request.get_json() or {}
    username = (d.get('username') or '').strip()
    password = d.get('password') or ''
    if not username or not password:
        return jsonify({'success': False, 'message': '请输入用户名和密码'}), 400

    user = query("SELECT * FROM users WHERE username=?", (username,), one=True)
    if not user or not verify_password(user['password_hash'], password):
        return jsonify({'success': False, 'message': '用户名或密码错误'}), 401
    if user['status'] != 1:
        return jsonify({'success': False, 'message': '该账户已被管理员禁用'}), 403

    token = make_token(user['id'], user['role'])
    resp = jsonify({'success': True, 'message': '登录成功'})
    resp.set_cookie('moshu_token', token, max_age=JWT_EXPIRE*3600, httponly=True, samesite='Lax')
    return resp

@auth.route('/api/user/register', methods=['POST'])
def register():
    d = request.get_json() or {}
    username = (d.get('username') or '').strip()
    password = d.get('password') or ''
    if not username or len(username) < 3:
        return jsonify({'success': False, 'message': '用户名至少 3 个字符'}), 400
    if len(password) < 8:
        return jsonify({'success': False, 'message': '密码至少 8 位'}), 400

    existing = query("SELECT id FROM users WHERE username=?", (username,), one=True)
    if existing:
        return jsonify({'success': False, 'message': '用户名已存在'}), 409

    uid = execute(
        "INSERT INTO users (username, password_hash, role, quota, status, created_at) VALUES (?,?,?,?,?,?)",
        (username, generate_password_hash(password), 'user', 0, 1, time.time())
    )
    token = make_token(uid, 'user')
    resp = jsonify({'success': True, 'message': '注册成功'})
    resp.set_cookie('moshu_token', token, max_age=JWT_EXPIRE*3600, httponly=True, samesite='Lax')
    return resp

@auth.route('/api/user/logout', methods=['POST'])
def logout():
    resp = jsonify({'success': True})
    resp.set_cookie('moshu_token', '', max_age=0)
    return resp

@auth.route('/api/user/self', methods=['GET'])
@login_required
def user_self():
    user = query("SELECT * FROM users WHERE id=?", (g.user_id,), one=True)
    if not user:
        return jsonify({'success': False, 'message': '用户不存在'}), 404
    return jsonify({'success': True, 'data': {
        'id': user['id'],
        'username': user['username'],
        'display_name': user['display_name'],
        'role': user['role'],
        'quota': user['quota'],
        'used_quota': user['used_quota'],
        'status': user['status'],
        'unlimited_quota': user['quota'] >= 2_000_000_000,
    }})

@auth.route('/api/user/self', methods=['PUT'])
@login_required
def update_self():
    d = request.get_json() or {}
    user = query("SELECT * FROM users WHERE id=?", (g.user_id,), one=True)
    if not user:
        return jsonify({'success': False, 'message': '用户不存在'}), 404

    updates = []
    params = []

    if 'display_name' in d:
        updates.append("display_name=?")
        params.append(d['display_name'])

    if 'password' in d:
        old_pw = d.get('old_password', '')
        if not verify_password(user['password_hash'], old_pw):
            return jsonify({'success': False, 'message': '原密码错误'}), 400
        if len(d['password']) < 8:
            return jsonify({'success': False, 'message': '新密码至少 8 位'}), 400
        updates.append("password_hash=?")
        params.append(generate_password_hash(d['password']))

    if updates:
        params.append(g.user_id)
        execute(f"UPDATE users SET {','.join(updates)} WHERE id=?", params)

    return jsonify({'success': True, 'message': '更新成功'})

# ── 管理员用户管理 ──

@auth.route('/api/user/', methods=['GET'])
@admin_required
def list_users():
    page = int(request.args.get('p', 1))
    page_size = int(request.args.get('page_size', 50))
    offset = (page - 1) * page_size
    items = query(
        "SELECT id, username, display_name, role, quota, used_quota, status, created_at FROM users ORDER BY id ASC LIMIT ? OFFSET ?",
        (page_size, offset)
    )
    total = query("SELECT COUNT(*) as c FROM users", one=True)
    return jsonify({'success': True, 'data': {
        'items': items,
        'total': total['c'] if total else 0,
    }})

@auth.route('/api/user/<int:uid>', methods=['PUT'])
@admin_required
def admin_update_user(uid):
    d = request.get_json() or {}
    user = query("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not user:
        return jsonify({'success': False, 'message': '用户不存在'}), 404

    updates = []
    params = []

    if 'quota' in d:
        updates.append("quota=?")
        params.append(int(d['quota']))
    if 'status' in d:
        updates.append("status=?")
        params.append(int(d['status']))
    if 'display_name' in d:
        updates.append("display_name=?")
        params.append(d['display_name'])
    if 'role' in d and d['role'] in ('admin', 'user'):
        updates.append("role=?")
        params.append(d['role'])

    if updates:
        params.append(uid)
        execute(f"UPDATE users SET {','.join(updates)} WHERE id=?", params)

    return jsonify({'success': True, 'message': '已更新'})

@auth.route('/api/user/<int:uid>', methods=['DELETE'])
@admin_required
def admin_delete_user(uid):
    if uid == g.user_id:
        return jsonify({'success': False, 'message': '不能删除自己'}), 400
    execute("DELETE FROM tokens WHERE user_id=?", (uid,))
    execute("DELETE FROM users WHERE id=?", (uid,))
    return jsonify({'success': True, 'message': '已删除'})
