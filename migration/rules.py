"""Explicitly selectable YAML scalar rule sets.

Two rule sets are provided:

* ``legacy`` — the historical PyYAML (YAML 1.1 style) resolver.  Plain
  scalars such as ``on``/``off``/``yes`` become booleans, ``01`` is an
  octal integer and timestamp scalars become ``datetime`` objects.
* ``modern`` — a YAML 1.2 Core Schema resolver.  Only ``true``/``false``
  are booleans, octal integers require the ``0o`` prefix and timestamp
  scalars stay plain strings because the Core Schema has no timestamp
  tag.

Both loaders share :class:`~yaml.constructor.SafeConstructor`, so an
explicit tag (``!!bool``, ``!!int``, ``!!timestamp`` ...) is interpreted
identically by either rule — that is what makes tagged correction plans
losslessly readable under both rules.
"""

__all__ = ['RULES', 'LEGACY_RULE', 'MODERN_RULE', 'get_loader',
           'LegacyLoader', 'ModernLoader', 'ModernResolver',
           'ModernConstructor']

import re

from yaml.loader import Reader, SafeLoader, Scanner, Parser, Composer
from yaml.constructor import SafeConstructor
from yaml.resolver import BaseResolver

LEGACY_RULE = 'legacy'
MODERN_RULE = 'modern'
RULES = (LEGACY_RULE, MODERN_RULE)


class ModernResolver(BaseResolver):
    """Implicit resolver implementing the YAML 1.2 Core Schema.

    Compared with the legacy resolver: booleans are limited to
    ``true``/``false`` spellings, sexagesimal and leading-zero octal
    integers are gone, and there is no timestamp resolver, so every
    timestamp-shaped scalar resolves to the default string tag.
    """


ModernResolver.add_implicit_resolver(
    'tag:yaml.org,2002:bool',
    re.compile(r'^(?:true|True|TRUE|false|False|FALSE)$'),
    list('tTfF'))

ModernResolver.add_implicit_resolver(
    'tag:yaml.org,2002:int',
    re.compile(r'^(?:[-+]?0b[0-1_]+'
               r'|[-+]?0o[0-7_]+'
               r'|[-+]?0x[0-9a-fA-F_]+'
               r'|[-+]?[0-9]+)$'),
    list('-+0123456789'))

ModernResolver.add_implicit_resolver(
    'tag:yaml.org,2002:float',
    re.compile(r'^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?'
               r'|[-+]?\.(?:inf|Inf|INF)'
               r'|\.(?:nan|NaN|NAN))$'),
    list('-+0123456789.'))

ModernResolver.add_implicit_resolver(
    'tag:yaml.org,2002:merge',
    re.compile(r'^(?:<<)$'),
    ['<'])

ModernResolver.add_implicit_resolver(
    'tag:yaml.org,2002:null',
    re.compile(r'^(?:~|null|Null|NULL|)$'),
    ['~', 'n', 'N', ''])

ModernResolver.add_implicit_resolver(
    'tag:yaml.org,2002:value',
    re.compile(r'^(?:=)$'),
    ['='])


class ModernConstructor(SafeConstructor):
    """YAML 1.2 Core integer construction.

    Integers are decimal, binary (``0b``), octal (``0o`` — the legacy
    bare-leading-zero form is rejected) or hexadecimal (``0x``); the
    sexagesimal shorthand is not supported.
    """

    def construct_yaml_int(self, node):
        value = self.construct_scalar(node).replace('_', '')
        sign = +1
        if value and value[0] == '-':
            sign = -1
        if value and value[0] in '+-':
            value = value[1:]
        if value == '0':
            return 0
        if value.startswith(('0b', '0o', '0x')):
            return sign * int(value, 0)
        return sign * int(value, 10)


ModernConstructor.add_constructor(
    'tag:yaml.org,2002:int',
    ModernConstructor.construct_yaml_int)


class LegacyLoader(SafeLoader):
    """Loader pinned to the legacy (PyYAML 1.1 style) rule set."""


class ModernLoader(Reader, Scanner, Parser, Composer,
                   ModernConstructor, ModernResolver):
    """Loader pinned to the modern (YAML 1.2 Core) rule set."""

    def __init__(self, stream):
        Reader.__init__(self, stream)
        Scanner.__init__(self)
        Parser.__init__(self)
        Composer.__init__(self)
        ModernConstructor.__init__(self)
        ModernResolver.__init__(self)


_LOADERS = {
    LEGACY_RULE: LegacyLoader,
    MODERN_RULE: ModernLoader,
}


def get_loader(rule):
    try:
        return _LOADERS[rule]
    except KeyError:
        raise ValueError("unknown scalar rule %r; expected one of %s"
                         % (rule, ', '.join(RULES)))
