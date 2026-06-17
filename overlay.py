"""墨枢 MOSHU — 极薄适配层：转发到 New API (3000)，仅 token 创建写 DB"""
import os, json, sqlite3, secrets, time
from flask import Flask, Blueprint, request, jsonify, Response, stream_with_context
import requests as http

NEWAPI = 'http://127.0.0.1:3000'
NA_DB = os.environ.get('NEWAPI_DB', '/root/moshu/data/one-api.db')

bp = Blueprint('overlay', __name__)

# ── 需要特殊处理的路径前缀 ──
# token 创建走我们自己的逻辑（生成 key + 写 DB）
# 其余全部透传 New API，并注入 New-Api-User 头

def _forward(path, method='GET'):
    """把请求转发到 New API，透传请求头 + session cookie"""
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() not in ('host', 'content-length', 'connection', 'transfer-encoding')}
    url = NEWAPI + path
    resp = http.request(
        method, url,
        headers=fwd,
        cookies=request.cookies,
        params=request.args,
        data=request.get_data(),
        stream=False,
        timeout=900,
        allow_redirects=False,
    )
    return resp


@bp.route('/api/token/create', methods=['POST'])
def create_token():
    """通过 New API API 创建令牌（填充缓存），再从 DB 读出完整 key 返回"""
    d = request.get_json() or {}
    name = (d.get('name') or '').strip()
    uid = request.headers.get('New-Api-User') or request.cookies.get('new_api_uid')
    if not uid:
        return jsonify({'success': False, 'message': '未登录'}), 401
    if not name:
        return jsonify({'success': False, 'message': '令牌名不能为空'}), 400

    unlimited = bool(d.get('unlimited_quota', True))
    remain = int(d.get('remain_quota', 0))
    expired = int(d.get('expired_time', -1))

    # 1) 调 New API 创建（填充 token 缓存）
    headers = {'Content-Type': 'application/json', 'New-Api-User': uid}
    try:
        resp = http.post(NEWAPI + '/api/token/', headers=headers, cookies=request.cookies,
                         json={'name': name, 'remain_quota': remain,
                               'unlimited_quota': unlimited, 'expired_time': expired},
                         timeout=30)
        rj = resp.json()
        if not rj.get('success'):
            return jsonify({'success': False, 'message': rj.get('message', '创建失败')}), 400
    except Exception as e:
        return jsonify({'success': False, 'message': f'创建失败: {e}'}), 500

    # 2) 从 DB 读出刚创建令牌的完整 key（New API API 只返回掩码）
    try:
        conn = sqlite3.connect(NA_DB)
        row = conn.execute(
            'SELECT id, key FROM tokens WHERE user_id=? AND name=? '
            'ORDER BY id DESC LIMIT 1', (int(uid), name)).fetchone()
        conn.close()
        if not row:
            return jsonify({'success': False, 'message': '令牌已创建但读取 key 失败'}), 500
        tid, key = row[0], row[1]
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

    return jsonify({'success': True, 'data': {'id': tid, 'key': key, 'name': name}})


@bp.route('/api/token/<int:tid>/delete', methods=['DELETE'])
def delete_token(tid):
    """删除令牌：直接删 New API DB"""
    uid = request.headers.get('New-Api-User') or request.cookies.get('new_api_uid')
    if not uid:
        return jsonify({'success': False, 'message': '未登录'}), 401
    try:
        conn = sqlite3.connect(NA_DB)
        cur = conn.execute('DELETE FROM tokens WHERE id=? AND user_id=?', (tid, int(uid)))
        conn.commit()
        conn.close()
        if cur.rowcount == 0:
            return jsonify({'success': False, 'message': '令牌不存在'}), 404
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    return jsonify({'success': True})


@bp.route('/api/token/batch/keys', methods=['POST'])
def batch_keys():
    """批量取令牌完整 key（从 DB 直读，New API API 只给掩码）"""
    uid = request.headers.get('New-Api-User') or request.cookies.get('new_api_uid')
    if not uid:
        return jsonify({'success': False, 'message': '未登录'}), 401
    d = request.get_json() or {}
    ids = d.get('ids', [])
    keys = {}
    try:
        conn = sqlite3.connect(NA_DB)
        for tid in ids:
            row = conn.execute('SELECT key FROM tokens WHERE id=? AND user_id=?',
                               (int(tid), int(uid))).fetchone()
            if row:
                keys[str(tid)] = row[0]
        conn.close()
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    return jsonify({'success': True, 'data': {'keys': keys}})


def _restart_newapi():
    """重启 New API 容器刷新 channel/token 缓存"""
    import subprocess
    try:
        subprocess.run(['docker', 'restart', 'moshu-new-api'],
                       timeout=60, capture_output=True)
        return True
    except Exception:
        return False


@bp.route('/api/channel/fetch_models_from_url', methods=['POST'])
def fetch_models_from_url():
    """根据任意 URL+key 拉取模型列表（New API 没有此端点）"""
    d = request.get_json() or {}
    base_url = (d.get('base_url') or '').strip().rstrip('/')
    api_key = (d.get('api_key') or '').strip()
    if not base_url or not api_key:
        return jsonify({'success': False, 'message': '请先填写 Base URL 和 API Key'}), 400
    # 尝试多个 URL 变体
    urls = []
    if base_url.endswith('/v1'):
        urls.append(base_url + '/models')
    else:
        urls.append(base_url + '/v1/models')
        urls.append(base_url + '/models')
    last_err = ''
    for url in urls:
        try:
            resp = http.get(url, headers={'Authorization': 'Bearer ' + api_key}, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                models = [m.get('id', m['id']) if isinstance(m, dict) else str(m) for m in data.get('data', []) if (isinstance(m, dict) and m.get('id')) or isinstance(m, str)]
                return jsonify({'success': True, 'data': {'models': models}})
            last_err = f'上游返回 {resp.status_code}'
        except Exception as e:
            last_err = str(e)[:100]
    return jsonify({'success': False, 'message': last_err or '请求超时'}), 502


@bp.route('/api/channel/create', methods=['POST'])
def create_channel():
    """创建渠道：直接写 New API DB + abilities（POST API 有 panic bug），然后重启刷新缓存"""
    uid = request.headers.get('New-Api-User') or request.cookies.get('new_api_uid')
    if not uid:
        return jsonify({'success': False, 'message': '未登录'}), 401
    d = request.get_json() or {}
    name = (d.get('name') or '').strip()
    base_url = (d.get('base_url') or '').strip().rstrip('/')
    key = (d.get('api_key') or d.get('key') or '').strip()
    models = d.get('models', [])
    if isinstance(models, list):
        models_str = ','.join(models)
    else:
        models_str = str(models)
    ctype = int(d.get('type', 1))
    group = d.get('group', 'default')
    # New API 永远在 base_url 后拼 /v1/chat/completions，所以不能带 /v1
    base_url = base_url.rstrip('/')
    if base_url.endswith('/v1'):
        base_url = base_url[:-3].rstrip('/')
    if not name or not base_url or not key:
        return jsonify({'success': False, 'message': '名称、地址、密钥不能为空'}), 400

    now = int(time.time())
    try:
        conn = sqlite3.connect(NA_DB)
        # 所有 json 类型字段用 NULL，避免 GORM scan 空字符串报 unexpected end of JSON input
        cur = conn.execute(
            'INSERT INTO channels (type, key, status, name, weight, created_time, test_time, '
            'response_time, base_url, other, balance, balance_updated_time, models, "group", '
            'used_quota, model_mapping, status_code_mapping, priority, auto_ban, other_info, '
            'tag, setting, param_override, header_override, remark, channel_info, settings) '
            'VALUES (?,?,?,?,?,?,?,?,?,NULL,0,0,?,?,0,NULL,NULL,0,1,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL)',
            (ctype, key, 1, name, 0, now, 0, 0, base_url, models_str, group)
        )
        cid = cur.lastrowid
        # 写 abilities 表（每个模型一行）
        for m in [x.strip() for x in models_str.split(',') if x.strip()]:
            conn.execute(
                'INSERT OR REPLACE INTO abilities ("group", model, channel_id, enabled, priority, weight, tag) '
                'VALUES (?,?,?,?,?,?,?)',
                (group, m, cid, 1, 0, 0, ''))
        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({'success': False, 'message': f'创建失败: {e}'}), 500

    # abilities 表已写入，New API distributor 每次按需查 DB，无需重启
    return jsonify({'success': True, 'data': {'id': cid}, 'message': '渠道已创建'})


@bp.route('/v1/chat/completions', methods=['POST'])
def proxy_chat():
    """流式 API 透传到 New API（AI 工具用，需要 New API 的稳定流式）"""
    # 透传所有请求头（含 Authorization）
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() not in ('host', 'content-length', 'connection', 'transfer-encoding')}
    try:
        is_stream = (request.get_json(force=True) or {}).get('stream', False)
    except Exception:
        is_stream = False
    resp = http.post(NEWAPI + '/v1/chat/completions',
                     headers=fwd, data=request.get_data(),
                     stream=True, timeout=900)
    excl = {'transfer-encoding', 'connection', 'keep-alive', 'content-length',
            'content-encoding'}
    out_headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in excl]
    if is_stream:
        return Response(stream_with_context(resp.iter_content(chunk_size=4096)),
                        content_type=resp.headers.get('Content-Type', 'text/event-stream'),
                        headers=out_headers, status=resp.status_code)
    return Response(resp.content,
                    content_type=resp.headers.get('Content-Type', 'application/json'),
                    headers=out_headers, status=resp.status_code)


@bp.route('/v1/models', methods=['GET'])
def proxy_models():
    resp = _forward('/v1/models', 'GET')
    return Response(resp.content, content_type='application/json', status=resp.status_code)


def make_app():
    app = Flask(__name__)
    app.register_blueprint(bp)

    # 其余 /api/* 全部透传到 New API
    @app.route('/api/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
    def passthrough(subpath):
        full = '/api/' + subpath
        resp = _forward(full, request.method)
        excl = {'transfer-encoding', 'connection', 'keep-alive', 'content-length',
                'content-encoding'}
        out_headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in excl]
        out = Response(resp.content,
                       content_type=resp.headers.get('Content-Type', 'application/json'),
                       headers=out_headers, status=resp.status_code)
        # 透传 Set-Cookie（New API 的 session cookie）
        for cookie in resp.raw.headers.getlist('Set-Cookie'):
            out.headers.add('Set-Cookie', cookie)
        return out

    # 根路径返回前端
    @app.route('/')
    def index():
        from flask import send_from_directory
        portal = os.environ.get('MOSHU_PORTAL', '/root/moshu/portal')
        return send_from_directory(portal, 'index.html')

    # New API 原生后台 + 静态资源：/console, /console/*, /logo, /assets, /oauth 等全部透传
    @app.route('/console', defaults={'subpath': ''})
    @app.route('/console/<path:subpath>')
    def console(subpath=''):
        full = '/console' + ('/' + subpath if subpath else '')
        resp = http.request(request.method, NEWAPI + full,
                            headers={k: v for k, v in request.headers.items()
                                     if k.lower() not in ('host', 'content-length', 'connection', 'transfer-encoding')},
                            cookies=request.cookies, params=request.args,
                            data=request.get_data(), allow_redirects=False, timeout=300)
        excl = {'transfer-encoding', 'connection', 'keep-alive', 'content-length', 'content-encoding'}
        out_headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in excl]
        out = Response(resp.content,
                       content_type=resp.headers.get('Content-Type', 'text/html'),
                       headers=out_headers, status=resp.status_code)
        for cookie in resp.raw.headers.getlist('Set-Cookie'):
            out.headers.add('Set-Cookie', cookie)
        return out

    # 其余 New API 静态资源（/logo.png /assets /oauth 等）透传
    # 透传 /static/* 到 New API（console SPA 的 JS/CSS）
    @app.route('/static/<path:subpath>')
    def passthrough_static(subpath):
        resp = http.request(request.method, NEWAPI + '/static/' + subpath,
                            headers={k: v for k, v in request.headers.items()
                                     if k.lower() not in ('host', 'content-length', 'connection', 'transfer-encoding')},
                            cookies=request.cookies, params=request.args,
                            data=request.get_data(), allow_redirects=False, timeout=300)
        excl = {'transfer-encoding', 'connection', 'keep-alive', 'content-length', 'content-encoding'}
        out_h = [(k, v) for k, v in resp.headers.items() if k.lower() not in excl]
        return Response(resp.content,
                        content_type=resp.headers.get('Content-Type', 'application/octet-stream'),
                        headers=out_h, status=resp.status_code)

    @app.route('/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE'])
    def static_passthrough(subpath):
        if subpath in ('index.html', 'favicon.ico'):
            from flask import send_from_directory
            portal = os.environ.get('MOSHU_PORTAL', '/root/moshu/portal')
            try:
                return send_from_directory(portal, subpath)
            except Exception:
                pass
        resp = http.request(request.method, NEWAPI + '/' + subpath,
                            headers={k: v for k, v in request.headers.items()
                                     if k.lower() not in ('host', 'content-length', 'connection', 'transfer-encoding')},
                            cookies=request.cookies, params=request.args,
                            data=request.get_data(), allow_redirects=False, timeout=300)
        excl = {'transfer-encoding', 'connection', 'keep-alive', 'content-length', 'content-encoding'}
        out_headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in excl]
        out = Response(resp.content,
                       content_type=resp.headers.get('Content-Type', 'application/octet-stream'),
                       headers=out_headers, status=resp.status_code)
        for cookie in resp.raw.headers.getlist('Set-Cookie'):
            out.headers.add('Set-Cookie', cookie)
        return out

    return app


if __name__ == '__main__':
    make_app().run(host='127.0.0.1', port=3001)
