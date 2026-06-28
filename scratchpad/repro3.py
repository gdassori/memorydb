import sys, os, math, random, sqlite3
sys.path.insert(0, os.path.join(os.getcwd(), "src"))
import sqlite_vec
from memorydb.vector import normalize, pack, unpack, SqliteVecIndex, BruteForceVectorIndex

class FakeStore:
    def __init__(self):
        c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; self.conn=c
        c.execute("CREATE TABLE nodes(id INTEGER PRIMARY KEY, uid TEXT, type TEXT)")
        c.execute("CREATE TABLE embeddings(node_id INTEGER PRIMARY KEY, dim INTEGER, vector BLOB)")
        self._meta={}
    def get_meta(self,k): return self._meta.get(k)
    def set_meta(self,k,v): self._meta[k]=v

def dot(a,b): return sum(x*y for x,y in zip(a,b))
def orthogonal_pair(dim,rng):
    a=[rng.gauss(0,1) for _ in range(dim)]; na=math.sqrt(dot(a,a)); a=[x/na for x in a]
    b=[rng.gauss(0,1) for _ in range(dim)]; proj=dot(b,a)
    b=[x-proj*ai for x,ai in zip(b,a)]; nb=math.sqrt(dot(b,b)); b=[x/nb for x in b]
    return a,b

F = 1e-9
for dim in (768, 1536, 3072):
    rng=random.Random(7); trials=300
    bf_scores=[]; ann_scores=[]
    bf_leak=0; ann_leak=0
    for t in range(trials):
        a,b=orthogonal_pair(dim,rng)
        store=FakeStore()
        store.conn.execute("INSERT INTO nodes(id,uid,type) VALUES(1,'u1','function')")
        an=normalize(a)
        store.conn.execute("INSERT INTO embeddings(node_id,dim,vector) VALUES(1,?,?)",(dim,pack(an)))
        idx=SqliteVecIndex(store,dim=dim); idx.upsert(1,a)
        bf=BruteForceVectorIndex(store)
        ann_s = idx.search(b,k=1)[0][0]
        bf_s = bf.search(b,k=1)[0][0]
        ann_scores.append(abs(ann_s)); bf_scores.append(abs(bf_s))
        if ann_s > F: ann_leak+=1
        if bf_s > F: bf_leak+=1
        store.conn.close()
    print(f"dim={dim}: ANN max|s|={max(ann_scores):.3e} leak>1e-9={ann_leak}/{trials} | BF max|s|={max(bf_scores):.3e} leak>1e-9={bf_leak}/{trials}")
