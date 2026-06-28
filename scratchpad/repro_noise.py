import sys, math, random
sys.path.insert(0, "src")
from memorydb.store import Store
from memorydb.vector import SqliteVecIndex, normalize, _EPS, BruteForceVectorIndex, pack

def random_orthogonal_pair(dim, rng):
    a = [rng.gauss(0,1) for _ in range(dim)]
    na = math.sqrt(sum(x*x for x in a)); a = [x/na for x in a]
    b = [rng.gauss(0,1) for _ in range(dim)]
    dot = sum(x*y for x,y in zip(a,b))
    b = [y - dot*x for x,y in zip(a,b)]
    nb = math.sqrt(sum(x*x for x in b)); b = [y/nb for y in b]
    return a, b

def test_dim(dim, trials=40, seed=1):
    rng = random.Random(seed)
    worst_ann = 0.0; worst_brute = 0.0; ann_leaked = 0; brute_leaked = 0
    for t in range(trials):
        st = Store(":memory:")
        idx = SqliteVecIndex(st, dim=dim)
        brute = BruteForceVectorIndex(st)
        a, b = random_orthogonal_pair(dim, rng)
        cur = st.conn.execute("INSERT INTO nodes(uid, type, name) VALUES(?,?,?)", (f"u{t}", "func", "n"))
        nid = cur.lastrowid
        idx.upsert(nid, b)
        nb = normalize(b)
        st.conn.execute("INSERT INTO embeddings(node_id, dim, vector) VALUES(?,?,?)", (nid, dim, pack(nb)))
        ann = idx.search(a, k=5)
        bf = brute.search(a, k=5)
        for s, n in ann:
            worst_ann = max(worst_ann, abs(s))
            if s > 1e-9: ann_leaked += 1
        for s, n in bf:
            worst_brute = max(worst_brute, abs(s))
            if s > 1e-9: brute_leaked += 1
        st.conn.close()
    return worst_ann, worst_brute, ann_leaked, brute_leaked

for dim in (256, 384, 768, 1024, 1536):
    wa, wb, al, bl = test_dim(dim)
    print(f"dim={dim:5d}  worst|ANN|={wa:.3e}  worst|brute|={wb:.3e}  _EPS={_EPS:.0e}  ANN_leaked={al}  BRUTE_leaked={bl}")
