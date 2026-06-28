import sys, os, tempfile, warnings
sys.path.insert(0, "/home/guido/src/memorydb/src")
warnings.simplefilter("ignore")

from memorydb.api import MemoryDB

# Build a tiny python project to index
d = tempfile.mkdtemp()
src = os.path.join(d, "mod.py")
with open(src, "w") as f:
    for i in range(30):
        f.write(f"def sym{i}(a, b):\n    return a + b + {i}\n\n")

db = MemoryDB.open(":memory:")
rep = db.index(d)
# confirm we're on the ANN backend
from memorydb.vector import SqliteVecIndex
print("backend:", type(db.planner.index).__name__)
print("nodes indexed (embedded):", rep.embedded)

for k in [5, 1024, 1025, 2048]:
    res = db.explain("sym5", k=k)
    print(f"explain k={k}: seeds={len(res['seeds'])} nodes={len(res['nodes'])}")
