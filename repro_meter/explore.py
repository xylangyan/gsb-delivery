from bitstring import Bits, Reader

for hx, body_w in [('0x0505a', 8), ('0x050a', 4), ('0x0000a', 8)]:
    bs = Bits(hx)
    n = len(bs)
    ref = bs[0:8].uint
    body = bs[8:8 + body_w].uint
    flag = bs[n - 4:n].uint
    print(f"{hx}: total={n} bits  bin={bs.bin}")
    print(f"  ref  bits[0:8]    = {bs[0:8].bin} -> uint {ref}")
    print(f"  body bits[8:{8+body_w}] = {bs[8:8+body_w].bin} -> uint {body}  (width {body_w})")
    print(f"  flag bits[{n-4}:{n}] = {bs[n-4:n].bin} -> uint {flag}")
