import sys, os
sys.path.insert(0, "/home/guido/src/memorydb/src")
import warnings; warnings.simplefilter("ignore")

from memorydb.store import Store
from memorydb.vector import SqliteVecIndex, BruteForceVectorIndex, normalize

store = Store(":memory:")
# create some nodes so the JOIN to nodes works
store.conn.execute("CREATE TABLE IF NOT EXISTS _ignore_dummy(x)")  # noop

# Inspect schema: need nodes table with id, uid, type
cols = store.conn.execute("PRAGMA table_info(nodes)").fetchall()
print("nodes cols:", [c[1] for c in cols])

idx = SqliteVecIndex(store, dim=4)
brute = BruteForceVectorIndex(store)

# Insert N nodes + embeddings
import array
def pack(v): return array.array("f", v).tobytes()

N = 10
for i in range(N):
    store.conn.execute("INSERT INTO nodes(uid, type, name) VALUES(?,?,?)", (f"u{i}", "func", f"n{i}"))
    nid = store.conn.execute("SELECT id FROM nodes WHERE uid=?", (f"u{i}",)).fetchone()[0]
    v = normalize([float(i+1), 1.0, 0.0, 0.0])
    store.conn.execute("INSERT INTO embeddings(node_id, dim, vector) VALUES(?,?,?)", (nid, 4, pack(v)))
    idx.upsert(nid, [float(i+1), 1.0, 0.0, 0.0])

q = [1.0, 1.0, 0.0, 0.0]

for k in [5, 1024, 1025, 2048]:
    try:
        ann = idx.search(q, k=k)
    except Exception as e:
        ann = f"EXC {type(e).__name__}: {e}"
    try:
        bf = brute.search(q, k=k)
    except Exception as e:
        bf = f"EXC {type(e).__name__}: {e}"
    print(f"k={k}: over={k*4} ANN={len(ann) if isinstance(ann,list) else ann}  BF={len(bf) if isinstance(bf,list) else bf}")

print("--- boundary ---")
for k in [1023, 1024, 1025]:
    ann = idx.search(q, k=k)
    print(f"k={k} over={k*4}: ANN={len(ann)}")
