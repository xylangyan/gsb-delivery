import sys
sys.path.insert(0, '.')
import scenario_lib as S
from bitstring import Bits, Reader

# Hypothetical packet whose 4-bit body really is 5: 0x055a
bs = Bits('0x055a')
print("0x055a total", len(bs), "bits:", bs.bin,
      "| ref", bs[0:8].uint, "| body4", bs[8:12].uint, "| flag", bs[12:16].uint)

S.register_old()
S.preview('0x0505a'); S.warehouse('0x0505a')
print("old-rule fmt0 read of 0x055a:", Reader(bs).read_list('meter8,meter,u4') and
      (lambda r: (r.read_list('meter8,meter,u4'), r.pos))(Reader(bs)))
S.register_new()
r = Reader(bs); vals = r.read_list('meter8,meter,u4')
print("new-rule fmt0 read of 0x055a:", vals, "pos", r.pos)
