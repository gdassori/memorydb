import math, random
def dot(a,b): return sum(x*y for x,y in zip(a,b))
# Unrelated random dense unit vectors (NOT forced orthogonal): typical cosine magnitude?
for dim in (768, 1536, 3072):
    rng = random.Random(1)
    mags=[]
    for _ in range(2000):
        a=[rng.gauss(0,1) for _ in range(dim)]; na=math.sqrt(dot(a,a)); a=[x/na for x in a]
        b=[rng.gauss(0,1) for _ in range(dim)]; nb=math.sqrt(dot(b,b)); b=[x/nb for x in b]
        mags.append(abs(dot(a,b)))
    mags.sort()
    print(f"dim={dim}: median|cos|={mags[len(mags)//2]:.3e}  min|cos|={mags[0]:.3e}  expected 1/sqrt(d)={1/math.sqrt(dim):.3e}")
