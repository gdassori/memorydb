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

N = 5000
for i in range(N):
    store.conn.execute("INSERT INTO nodes(uid, type, name) VALUES(?,?,?)", (f"c{i}", "common", f"c{i}"))
    nid = store.conn.execute("SELECT id FROM nodes WHERE uid=?", (f"c{i}",)).fetchone()[0]
    v = [1.0, 0.0001*(i+1), 0.0, 0.0]
    store.conn.execute("INSERT INTO embeddings(node_id, dim, vector) VALUES(?,?,?)", (nid, 4, pack(normalize(v))))
    idx.upsert(nid, v)
# rare row: far but NOT orthogonal -> positive score, survives the >1e-9 planner filter
store.conn.execute("INSERT INTO nodes(uid, type, name) VALUES(?,?,?)", ("rare0", "rare", "rare0"))
rid = store.conn.execute("SELECT id FROM nodes WHERE uid=?", ("rare0",)).fetchone()[0]
rv = [1.0, 5.0, 0.0, 0.0]   # cosine with [1,0,0,0] = 1/sqrt(26) ~ 0.196 (positive, far)
store.conn.execute("INSERT INTO embeddings(node_id, dim, vector) VALUES(?,?,?)", (rid, 4, pack(normalize(rv))))
idx.upsert(rid, rv)
store.conn.commit()

q = [1.0, 0.0, 0.0, 0.0]
ann = idx.search(q, k=2, types=["rare"])
bf = brute.search(q, k=2, types=["rare"])
print(f"non-ortho rare, k=2: ANN={ann}  BF={bf}")
