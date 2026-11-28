import sys
sys.path.insert(0, '.')
import scenario_lib as S
from bitstring import bits as bits_mod
from bitstring.bits import _prepared_fmt

FMT0 = 'meter8,meter,u4'

def has_fmt0():
    # cache_clear-free membership test: temporarily wrap by relying on cache_info + direct probe
    info_before = _prepared_fmt.cache_info()
    try:
        dtypes, _, _ = _prepared_fmt(FMT0)   # hit if present, miss+insert if absent
    except Exception:
        return None
    info_after = _prepared_fmt.cache_info()
    return info_before.hits != info_after.hits  # True => was a hit

S.register_old()
S.preview('0x0505a'); S.warehouse('0x0505a')
S.register_new()
for k in range(1, 257):
    S.warehouse('0x0505a', S.alternative_fmt(k))
# End of alt loop, BEFORE any further fmt0 read: is fmt0 still cached?
info = _prepared_fmt.cache_info()
# peek without inserting: use internal mapping check via a throwaway probe of currsize and a miss
dtypes, _, _ = _prepared_fmt(FMT0)
info2 = _prepared_fmt.cache_info()
print("after k=256 loop: cache info", info)
print("fmt0 resolution now (new rule still active): probe =", dtypes[0]._read_fn(S.MESSAGES['0x0505a'][0], 0))
print("that call was a cache", "HIT" if info2.hits > info.hits else "MISS (fmt0 had been evicted; rebuilt from current register)")

# Now withdraw WITHOUT having read fmt0 while new active beyond this probe...
# (the probe above itself re-caches new dtypes; simulate clean variant in second run)
