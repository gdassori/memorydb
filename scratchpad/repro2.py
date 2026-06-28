import sys, os, math, random, sqlite3
sys.path.insert(0, os.path.join(os.getcwd(), "src"))
import sqlite_vec
from memorydb.vector import normalize, pack, unpack, SqliteVecIndex, BruteForceVectorIndex

# Minimal fake Store: just needs .conn, get_meta/set_meta, and a nodes table for the join.
class FakeStore:
    def __init__(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        self.conn = c
        c.execute("CREATE TABLE nodes(id INTEGER PRIMARY KEY, uid TEXT, type TEXT)")
        c.execute("CREATE TABLE embeddings(node_id INTEGER PRIMARY KEY, dim INTEGER, vector BLOB)")
        self._meta = {}
    def get_meta(self, k): return self._meta.get(k)
    def set_meta(self, k, v): self._meta[k] = v

def dot(a, b): return sum(x*y for x, y in zip(a, b))

def orthogonal_pair(dim, rng):
    a = [rng.gauss(0,1) for _ in range(dim)]; na=math.sqrt(dot(a,a)); a=[x/na for x in a]
    b = [rng.gauss(0,1) for _ in range(dim)]; proj=dot(b,a)
    b = [x-proj*ai for x,ai in zip(b,a)]; nb=math.sqrt(dot(b,b)); b=[x/nb for x in b]
    return a, b

PLANNER_FILTER = 1e-9

for dim in (768, 1536, 3072):
    rng = random.Random(7)
    spurious_ann_seeds = 0
    spurious_bf_seeds = 0
    trials = 200
    for t in range(trials):
        a, b = orthogonal_pair(dim, rng)
        store = FakeStore()
        store.conn.execute("INSERT INTO nodes(id, uid, type) VALUES(1, 'u1', 'function')")
        an = normalize(a)
        store.conn.execute("INSERT INTO embeddings(node_id, dim, vector) VALUES(1, ?, ?)", (dim, pack(an)))
        idx = SqliteVecIndex(store, dim=dim)
        idx.upsert(1, a)
        bf = BruteForceVectorIndex(store)
        # ANN seeds
        ann = [(s, nid) for s, nid in idx.search(b, k=5) if s > PLANNER_FILTER]
        bfr = [(s, nid) for s, nid in bf.search(b, k=5) if s > PLANNER_FILTER]
        if ann: spurious_ann_seeds += 1
        if bfr: spurious_bf_seeds += 1
        store.conn.close()
    print(f"dim={dim}: ANN produced spurious seed in {spurious_ann_seeds}/{trials} trials; BF in {spurious_bf_seeds}/{trials}")
