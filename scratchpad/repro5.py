import sys, math, random
sys.path.insert(0, "src")
from memorydb.store import Store
from memorydb.vector import SqliteVecIndex, normalize, pack

# Simulate PRE-P5-4 (no clamp): the raw ANN score 1 - d^2/2.  Compare leak count to clamped.
def random_orthogonal_pair(dim, rng):
    a = [rng.gauss(0,1) for _ in range(dim)]
    na = math.sqrt(sum(x*x for x in a)); a = [x/na for x in a]
    b = [rng.gauss(0,1) for _ in range(dim)]
    dot = sum(x*y for x,y in zip(a,b))
    b = [y - dot*x for x,y in zip(a,b)]
    nb = math.sqrt(sum(x*x for x in b)); b = [y/nb for y in b]
    return a, b

for dim in (1024, 1536):
    rng = random.Random(99)
    raw_leaks = 0; clamped_leaks = 0; trials = 500
    for t in range(trials):
        st = Store(":memory:")
        idx = SqliteVecIndex(st, dim=dim)
        a, b = random_orthogonal_pair(dim, rng)
        cur = st.conn.execute("INSERT INTO nodes(uid,type,name) VALUES(?,?,?)", (f"u{t}","func","n"))
        nid = cur.lastrowid
        idx.upsert(nid, b)
        # raw distance straight from vec_items
        row = st.conn.execute(
            "SELECT v.distance d FROM vec_items v WHERE v.embedding MATCH ? AND k=1 ORDER BY v.distance",
            (idx._serialize(normalize(a)),)).fetchone()
        d = row["d"]; raw = 1.0 - d*d/2.0
        if raw > 1e-9: raw_leaks += 1
        # clamped (what search returns now)
        ann = idx.search(a, k=1)
        if ann and ann[0][0] > 1e-9: clamped_leaks += 1
        st.conn.close()
    print(f"dim={dim}: PRE-FIX(raw) leaks {raw_leaks}/{trials}  |  POST-FIX(clamped) leaks {clamped_leaks}/{trials}")
