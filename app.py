"""墨枢 MOSHU — 主入口"""
import os
from flask import Flask, send_from_directory
from flask_cors import CORS
from db import init_db
from auth import auth
from tokens import tokens
from channels import channels
from proxy import proxy

def create_app():
    app = Flask(__name__, static_folder=None)
    CORS(app, supports_credentials=True)

    # 初始化数据库
    init_db()

    # 注册蓝图
    app.register_blueprint(auth)
    app.register_blueprint(tokens)
    app.register_blueprint(channels)
    app.register_blueprint(proxy)

    # 前端门户（开发用，生产环境由 nginx 直接托管）
    @app.route('/')
    def portal():
        portal_dir = os.path.join(os.path.dirname(__file__), 'portal')
        if os.path.exists(os.path.join(portal_dir, 'index.html')):
            return send_from_directory(portal_dir, 'index.html')
        return 'MOSHU API Gateway', 200

    return app

if __name__ == '__main__':
    app = create_app()
    port = int(os.environ.get('PORT', 3001))
    app.run(host='0.0.0.0', port=port, debug=False)
