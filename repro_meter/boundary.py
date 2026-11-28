import sys
sys.path.insert(0, '.')
import scenario_lib as S
from bitstring import bits as bits_mod

# (a) precise eviction boundary of fmt0 in one continuous process
S.register_old()
S.preview('0x0505a'); S.warehouse('0x0505a')   # fmt0 cached (old), meter8 preview cached (old)
S.register_new()
evicted_at = None
for k in range(1, 301):
    S.warehouse('0x0505a', S.alternative_fmt(k))
    info = bits_mod._prepared_fmt.cache_info()
    if evicted_at is None and info.currsize == 256 and k >= 256:
        # probe whether fmt0 still resolves to old meter8
        probe, _ = S.fmt0_cached_dtype_generation()
        if probe == 105:
            evicted_at = k
            break
print("fmt0 evicted and re-resolved at k =", evicted_at)

# (b) continuous process: after eviction, a fmt0 read under new rule, then withdraw
print("fmt0 read while new active (post-eviction):", S.warehouse('0x0505a'))
S.register_old()  # withdrawal
print("fmt0 read AFTER withdrawal, same process  :", S.warehouse('0x0505a'))
print("preview after withdrawal, same process     :", S.preview('0x0505a'))
print("cached fmt0 probe after withdrawal         :", S.fmt0_cached_dtype_generation()[0])
print("cached preview probe after withdrawal      :", S.preview_cached_probe())
