import sqlite3, sqlite_vec

conn = sqlite3.connect(":memory:")
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)

conn.execute("CREATE VIRTUAL TABLE vec_items USING vec0(node_id integer primary key, embedding float[4])")
# insert a few rows
for i in range(10):
    v = [float(i), 0.0, 0.0, 1.0]
    conn.execute("INSERT INTO vec_items(node_id, embedding) VALUES(?, ?)", (i, sqlite_vec.serialize_float32(v)))

q = sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0])

for k in [10, 1024, 1025, 4096, 4097, 8192]:
    try:
        rows = conn.execute("SELECT node_id, distance FROM vec_items WHERE embedding MATCH ? AND k = ? ORDER BY distance", (q, k)).fetchall()
        print(f"k={k}: OK, {len(rows)} rows")
    except sqlite3.OperationalError as e:
        print(f"k={k}: OperationalError: {e}")
