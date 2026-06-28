import sys, os, tempfile, warnings
sys.path.insert(0, "/home/guido/src/memorydb/src")
warnings.simplefilter("ignore")

from memorydb.api import MemoryDB
from memorydb.vector import BruteForceVectorIndex

d = tempfile.mkdtemp()
src = os.path.join(d, "mod.py")
with open(src, "w") as f:
    for i in range(30):
        f.write(f"def sym{i}(a, b):\n    return a + b + {i}\n\n")

# Force brute-force backend
db = MemoryDB.open(":memory:")
# rebuild planner index as brute force
bf = BruteForceVectorIndex(db.store)
db.planner.index = bf
db.store.attach_index(bf)
db.index(d)
print("backend:", type(db.planner.index).__name__)

for k in [1024, 1025, 2048]:
    res = db.explain("sym5", k=k)
    print(f"BF explain k={k}: seeds={len(res['seeds'])}")
