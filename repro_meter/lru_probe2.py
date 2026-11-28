import sys
sys.path.insert(0, '.')
import scenario_lib as S
from bitstring.bits import _prepared_fmt
FMT0 = 'meter8,meter,u4'
S.register_old()
S.preview('0x0505a'); S.warehouse('0x0505a')
S.register_new()
for k in range(1, 257):
    S.warehouse('0x0505a', S.alternative_fmt(k))
S.register_old()  # withdraw, and NEVER read fmt0 between eviction and withdrawal
vals, pos = S.warehouse('0x0505a')
print("withdrawal then first fmt0 read (no window fmt0 read post-eviction):", vals, pos)
