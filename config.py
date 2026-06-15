"""墨枢 MOSHU — 配置"""
import os

# ── 数据库 ──
DB_PATH = os.environ.get('MOSHU_DB', 'data/moshu.db')

# ── JWT ──
JWT_SECRET = os.environ.get('JWT_SECRET', 'moshu-change-me-in-production-2026-eeimoo')
JWT_EXPIRE = 72  # hours

# ── 计费 ──
QUOTA_PER_UNIT = 500_000    # 1 USD = 500,000 quota
USD_EXCHANGE_RATE = 7.3     # 1 USD = 7.3 CNY

# ── 管理员（首次启动自动创建）──
ADMIN_USER = os.environ.get('ADMIN_USER', 'eeimoo')
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'Aa12345678')

# ── 代理 ──
PROXY_TIMEOUT = 120  # 秒
