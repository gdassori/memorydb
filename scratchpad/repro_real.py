import sys, math, random
sys.path.insert(0, "src")
from memorydb.api import MemoryDB

# A realistic high-dim embedder: deterministic hashing into 1536 dims (semantically meaningful overlap).
import hashlib, re
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+")
class Hash1536:
    model = "hash-1536"
    def __init__(self, dim=1536): self.dim = dim
    def embed(self, texts):
        out=[]
        for t in texts:
            v=[0.0]*self.dim
            for tok in _IDENT.findall(t or ""):
                v[int(hashlib.md5(tok.lower().encode()).hexdigest(),16)%self.dim]+=1.0
            n=math.sqrt(sum(x*x for x in v)) or 1.0
            out.append([x/n for x in v])
        return out

import warnings; warnings.filterwarnings("ignore")
db = MemoryDB.open(":memory:", embedder=Hash1536(1536))
print("index:", type(db._planner.index).__name__)
# How often do two UNRELATED short texts (disjoint tokens) come out orthogonal AND leak a seed?
# With a hashing embedder, disjoint token sets -> exactly orthogonal (no shared bucket unless collision).
emb = Hash1536(1536)
leaks=0; trials=300; rng=random.Random(3)
words = [f"word{i}" for i in range(2000)]
from memorydb.vector import SqliteVecIndex
from memorydb.store import Store
for t in range(trials):
    st=Store(":memory:"); idx=SqliteVecIndex(st, dim=1536)
    qtok = rng.choice(words); btok = rng.choice([w for w in words if w!=qtok])
    qv = emb.embed([qtok])[0]; bv = emb.embed([btok])[0]
    cur=st.conn.execute("INSERT INTO nodes(uid,type,name) VALUES(?,?,?)",(f"u{t}","func","n"))
    idx.upsert(cur.lastrowid, bv)
    ann=idx.search(qv,k=5)
    if any(s>1e-9 for s,_ in ann): leaks+=1
    st.conn.close()
print(f"realistic hashing-embedder, disjoint single-token docs, dim 1536: ANN junk-seed leaks {leaks}/{trials}")
