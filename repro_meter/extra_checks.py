import sys
sys.path.insert(0, '.')
import scenario_lib as S
from bitstring import Dtype

# 1) Alt template read AFTER withdrawal (new process, N=0 style then one alt at stage D).
S.register_old()
S.preview('0x0505a'); S.warehouse('0x0505a')
S.register_new()
S.register_old()  # withdraw without any alt use
print("alt k=1 read after withdrawal:", S.warehouse('0x0505a', S.alternative_fmt(1)))

# 2) What would 0x050a look like if its body were really 5?
from bitstring import Bits
print("0x055a bits 8:12 =", Bits('0x055a')[8:12].bin, "->", Bits('0x055a')[8:12].uint)

# 3) Eviction boundary: which k evicts fmt0? Track cache membership in one process.
S2 = __import__('importlib').import_module('scenario_lib')
# fresh process instead below
