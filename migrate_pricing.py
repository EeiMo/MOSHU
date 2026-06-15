"""墨枢 MOSHU — 迁移定价表：ratio → price"""
import sqlite3, sys

DB = sys.argv[1] if len(sys.argv) > 1 else '/root/moshu/backend/data/moshu.db'
print(f'[MOSHU] 迁移定价表: {DB}')

conn = sqlite3.connect(DB)

# 检查是否还是旧结构
try:
    conn.execute("SELECT model_ratio FROM pricing LIMIT 1")
    has_ratio = True
except sqlite3.OperationalError:
    has_ratio = False

if not has_ratio:
    print('定价表已是新结构，跳过')
    conn.close()
    sys.exit(0)

# 读取旧数据
rows = conn.execute("SELECT model_name, model_ratio, completion_ratio FROM pricing").fetchall()
print(f'找到 {len(rows)} 条定价记录')

# 转换：ratio → price
# input_price = model_ratio * 0.002 * 1000 (group_ratio=1)
# output_price = model_ratio * completion_ratio * 0.002 * 1000
BK = 0.002 * 1000
for mn, mr, cr in rows:
    ip = mr * BK
    op = mr * (cr or 1) * BK
    print(f'  {mn}: ratio={mr}/{cr} → ¥{ip:.4f} / ¥{op:.4f}')

# 重建表
conn.execute("DROP TABLE pricing")
conn.execute("""
    CREATE TABLE pricing (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        model_name       TEXT UNIQUE NOT NULL,
        input_price      REAL DEFAULT 0,
        output_price     REAL DEFAULT 0,
        enabled          INTEGER DEFAULT 1,
        created_at       REAL
    )
""")
for mn, mr, cr in rows:
    ip = mr * BK
    op = mr * (cr or 1) * BK
    conn.execute(
        "INSERT INTO pricing (model_name, input_price, output_price, enabled, created_at) VALUES (?,?,?,?,1)",
        (mn, ip, op, 0)
    )
conn.commit()
print(f'迁移完成，{len(rows)} 条记录已转换')
conn.close()
