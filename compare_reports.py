#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比两份 etf-full-scan HTML 报告的收敛程度（复用主理人对比逻辑）"""
import re, html, sys

def clean(c):
    return html.unescape(re.sub(r'<[^>]+>','',c)).strip()

def get_table(path, idx):
    with open(path, encoding='utf-8') as f:
        content = f.read()
    tables = re.findall(r'<table[^>]*>(.*?)</table>', content, re.S)
    if idx >= len(tables):
        return []
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', tables[idx], re.S)
    return [[clean(c) for c in re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', r, re.S)] for r in rows]

def parse_rows(path, idx):
    data = get_table(path, idx)
    if not data:
        return {}
    header = data[0]
    nh = len(header)
    result = {}
    for r in data[1:]:
        if len(r) != nh:
            continue
        d = dict(zip(header, r))
        name_cell = d.get('ETF','')
        cm = re.search(r'(\d{6})(?:\.(?:SH|SZ|BJ))?', name_cell)
        code = cm.group(1) if cm else 'N/A'
        bm = re.match(r'^([^\u4e00-\u9fa5A-Za-z0-9]+)', name_cell)
        badge = bm.group(1) if bm else ''
        result[code] = {'raw': d, 'badge': badge, 'name': name_cell}
    return result

def parse_summary(path):
    data = get_table(path, 2)
    if not data:
        return {}
    header = data[0]
    res = {}
    for r in data[1:]:
        if len(r) != len(header):
            continue
        d = dict(zip(header, r))
        res[d.get('板块','')] = d
    return res

def main():
    path_a, path_b = sys.argv[1], sys.argv[2]
    label_a, label_b = sys.argv[3], sys.argv[4]
    a = parse_rows(path_a, 1)
    b = parse_rows(path_b, 1)
    ca, cb = set(a), set(b)
    common = ca & cb
    print(f'{label_a}: {len(ca)}只 | {label_b}: {len(cb)}只 | 共同: {len(common)}')
    print(f'{label_a}独有: {sorted(ca-cb)}')
    print(f'{label_b}独有: {sorted(cb-ca)}')
    fields = ['现价','涨跌%','距EMA%','①PA','②威科夫','③份额+','RSI','量(亿)']
    diff_cnt = 0
    diffs = []
    for code in sorted(common):
        ra, rb = a[code], b[code]
        d = []
        for f in fields:
            va, vb = ra['raw'].get(f,''), rb['raw'].get(f,'')
            if va != vb:
                d.append(f'{f}: {va}|{vb}')
        if ra['badge'] != rb['badge']:
            d.append(f'徽章: {ra["badge"]}|{rb["badge"]}')
        if d:
            diff_cnt += 1
            diffs.append((code, ra['name'][:14], rb['name'][:14], d))
    print(f'共同{len(common)}只中 {diff_cnt} 只有差异')
    for code, na, nb, d in diffs[:25]:
        print(f'  [{code}] {na} vs {nb}')
        for x in d:
            print(f'      {x}')

if __name__ == '__main__':
    main()
