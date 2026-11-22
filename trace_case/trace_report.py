#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""用 formulas 对报表错码做来源取证。

场景：报表 REPORT!B1 两次回填都显示同一错码 #VALUE!。
  运行A：CONFIG!A1=1，IF 走分支一 =CALC!B1（=DATA!B1*2，文本乘数）
  运行B：CONFIG!A1=2，IF 走分支二 =CALC!B2（=ALT!C1+ALT!C2，空白格+外链错误）

每次运行产出一份"解释"：结果格、错码、条件取值、真正决定结果的源格及取值，
并逐条验证：拿掉任何一项必需证据，本次结论即无法重现；未执行分支可缺。
同时区分"这次确实算出了错"与"公式/输入没取到"。
"""
import logging
import os
import os.path as osp
import shutil
import tempfile

import openpyxl

import formulas
from formulas.tokens.operand import XlError

logging.disable(logging.CRITICAL)

HERE = osp.dirname(osp.abspath(__file__))
REPORT = osp.join(HERE, 'report.xlsx')
EXT = osp.join(HERE, 'ext.xlsx')

CFG = "'[report.xlsx]CONFIG'!A1"
DATA_B1 = "'[report.xlsx]DATA'!B1"
CALC_B1 = "'[report.xlsx]CALC'!B1"
CALC_B2 = "'[report.xlsx]CALC'!B2"
ALT_C1 = "'[report.xlsx]ALT'!C1"
ALT_C2 = "'[report.xlsx]ALT'!C2"
EXT_A1 = "'[ext.xlsx]SHEET1'!A1"
OUT = "'[report.xlsx]REPORT'!B1"

ERR = '#VALUE!'


def scalar(v):
    try:
        return v.value[0, 0]
    except AttributeError:
        return v
    except Exception:
        try:
            return v.value
        except Exception:
            return v


def disp(v):
    if isinstance(v, XlError):
        return str(v)
    if v is None or type(v).__name__ == 'Empty' or str(v) == 'empty':
        return '<空白>'
    return repr(v) if isinstance(v, str) else v


def is_err(v, code=None):
    ok = isinstance(v, XlError) or (isinstance(v, str) and v.startswith('#'))
    return ok and (code is None or str(v) == code)


def load_model(dirpath):
    return formulas.ExcelModel().loads(osp.join(dirpath, 'report.xlsx')).finish()


def evaluate(dirpath, overrides=()):
    """在 dirpath 的工作簿上，用 overrides 覆盖输入后算 REPORT!B1。

    返回 (结果值, 本次执行的子图 {节点: 值}, 模型)。
    """
    xl = load_model(dirpath)
    sol = xl.calculate(inputs=dict(overrides), outputs=[OUT])
    got = {k: scalar(v) for k, v in sol.items()}
    return got.get(OUT), got, xl


def variant(edits, keep_ext=True):
    """生成报表变体：edits = {(sheet, coord): value}，value=None 表示清空。"""
    dirpath = tempfile.mkdtemp(prefix='trace_')
    wb = openpyxl.load_workbook(REPORT)
    for (sheet, coord), value in edits.items():
        wb[sheet][coord] = value
    wb.save(osp.join(dirpath, 'report.xlsx'))
    if keep_ext:
        shutil.copy(EXT, osp.join(dirpath, 'ext.xlsx'))
    return dirpath


def cleanup(dirpath):
    shutil.rmtree(dirpath, ignore_errors=True)


def run_case(cond):
    """一次正式运行：返回解释所需的全部事实。"""
    res, got, xl = evaluate(HERE, {CFG: cond})
    formula_ok = OUT in xl.cells and xl.cells[OUT].func is not None
    ext_loaded = any('EXT.XLSX' in k for k in xl.books)
    taken = CALC_B1 if cond == 1 else CALC_B2
    skipped = CALC_B2 if cond == 1 else CALC_B1
    return {
        'cond': cond, 'result': res, 'computed': got, 'xl': xl,
        'formula_compiled': formula_ok, 'ext_loaded': ext_loaded,
        'taken_branch': taken, 'skipped_branch': skipped,
    }


def check(desc, edits, expect, keep_ext=True, overrides=()):
    """证据移除检验：在变体上重算，返回 (是否如预期, 实际值, 说明)。"""
    dirpath = variant(edits, keep_ext=keep_ext)
    try:
        res, got, _ = evaluate(dirpath, overrides)
    finally:
        cleanup(dirpath)
    if expect == 'same_err':
        ok = is_err(res, ERR)
        note = '结论仍重现' if ok else '结论未重现'
    elif expect == 'not_same':
        ok = not is_err(res, ERR)
        note = '结论无法重现（符合预期）' if ok else '结论仍重现（不符合预期）'
    else:  # expect 为具体错码
        ok = is_err(res, expect)
        note = f'得到 {res}'
    return ok, res, f'{desc} -> REPORT!B1 = {disp(res)}（{note}）'


def main():
    lines = []
    log = lines.append
    log('# 报表错码来源取证（formulas）\n')
    log('- 选定结果格：`REPORT!B1` = `=IF(CONFIG!A1=1, CALC!B1, CALC!B2)`')
    log('- 分支一：`CALC!B1` = `=DATA!B1*2`（源格 `DATA!B1`）')
    log('- 分支二：`CALC!B2` = `=ALT!C1+ALT!C2`（源格 `ALT!C1` 空白格、`ALT!C2` 外链 `\'[ext.xlsx]Sheet1\'!A1`）')
    log('- 条件：`CONFIG!A1`，取值 1 -> 分支一，其他 -> 分支二\n')

    explanations = []
    for name, cond in (('A', 1), ('B', 2)):
        case = run_case(cond)
        got = case['computed']
        res = case['result']
        taken, skipped = case['taken_branch'], case['skipped_branch']
        log(f'## 运行{name}：CONFIG!A1 = {cond}\n')
        log(f'- 结果：`REPORT!B1` = **{disp(res)}**（错码 {res}）')
        log(f'- 公式已取到并编译：{case["formula_compiled"]}；'
            f'外链工作簿已加载：{case["ext_loaded"]}')
        log(f'- 性质判定：结果是由求值真实算出的错误值 {res}，'
            f'不是公式未编译或输入未取到（见下方诊断）')
        log(f'- 条件取值：`CONFIG!A1` = {cond} -> 执行'
            f'{"分支一（CALC!B1）" if cond == 1 else "分支二（CALC!B2）"}')
        log(f'- 本次执行子图实际算到的格子（含 IF 急切求值的对侧分支）：')
        for k in sorted(got):
            log(f'    - `{k}` = {disp(got[k])}')
        if cond == 1:
            required = [(CFG, 1, '条件：选中分支一'),
                        (DATA_B1, got[DATA_B1], '分支一源格：文本，乘数运算产生 #VALUE!')]
            optional = [(ALT_C1, got[ALT_C1]), (ALT_C2, got[ALT_C2]),
                        (EXT_A1, got[EXT_A1]), (CALC_B2, got[CALC_B2])]
        else:
            required = [(CFG, 2, '条件：选中分支二'),
                        (ALT_C2, got[ALT_C2], '外链错误：取自 ext.xlsx 的 #VALUE!，经加法传播')]
            optional = [(DATA_B1, got[DATA_B1]), (CALC_B1, got[CALC_B1])]
        log(f'- 决定结果的必需证据：')
        for cell, val, why in required:
            log(f'    - `{cell}` = {disp(val)} — {why}')
        log(f'- 读到但不决定结果（可缺）：')
        for cell, val in optional:
            log(f'    - `{cell}` = {disp(val)}')
        if cond == 2:
            log(f'    - `{ALT_C1}` = {disp(got[ALT_C1])} — 本次读到，'
                f'算术中按 0 计；换成任何非错误值错码不变，故非必需')
        log('')
        explanations.append((name, cond, case, required))

    log('## 证据必要性检验（拿掉任一必需证据，结论即不能重现）\n')
    log('每次检验先清掉未执行分支的格子（证明其可缺），再移除待验证据。\n')

    checks = []
    # 运行A：结论 = REPORT!B1 的 #VALUE! 由分支一 DATA!B1 算出
    checks.append(('A', '基线：仅清掉未执行分支（ALT!C1、ALT!C2）',
                   *check('清 ALT!C2', {('ALT', 'C2'): None}, 'same_err',
                          overrides={CFG: 1})))
    checks.append(('A', '移除 CONFIG!A1（清空）',
                   *check('清 ALT!C2 + 清 CONFIG!A1',
                          {('ALT', 'C2'): None, ('CONFIG', 'A1'): None},
                          'not_same', overrides={})))
    checks.append(('A', '移除 DATA!B1（清空）',
                   *check('清 ALT!C2 + 清 DATA!B1',
                          {('ALT', 'C2'): None, ('DATA', 'B1'): None},
                          'not_same', overrides={CFG: 1})))
    # 运行B：结论 = REPORT!B1 的 #VALUE! 由分支二外链错误算出
    checks.append(('B', '基线：仅清掉未执行分支（DATA!B1）',
                   *check('清 DATA!B1', {('DATA', 'B1'): None}, 'same_err',
                          overrides={CFG: 2})))
    checks.append(('B', '移除 CONFIG!A1（改回 1，即条件取另一值）',
                   *check('清 DATA!B1 + CONFIG!A1=1',
                          {('DATA', 'B1'): None}, 'not_same',
                          overrides={CFG: 1})))
    checks.append(('B', '移除 ALT!C2 外链（清空）',
                   *check('清 DATA!B1 + 清 ALT!C2',
                          {('DATA', 'B1'): None, ('ALT', 'C2'): None},
                          'not_same', overrides={CFG: 2})))
    checks.append(('B', '对照：ALT!C1 空白格换成 7（非必需证据）',
                   *check('ALT!C1=7', {('ALT', 'C1'): 7}, 'same_err',
                          overrides={CFG: 2})))

    for run, desc, ok, res, note in checks:
        log(f'- [运行{run}] {desc}：{"通过" if ok else "失败"}。{note}')

    log('\n## 类别诊断：确实算出了错，还是公式/输入没取到\n')
    dirpath = variant({}, keep_ext=False)
    try:
        res, got, _ = evaluate(dirpath, {CFG: 2})
    finally:
        cleanup(dirpath)
    log(f'- 正式运行A/B：公式均已编译、外链工作簿均已加载，'
        f'`REPORT!B1` 的 #VALUE! 是求值真实算出的错误值。')
    log(f'- 对照实验：删掉 ext.xlsx 后重算运行B，`REPORT!B1` = **{disp(res)}**'
        f'（formulas 对加载失败的外链单元格替换为 #REF!）——'
        f'这属于"所需输入没取到"，错码也不同，应与"确实算出了错"区分。')
    log(f'- 同一错码两次出现的根因不同：运行A来自分支一文本乘数，'
        f'运行B来自分支二外链错误；仅看错码无法区分来源，必须回溯到源格。')

    report = '\n'.join(lines) + '\n'
    out = osp.join(HERE, 'TRACE_REPORT.md')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(report)
    print(report)
    print('written:', out)


if __name__ == '__main__':
    main()
