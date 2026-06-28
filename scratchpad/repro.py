import sys, os, math, random, sqlite3
sys.path.insert(0, os.path.join(os.getcwd(), "src"))
import sqlite_vec
from memorydb.vector import normalize, pack, unpack

def make_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)
    return c

serialize = sqlite_vec.serialize_float32

def dot(a, b):
    return sum(x*y for x, y in zip(a, b))

def orthogonal_pair(dim, rng):
    a = [rng.gauss(0, 1) for _ in range(dim)]
    na = math.sqrt(dot(a, a)); a = [x/na for x in a]
    b = [rng.gauss(0, 1) for _ in range(dim)]
    proj = dot(b, a)
    b = [x - proj*ai for x, ai in zip(b, a)]   # exactly orthogonal in float64
    nb = math.sqrt(dot(b, b)); b = [x/nb for x in b]
    return a, b, dot(a, b)

def f32(v):
    # round-trip a python float list through float32 like storage does
    return list(unpack(pack(v)))

for dim in (256, 768, 1536, 3072):
    rng = random.Random(42)
    trials = 300
    leaks = 0; worst = 0.0; true_cos_max = 0.0; worst_bf = 0.0
    for t in range(trials):
        a, b, cos = orthogonal_pair(dim, rng)
        true_cos_max = max(true_cos_max, abs(cos))
        c = make_conn()
        c.execute("CREATE VIRTUAL TABLE vec_items USING vec0(node_id integer primary key, embedding float[%d])" % dim)
        an = normalize(a); bn = normalize(b)
        c.execute("INSERT INTO vec_items(node_id, embedding) VALUES(1, ?)", (serialize(an),))
        row = c.execute("SELECT distance FROM vec_items WHERE embedding MATCH ? AND k=1 ORDER BY distance",
                        (serialize(bn),)).fetchone()
        d = row["distance"]
        s = 1.0 - (d*d)/2.0
        worst = max(worst, abs(s))
        if abs(s) >= 1e-6:
            leaks += 1
        # brute force equivalent: float64 dot over the float32-rounded STORED vector and the normalized query
        a_stored = f32(an)
        bf = dot(normalize(b), a_stored)
        worst_bf = max(worst_bf, abs(bf))
        c.close()
    print(f"dim={dim:5d}  true_cos_max={true_cos_max:.2e}  ANN worst|s|={worst:.3e}  leaks(>=1e-6)={leaks}/{trials}  BF worst|dot|={worst_bf:.3e}")
