"""행·열 배치(pivot) 테스트.

    python3 -m pytest -q tests/test_pivot.py
"""
from __future__ import annotations

import csv
import io
import zipfile

import pytest
from openpyxl import load_workbook

from datahub import exporter, pivot as pv


def res(units=("지수", "지수", "지수", "지수"), same_name=False):
    names = [("CPI", "서울"), ("CPI", "부산"), ("PPI", "서울"), ("PPI", "부산")]
    codes = [("A", "11"), ("A", "21"), ("B", "11"), ("B", "21")]
    if same_name:                     # 시도 이름은 같은데 코드가 다른 경우
        names[1] = ("CPI", "서울")
    series = [{"codes": {"ITEM": c[0], "C1": c[1]}, "names": {"항목": n[0], "시도": n[1]}, "unit": u, "label": f"{n[0]} {n[1]}"}
              for n, c, u in zip(names, codes, units)]
    rows = []
    for si in range(4):
        for t in ("2024", "2025"):
            rows.append([si, t, f"{si}{t[-1]}"])
    return {"source": "KOSIS", "source_label": "KOSIS", "id": "x", "ids": {"통계표ID": "x"}, "name": "표", "org_name": "기관",
            "url": "", "path": "", "cycle": "A", "cycle_label": "연", "start": "2024", "end": "2025",
            "dim_names": ["항목", "시도"], "series": series, "rows": rows, "times": ["2024", "2025"], "row_count": len(rows),
            "fetched_at": "2026-11-16 12:00:00", "requests": [], "warnings": [], "selection_text": "", "notes": []}


def test_default_and_transpose():
    r = res()
    assert pv.default(r) == {"rows": ["시점"], "cols": ["항목", "시도"]}
    head, body, n = pv.table(r, {"rows": ["시점"], "cols": ["항목", "시도"]})
    assert head == [["항목", "CPI", "CPI", "PPI", "PPI"], ["시도", "서울", "부산", "서울", "부산"], ["시점", "", "", "", ""]]
    assert body == [["2024", "04", "14", "24", "34"], ["2025", "05", "15", "25", "35"]] and n == 1
    head, body, n = pv.table(r, {"rows": ["항목", "시도"], "cols": ["시점"]})       # 행·열 바꾸기
    assert head == [["항목", "시도", "2024", "2025"]] and n == 2
    assert body[0] == ["CPI", "서울", "04", "05"] and body[3] == ["PPI", "부산", "34", "35"]
    head, body, n = pv.table(r, {"rows": ["시도", "시점"], "cols": ["항목"]})       # 시도를 바깥 행으로
    assert head == [["시도", "시점", "CPI", "PPI"]]
    assert [b[:2] for b in body] == [["서울", "2024"], ["서울", "2025"], ["부산", "2024"], ["부산", "2025"]]
    assert body[2] == ["부산", "2024", "14", "34"]


def test_all_rows_all_cols():
    r = res()
    head, body, n = pv.table(r, {"rows": ["항목", "시도", "시점"], "cols": []})
    assert head == [["항목", "시도", "시점", "값"]] and len(body) == 8 and body[0] == ["CPI", "서울", "2024", "04"]
    head, body, n = pv.table(r, {"rows": [], "cols": ["시점", "항목", "시도"]})
    assert n == 1 and len(head) == 4 and head[-1] == ["값"] + [""] * 8 and body == [["값", "04", "14", "24", "34", "05", "15", "25", "35"]]


def test_name_collision_units_and_validation():
    r = res(same_name=True)
    head, body, _ = pv.table(r, {"rows": ["항목", "시도"], "cols": ["시점"]})
    assert [b[1] for b in body] == ["서울 (11)", "서울 (21)", "서울", "부산"]   # 모든 차원 이름이 겹칠 때만 코드로 구분
    assert [b[0] for b in body] == ["CPI", "CPI", "PPI", "PPI"]                    # 항목 코드는 같으니 그대로
    r = res(units=("지수", "%", "지수", "%"))
    head, body, _ = pv.table(r, {"rows": ["항목", "시도"], "cols": ["시점"]})
    assert [b[0] for b in body] == ["CPI [지수]", "CPI [%]", "PPI [지수]", "PPI [%]"]       # 단위가 섞이면 단위를 붙임
    for bad in ({"rows": ["시점"], "cols": ["항목"]}, {"rows": ["시점", "항목"], "cols": ["항목", "시도"]},
                {"rows": ["시점", "없는차원"], "cols": ["항목", "시도"]}):
        with pytest.raises(ValueError):
            pv.validate(res(), bad)
    assert pv.validate(res(), None) is None


def test_files_follow_pivot():
    r = {**res(), "pivot": {"rows": ["항목", "시도"], "cols": ["시점"]}}
    lines = [l for l in exporter.csv_bytes(r, "wide").decode("utf-8-sig").splitlines() if not l.startswith("#")]
    assert list(csv.reader(lines))[:2] == [["항목", "시도", "2024", "2025"], ["CPI", "서울", "04", "05"]]
    assert "# 행·열 배치(가로형): 행: 항목, 시도 / 열: 시점" in exporter.csv_bytes(r, "wide").decode("utf-8-sig")
    long_rows = list(csv.reader(io.StringIO(exporter.csv_bytes(r, "long").decode("utf-8-sig"))))
    assert len(long_rows) == 9                                                  # 세로형은 배치와 무관
    r2 = {**res(), "pivot": {"rows": ["시점"], "cols": ["항목", "시도"]}}
    wb = load_workbook(io.BytesIO(exporter.xlsx_bytes(r2)))
    ws = wb["데이터(가로형)"]
    assert [c.value for c in ws[1]] == ["항목", "CPI", "CPI", "PPI", "PPI"] and ws.freeze_panes == "B4"
    assert ws["B4"].value == 4 and ws["A4"].value == "2024"                      # '04' → 숫자 4(Excel 숫자 칸 규칙 그대로)
    z = zipfile.ZipFile(io.BytesIO(exporter.zip_bytes([r, res()])))
    names = sorted(z.namelist())
    assert len(names) == 5
    plain = load_workbook(io.BytesIO(exporter.xlsx_bytes(res())))["데이터(가로형)"]
    assert [c.value for c in plain[1]][:2] == ["시점", "CPI | 서울"] or plain["A1"].value == "시점"   # 배치가 없으면 예전 그대로


def test_accounts_default_and_pivot():
    r = res()
    r["layout"] = "accounts"
    assert pv.default(r) == {"rows": ["항목", "시도"], "cols": ["시점"]}
    r["pivot"] = {"rows": ["시점"], "cols": ["항목", "시도"]}
    ws = load_workbook(io.BytesIO(exporter.xlsx_bytes(r)))["데이터(가로형)"]
    assert ws.max_row == 5 and ws["A5"].value == "2025"
