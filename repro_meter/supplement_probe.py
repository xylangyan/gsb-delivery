import sys
sys.path.insert(0, '.')
import scenario_lib as S
from bitstring import Dtype

stage = sys.argv[1]
S.register_old()
startup = [Dtype('meter', 8), Dtype('meter'), Dtype('u4')]
m8, m_var, u4 = startup
m8_fn = m8._read_fn
m_var_obj = m_var
S.preview('0x0505a'); S.warehouse('0x0505a')
S.register_new()
if stage == 'C':
    for k in range(1, 301):
        S.warehouse('0x0505a', S.alternative_fmt(k))
if stage == 'D':
    for k in range(1, 301):
        S.warehouse('0x0505a', S.alternative_fmt(k))
    S.register_old()

bs = S.MESSAGES['0x0505a'][0]
# What does the retained fixed-length meter8 read directly?
print(f"stage {stage}: retained meter8._read_fn on raw5 ->", m8._read_fn(bs, 0),
      "| same fn object as startup?", m8._read_fn is m8_fn)
# What does a freshly resolved meter8 read?
fresh_m8 = Dtype('meter', 8)
print(f"stage {stage}: fresh Dtype('meter',8) on raw5  ->", fresh_m8._read_fn(bs, 0))
# Re-length the retained unsized meter the way _read_dtype_list does (bits.py:1369)
relen = Dtype(m_var_obj, 8)
print(f"stage {stage}: Dtype(retained_meter, 8) on raw5 ->", relen._read_fn(bs, 0),
      "| same object as retained unsized?", relen is m_var_obj)
print(f"stage {stage}: supplement read 0x0505a ->", S.supplement('0x0505a', startup))
