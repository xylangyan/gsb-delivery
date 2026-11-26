# -*- coding: utf-8 -*-
"""带调索引两端交接复跑脚本（原库 pypinyin，未改动）。

A 端：整个标签字符串交给 pypinyin 自动切词 + 变调。
B 端：旧登记的预切汉字段列表，每个字段整段交给 pypinyin + 变调。
索引键 = TONE3 拼音以 '.' 连接。
"""
from pypinyin import lazy_pinyin, Style

S = Style.TONE3


def key(x):
    return '.'.join(lazy_pinyin(x, style=S, tone_sandhi=True))


def table(name, label, fields):
    a = key(label)
    b = key(fields)
    print(f'[{name}]')
    print(f'  原字段/标签 : {label!r}')
    print(f'  预切字段块  : {fields!r}')
    print(f'  A 自动切词键: {a}')
    print(f'  B 预切字段键: {b}')
    print(f'  对齐        : {a == b}')
    print()


BLOCK = '我爱很美很美很美'
table('短例·整块无去', BLOCK, [BLOCK])
table('短例·整块带末去', BLOCK + '去', [BLOCK + '去'])
table('短例·逐段预切无去', BLOCK, ['我爱', '很美', '很美', '很美'])
table('短例·逐段预切带末去', BLOCK + '去', ['我爱', '很美', '很美', '很美', '去'])
table('短例·两段', '我爱很美', ['我爱', '很美'])
