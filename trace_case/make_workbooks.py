#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""生成取证用工作簿：report.xlsx（报表）与 ext.xlsx（外链工作簿）。

报表结构
--------
CONFIG!A1   条件开关：1 -> IF 分支一；其他值 -> 分支二
DATA!B1     分支一源格：文本 "abc"（文本乘数 -> #VALUE!）
CALC!B1     分支一表达式：=DATA!B1*2
ALT!C1      分支二源格：空白格（算术中按 0 计）
ALT!C2      分支二源格：外链 ='[ext.xlsx]Sheet1'!A1
CALC!B2     分支二表达式：=ALT!C1+ALT!C2
REPORT!B1   报表结果格：=IF(CONFIG!A1=1, CALC!B1, CALC!B2)

ext.xlsx
--------
Sheet1!A1   ="abc"*2 -> #VALUE!（外链取到的是真实算出的错误值）
"""
import openpyxl

REPORT = 'report.xlsx'
EXT = 'ext.xlsx'


def build(dirpath='.'):
    ext = openpyxl.Workbook()
    ext.active.title = 'Sheet1'
    ext['Sheet1']['A1'] = '="abc"*2'
    ext.save(f'{dirpath}/{EXT}')

    wb = openpyxl.Workbook()
    wb.active.title = 'CONFIG'
    wb['CONFIG']['A1'] = 1

    wb.create_sheet('DATA')
    wb['DATA']['B1'] = 'abc'

    wb.create_sheet('CALC')
    wb['CALC']['B1'] = '=DATA!B1*2'
    wb['CALC']['B2'] = '=ALT!C1+ALT!C2'

    wb.create_sheet('ALT')
    # ALT!C1 刻意留空（空白格）
    wb['ALT']['C2'] = "='[ext.xlsx]Sheet1'!A1"

    wb.create_sheet('REPORT')
    wb['REPORT']['B1'] = '=IF(CONFIG!A1=1, CALC!B1, CALC!B2)'

    wb.save(f'{dirpath}/{REPORT}')


if __name__ == '__main__':
    build()
    print('workbooks written: report.xlsx, ext.xlsx')
