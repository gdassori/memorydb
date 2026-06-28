import sys, math, random
sys.path.insert(0, "src")
from memorydb.api import MemoryDB

# Custom realistic embedder at dim 1536, returning normalized vectors.
class FakeEmbedder:
    model = "fake-1536"
    def __init__(self, dim=1536):
        self.dim = dim
    def embed(self, texts):
        out = []
        for tx in texts:
            rng = random.Random(hash(tx) & 0xffffffff)
            v = [rng.gauss(0,1) for _ in range(self.dim)]
            n = math.sqrt(sum(x*x for x in v)) or 1.0
            out.append([x/n for x in v])
        return out

db = MemoryDB.open(":memory:", embedder=FakeEmbedder(1536))
# Inspect which index was chosen
print("index type:", type(db._planner.index).__name__)
