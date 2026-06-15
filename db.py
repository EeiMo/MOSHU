"""墨枢 MOSHU — 数据库初始化与连接"""
import sqlite3, os, time
from config import DB_PATH, ADMIN_USER, ADMIN_PASS
from werkzeug.security import generate_password_hash

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def query(sql, args=(), one=False):
    conn = get_db()
    try:
        cur = conn.execute(sql, args)
        if one:
            row = cur.fetchone()
            return dict(row) if row else None
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

def execute(sql, args=()):
    conn = get_db()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name  TEXT DEFAULT '',
        role          TEXT DEFAULT 'user',
        quota         INTEGER DEFAULT 0,
        used_quota    INTEGER DEFAULT 0,
        status        INTEGER DEFAULT 1,
        created_at    REAL
    );
    CREATE TABLE IF NOT EXISTS tokens (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL,
        name            TEXT NOT NULL,
        key             TEXT UNIQUE NOT NULL,
        status          INTEGER DEFAULT 1,
        remain_quota    INTEGER DEFAULT 0,
        unlimited_quota INTEGER DEFAULT 1,
        expired_time    INTEGER DEFAULT 0,
        created_at      REAL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS channels (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        type          INTEGER DEFAULT 1,
        base_url      TEXT NOT NULL,
        api_key       TEXT NOT NULL,
        models        TEXT DEFAULT '[]',
        model_mapping TEXT DEFAULT '{}',
        status        INTEGER DEFAULT 1,
        priority      INTEGER DEFAULT 0,
        weight        INTEGER DEFAULT 1,
        created_at    REAL
    );
    CREATE TABLE IF NOT EXISTS pricing (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        model_name       TEXT UNIQUE NOT NULL,
        model_ratio      REAL DEFAULT 1,
        completion_ratio REAL DEFAULT 1,
        enabled          INTEGER DEFAULT 1,
        created_at       REAL
    );
    CREATE TABLE IF NOT EXISTS logs (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id          INTEGER NOT NULL,
        token_id         INTEGER DEFAULT 0,
        model_name       TEXT DEFAULT '',
        channel_id       INTEGER DEFAULT 0,
        prompt_tokens    INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0,
        quota            INTEGER DEFAULT 0,
        use_time         REAL DEFAULT 0,
        is_stream        INTEGER DEFAULT 0,
        content          TEXT DEFAULT '',
        ip               TEXT DEFAULT '',
        request_id       TEXT DEFAULT '',
        type             INTEGER DEFAULT 2,
        created_at       REAL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    """)
    conn.commit()

    # 创建管理员（首次）
    cur = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'")
    if cur.fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, quota, status, created_at) VALUES (?,?,?,?,?,?)",
            (ADMIN_USER, generate_password_hash(ADMIN_PASS), 'admin', 0, 1, time.time())
        )
        conn.commit()
        print(f'[MOSHU] 管理员已创建: {ADMIN_USER}')

    conn.close()
