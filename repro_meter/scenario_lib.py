"""Shared scenario library: register the read-only 'meter' dtype old/new rules.

Old rule: return the raw unsigned integer of the field.
New rule: nonzero -> raw + 100; zero stays zero.
Both rules are registered under the same name; withdrawing re-registers the old one.
"""
from bitstring import Bits, Reader, Dtype
from bitstring.dtypes import DtypeDefinition, dtype_register
from bitstring import utils, bits as bits_mod
from bitstring.reader import _read_fmt_cache

MESSAGES = {
    '0x0505a': (Bits('0x0505a'), 8),   # 20 bits: ref8 / body8 / flag4
    '0x050a':  (Bits('0x050a'), 4),    # 16 bits: ref8 / body4 / flag4
    '0x0000a': (Bits('0x0000a'), 8),   # 20 bits: ref8 / body8 / flag4
}
PREVIEW_FMT = 'meter8'
WAREHOUSE_FMT = 'meter8,meter,u4'

GENERATION = {'n': 0}


def _make_definition(rule_name, get_fn):
    GENERATION['n'] += 1
    gen = GENERATION['n']
    return DtypeDefinition(
        name='meter',
        set_fn=None,                       # read-only
        get_fn=get_fn,
        return_type=int,
        is_signed=False,
        description=f'meter {rule_name} gen{gen}',
    ), gen


def old_definition():
    def get_old(bs):
        return bs.uint
    return _make_definition('old', get_old)


def new_definition():
    def get_new(bs):
        raw = bs.uint
        return raw + 100 if raw != 0 else 0
    return _make_definition('new', get_new)


def register_old():
    d, gen = old_definition()
    dtype_register.add_dtype(d)
    return gen


def register_new():
    d, gen = new_definition()
    dtype_register.add_dtype(d)
    return gen


def alternative_fmt(k):
    # Same template as WAREHOUSE_FMT, only with k spaces after the first comma.
    return 'meter8,' + (' ' * k) + 'meter,u4'


def preview(msg_hex):
    bs, _ = MESSAGES[msg_hex]
    r = Reader(bs, pos=0)                 # fresh Reader at bit 0 every time
    value = r.read_value(PREVIEW_FMT)
    return value, r.pos


def warehouse(msg_hex, fmt=WAREHOUSE_FMT):
    bs, _ = MESSAGES[msg_hex]
    r = Reader(bs, pos=0)
    values = r.read_list(fmt)
    return values, r.pos


def supplement(msg_hex, startup_params):
    # Backfill path: the three params are Dtype objects retained at program startup.
    bs, _ = MESSAGES[msg_hex]
    r = Reader(bs, pos=0)
    values = r.read_list(list(startup_params))
    return values, r.pos


def cache_snapshot():
    prep = bits_mod._prepared_fmt.cache_info()
    pre = utils.preprocess_tokens.cache_info()
    return {
        '_prepared_fmt': prep,
        'preprocess_tokens': pre,
        "Reader._read_fmt_cache keys": list(_read_fmt_cache.keys()),
        '_token_cache size': len(__import__('bitstring.dtypes', fromlist=['_token_cache'])._token_cache),
    }


def fmt0_cached_dtype_generation():
    """Which generation of meter8 Dtype object does the cached fmt0 tuple hold?"""
    dtypes, _, total = bits_mod._prepared_fmt(WAREHOUSE_FMT)
    meter8 = dtypes[0]
    # Probe the cached read_fn on the real ref bits of 0x0505a.
    probe = meter8._read_fn(MESSAGES['0x0505a'][0], 0)
    return probe, total


def preview_cached_probe():
    info = _read_fmt_cache.get(PREVIEW_FMT)
    if info is None:
        return None
    _, _, d = info
    return d._read_fn(MESSAGES['0x0505a'][0], 0)
