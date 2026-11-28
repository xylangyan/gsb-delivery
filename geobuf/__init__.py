from .encode import Encoder
from .decode import Decoder
from .packets import (
    NoPacketArrangementError,
    check_boundary,
    encode_run,
    extract_boundaries,
    plan_packets,
    verify_boundaries,
)

__version__ = '2.0.1'


def encode(*args):
    return Encoder().encode(*args)


def decode(*args):
    return Decoder().decode(*args)
