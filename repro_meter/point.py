"""Fresh-process replay of one operation point.

Usage: point.py <N> <stage>
  N in {0,255,256,300}; stage in {A,B,C,D}
Timeline inside the process:
  register old -> (stage A checks) -> preview+warehouse once -> register new
  -> (stage B checks) -> alt templates k=1..N on 0x0505a -> (stage C checks)
  -> withdraw (register old) -> (stage D checks)
Only the requested stage's checks are executed, so earlier-check side effects
match the real timeline (each real stage also did those reads).
"""
import sys

sys.path.insert(0, '/Users/guo/Documents/GSB/1109/batches/accounts55-bao-20261128/control/questions/GSB1109-20261127-BAO-BITSTRING-01/retry-2/workspace/repro_meter')

from bitstring import Dtype
import scenario_lib as S

N = int(sys.argv[1])
STAGE = sys.argv[2]
ALL_MSGS = ['0x0505a', '0x050a', '0x0000a']


def line(label, values, pos):
    print(f"    {label:<26} values={values} pos={pos}")


def checks(tag):
    print(f"  --- {tag} ---")
    for m in ALL_MSGS:
        line(f"preview {m}", *S.preview(m))
    for m in ALL_MSGS:
        line(f"warehouse fmt0 {m}", *S.warehouse(m))
    for m in ALL_MSGS:
        line(f"supplement {m}", *S.supplement(m, STARTUP_PARAMS))
    probe, total = S.fmt0_cached_dtype_generation()
    print(f"    cached fmt0 meter8 probe on raw5 -> {probe} (total_bits={total})")
    print(f"    cached preview meter8 probe      -> {S.preview_cached_probe()}")
    ci = S.cache_snapshot()
    print(f"    _prepared_fmt {ci['_prepared_fmt']}")
    print(f"    preprocess    {ci['preprocess_tokens']}")
    print(f"    _read_fmt_cache keys={ci['Reader._read_fmt_cache keys']} "
          f"_token_cache size={ci['_token_cache size']}")


# Startup: old rule registered, backfill params retained from this moment.
S.register_old()
STARTUP_PARAMS = [Dtype('meter', 8), Dtype('meter'), Dtype('u4')]
print(f"N={N} target stage={STAGE}")

if STAGE == 'A':
    checks("STAGE A old rule registered (preview+warehouse first runs)")
    sys.exit(0)

# The once-only preview and warehouse before the new rule goes in.
S.preview('0x0505a')
S.warehouse('0x0505a')
S.register_new()

if STAGE == 'B':
    checks("STAGE B new rule just registered")
    sys.exit(0)

# Alternative batch: k=1..N spaces, each reading 0x0505a once.
alt_results = {}
for k in range(1, N + 1):
    vals, pos = S.warehouse('0x0505a', S.alternative_fmt(k))
    alt_results[k] = (vals, pos)
if alt_results:
    shown = [1, 2, 255, 256, 257, 300]
    print("  alt template reads of 0x0505a (k spaces):")
    for k in shown:
        if k in alt_results:
            print(f"    k={k:<4} fmt={S.alternative_fmt(k)!r:<34} values={alt_results[k][0]} pos={alt_results[k][1]}")
    distinct = sorted({tuple(v) for v, _ in alt_results.values()})
    print(f"    distinct alt result tuples over k=1..{N}: {distinct}")

if STAGE == 'C':
    checks(f"STAGE C after {N} alt templates, new rule still active")
    sys.exit(0)

S.register_old()  # withdrawal
checks(f"STAGE D after withdrawal (old re-registered), N was {N}")
