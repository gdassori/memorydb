import sys, warnings
sys.path.insert(0, "/home/guido/src/memorydb/src")
warnings.simplefilter("ignore")
import array
from memorydb.store import Store
from memorydb.vector import SqliteVecIndex, BruteForceVectorIndex, normalize

store = Store(":memory:")
idx = SqliteVecIndex(store, dim=4)
brute = BruteForceVectorIndex(store)

def pack(v): return array.array("f", v).tobytes()

# 5000 rows of "common" type close to the query, 1 "rare" row far from the query
# query = [1,0,0,0]. common rows are near [1,0,0,0]; rare row is at [0,0,0,1] (far / orthogonal-ish)
N = 5000

for i in range(N):
    store.conn.execute("INSERT INTO nodes(uid, type, name) VALUES(?,?,?)", (f"c{i}", "common", f"c{i}"))
    nid = store.conn.execute("SELECT id FROM nodes WHERE uid=?", (f"c{i}",)).fetchone()[0]
    # near the query but slightly varied so they rank ahead of the rare row
    v = [1.0, 0.001*(i+1), 0.0, 0.0]
    store.conn.execute("INSERT INTO embeddings(node_id, dim, vector) VALUES(?,?,?)", (nid, 4, pack(normalize(v))))
    idx.upsert(nid, v)
# the rare row, far from query so it ranks LAST (past 4096)
store.conn.execute("INSERT INTO nodes(uid, type, name) VALUES(?,?,?)", ("rare0", "rare", "rare0"))
rid = store.conn.execute("SELECT id FROM nodes WHERE uid=?", ("rare0",)).fetchone()[0]
rv = [0.0, 0.0, 0.0, 1.0]
store.conn.execute("INSERT INTO embeddings(node_id, dim, vector) VALUES(?,?,?)", (rid, 4, pack(normalize(rv))))
idx.upsert(rid, rv)
store.conn.commit()

q = [1.0, 0.0, 0.0, 0.0]
# rare type, k=2: escalation grows over from 8 -> ... -> past 4096
ann = idx.search(q, k=2, types=["rare"])
bf = brute.search(q, k=2, types=["rare"])
print(f"types=[rare] k=2: ANN={ann}  BF={bf}")
# also show what k the escalation reaches
