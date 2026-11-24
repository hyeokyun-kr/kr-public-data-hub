"""행·열 배치(KOSIS '행렬전환'과 같은 일). 시계열 결과(series·rows·times)를 사용자가 고른 배치로 표를 만든다.

배치 = {"rows": [차원 이름...], "cols": [차원 이름...]}. 차원은 결과의 dim_names(예: 항목·시도·품목)와 '시점'.
모든 차원이 행이나 열 중 한쪽에 한 번씩 있어야 한다. 행·열 안의 순서가 바깥→안쪽 순서다.

같은 규칙을 화면(static/app.js 의 pivotGrid)도 따른다:
- 차원 값의 순서: 시점은 결과의 times 순서, 나머지는 시계열 순서에서 처음 나온 순서.
- 모든 차원의 이름이 같은 시계열끼리만, 코드가 다른 차원에 '이름 (코드)'를 붙여 구분한다
  (재무상태표·포괄손익계산서의 '비지배지분'처럼 다른 차원으로 이미 갈리는 것은 그대로 둔다).
- 시계열마다 단위가 다르면 첫 차원 값 뒤에 ' [단위]'를 붙인다.
- 그래도 모든 차원 값이 같은 시계열이 둘 이상이면 뒤의 것의 마지막 차원 값에 ' #2', ' #3'…을 붙인다.
- 머리글: 열 차원이 하나면 머리글 한 줄(왼쪽 칸 = 행 차원 이름). 둘 이상이면 열 차원마다 한 줄(왼쪽 끝 칸 = 그 열 차원 이름)
  + 맨 아래 한 줄(왼쪽 칸 = 행 차원 이름). 행 차원이 없으면 왼쪽에 '값' 한 칸, 열 차원이 없으면 값 열 하나('값').
"""
from __future__ import annotations

TIME = "시점"


def dims_of(res: dict) -> list[str]:
    return list(res.get("dim_names") or []) + [TIME]


def default(res: dict) -> dict:
    d = list(res.get("dim_names") or [])
    return {"rows": d, "cols": [TIME]} if res.get("layout") == "accounts" else {"rows": [TIME], "cols": d}


def validate(res: dict, pivot: dict | None) -> dict | None:
    """올바른 배치면 정리해 돌려주고, 없으면 None(기존 가로형), 틀리면 ValueError."""
    if not pivot:
        return None
    rows, cols = list(pivot.get("rows") or []), list(pivot.get("cols") or [])
    want = dims_of(res)
    if sorted(rows + cols) != sorted(want) or len(set(rows + cols)) != len(rows + cols):
        raise ValueError(f"배치에는 {', '.join(want)}가 행이나 열에 한 번씩 있어야 합니다.")
    return {"rows": rows, "cols": cols}


def labels(res: dict) -> list[dict]:
    """시계열마다 {차원 이름: 표시 값}."""
    dims = list(res.get("dim_names") or [])
    series = res["series"]
    codes = [list((s.get("codes") or {}).values()) for s in series]

    def code(si, i):
        return codes[si][i] if i < len(codes[si]) else None
    out = []
    for si, s in enumerate(series):
        nm = s.get("names") or {}
        out.append({d: str(nm[d]) if nm.get(d) not in (None, "") else str(code(si, i) or "") for i, d in enumerate(dims)})
    groups: dict = {}                                    # 모든 차원 이름이 같은 시계열 묶음
    for si in range(len(series)):
        groups.setdefault(tuple(out[si][d] for d in dims), []).append(si)
    for g in groups.values():
        if len(g) < 2:
            continue
        for i, d in enumerate(dims):                     # 그 묶음 안에서 코드가 갈리는 차원에 '(코드)'
            if len({code(si, i) for si in g}) > 1:
                for si in g:
                    if code(si, i) not in (None, ""):
                        out[si][d] = f"{out[si][d]} ({code(si, i)})"
    units = {s.get("unit") for s in series}
    if len(units) > 1 and dims:                          # 단위가 섞이면 첫 차원 값 뒤에 [단위]
        for si, s in enumerate(series):
            out[si][dims[0]] = f"{out[si][dims[0]]} [{s.get('unit') or ''}]"
    if dims:                                             # 그래도 같으면 #2, #3
        seen: dict = {}
        for si in range(len(series)):
            k = tuple(out[si][d] for d in dims)
            seen[k] = seen.get(k, 0) + 1
            if seen[k] > 1:
                out[si][dims[-1]] = f"{out[si][dims[-1]]} #{seen[k]}"
    return out


def build(res: dict, pivot: dict) -> dict:
    lab = labels(res)
    dims = list(res.get("dim_names") or [])
    order = {TIME: {t: i for i, t in enumerate(res["times"])}}
    for d in dims:
        order[d] = {}
        for x in lab:
            order[d].setdefault(x[d], len(order[d]))
    rows, cols = pivot["rows"], pivot["cols"]
    cells, rks, cks = {}, set(), set()
    for si, t, v in res["rows"]:
        val = {**lab[si], TIME: t}
        rk, ck = tuple(val[d] for d in rows), tuple(val[d] for d in cols)
        cells[(rk, ck)] = v
        rks.add(rk)
        cks.add(ck)

    def key(dims_, k):
        return tuple(order[d].get(x, 10 ** 9) for d, x in zip(dims_, k))
    return {"rows": rows, "cols": cols, "row_keys": sorted(rks, key=lambda k: key(rows, k)),
            "col_keys": sorted(cks, key=lambda k: key(cols, k)), "cells": cells}


def table(res: dict, pivot: dict) -> tuple[list[list], list[list], int]:
    """(머리글 줄들, 본문 줄들, 왼쪽 칸 수)."""
    b = build(res, pivot)
    rows, cols, rkeys, ckeys = b["rows"], b["cols"], b["row_keys"], b["col_keys"]
    n_left = max(1, len(rows))
    left_names = rows if rows else ["값"]
    if len(cols) <= 1:
        head = [left_names + ([ck[0] for ck in ckeys] if cols else ["값"])]
    else:
        head = [[""] * (n_left - 1) + [c] + [ck[j] for ck in ckeys] for j, c in enumerate(cols)]
        head.append(left_names + [""] * len(ckeys))
    body = []
    for rk in rkeys:
        lead = list(rk) if rows else ["값"]
        body.append(lead + [b["cells"].get((rk, ck)) for ck in ckeys])
    return head, body, n_left


def describe(pivot: dict) -> str:
    return f"행: {', '.join(pivot['rows']) or '없음'} / 열: {', '.join(pivot['cols']) or '없음'}"
