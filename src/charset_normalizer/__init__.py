"""
Charset-Normalizer
~~~~~~~~~~~~~~
The Real First Universal Charset Detector.
A library that helps you read text from an unknown charset encoding.
Motivated by chardet, This package is trying to resolve the issue by taking a new approach.
All IANA character set names for which the Python core library provides codecs are supported.

Basic usage:
   >>> from charset_normalizer import from_bytes
   >>> results = from_bytes('Bсеки човек има право на образование. Oбразованието!'.encode('utf_8'))
   >>> best_guess = results.best()
   >>> str(best_guess)
   'Bсеки човек има право на образование. Oбразованието!'

Others methods and usages are available - see the full documentation
at <https://github.com/Ousret/charset_normalizer>.
:copyright: (c) Ahmed TAHRI
:license: MIT, see LICENSE for more details.
"""

from __future__ import annotations

import logging

from .api import from_bytes, from_fp, from_path, is_binary
from .audit import (
    AttrDict,
    SiteAuditor,
    SwitchSpec,
    audit_configurations,
    audit_volume,
    compare_configurations,
    first_candidate_difference,
    inspect_configuration,
    parse_switch_specs,
    primary_and_representative,
    switch_impact_report,
)
from .legacy import detect
from .models import CharsetMatch, CharsetMatches
from .utils import set_logging_handler
from .version import VERSION, __version__

__all__ = (
    "from_fp",
    "from_path",
    "from_bytes",
    "is_binary",
    "detect",
    "CharsetMatch",
    "CharsetMatches",
    "AttrDict",
    "SwitchSpec",
    "SiteAuditor",
    "inspect_configuration",
    "primary_and_representative",
    "parse_switch_specs",
    "first_candidate_difference",
    "compare_configurations",
    "audit_configurations",
    "switch_impact_report",
    "audit_volume",
    "__version__",
    "VERSION",
    "set_logging_handler",
)

# Attach a NullHandler to the top level logger by default
# https://docs.python.org/3.3/howto/logging.html#configuring-logging-for-a-library

logging.getLogger("charset_normalizer").addHandler(logging.NullHandler())
