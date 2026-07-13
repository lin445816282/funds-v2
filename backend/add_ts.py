import sqlite3
import os

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'funds-v2.db')
db = sqlite3.connect(DB)
cols = [r[1] for r in db.execute('PRAGMA table_info(order_amounts)').fetchall()]
for col in ['created_at', 'updated_at']:
    if col not in cols:
        db.execute(f'ALTER TABLE order_amounts ADD COLUMN {col} TEXT')
        print(f'added {col}')
    else:
        print(f'{col} exists')
db.commit()
db.close()
print('done')
