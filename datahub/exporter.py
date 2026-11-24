"""CSV·Excel·ZIP 만들기. 모든 파일에 출처 기관·통계표 ID·조회 조건·수집 시점을 남긴다.

- CSV(세로형, 기본): 한 줄 = 시계열 하나의 한 시점. 줄마다 출처·ID·코드·수집시점이 붙어 파일 하나만 봐도 출처를 안다.
- CSV(가로형): 행 = 시점, 열 = 시계열. 맨 위 줄에 출처 요약 대신 열 이름에 단위를 붙인다.
- Excel: '데이터(가로형)', '데이터(세로형)', '출처·조회조건' 세 시트.
- ZIP: 고른 통계마다 Excel+CSV, 그리고 00_출처_조회조건.csv(전체 목록).
- DART 재무제표(layout="accounts")는 가로형을 행 = 계정, 열 = 연도로 만든다(재무제표를 보는 익숙한 모양).
- DART 공시 목록(layout="table")은 받은 표를 그대로 쓴다.
값은 원천 API 문자열 그대로 쓴다(CSV). Excel은 숫자로 읽히는 값만 숫자 칸으로 넣는다.
"""
from __future__ import annotations

import csv
import io
import math
import re
import zipfile
from decimal import Decimal, InvalidOperation

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import pivot as pv
from .config import DOWNLOAD_LOG

_BAD = re.compile(r'[\\/:*?"<>|\s]+')


def safe(s: str, n: int = 40) -> str:
    s = _BAD.sub("_", s or "").strip("_.")
    return s[:n] or "data"


def base_name(res: dict) -> str:
    ident = res["ids"].get("통계표ID") or res["ids"].get("통계표코드") or res["id"]
    stamp = res["fetched_at"][:10].replace("-", "")
    return safe(f"{res['source']}_{ident}_{res['name']}", 60) + f"_{res['cycle_label']}_{res['start']}~{res['end']}_수집{stamp}".replace("/", "-")


def provenance(res: dict) -> list[tuple[str, str]]:
    p = [
        ("원천 시스템", res["source_label"]),
        ("작성 기관", res.get("org_name") or ""),
        ("통계표명", res["name"]),
    ]
    p += [(k, v) for k, v in res["ids"].items()]
    p += [
        ("분류 경로", res.get("path") or ""),
        ("원본 화면", res.get("url") or ""),
        ("주기", res["cycle_label"]),
        ("조회 기간", f"{res['start']} ~ {res['end']}"),
        ("선택 항목·분류", res.get("selection_text") or ""),
    ]
    if res.get("layout") != "table":
        p.append(("시계열 수", str(len(res["series"]))))
    p += [
        ("자료 행 수", str(res["row_count"])),
        ("수집 시점(한국시간)", res["fetched_at"]),
        ("API 호출 수", str(len(res["requests"]))),
    ]
    for i, u in enumerate(res["requests"][:20], start=1):
        p.append((f"API 호출 {i}(인증키 가림)", u))
    if len(res["requests"]) > 20:
        p.append(("API 호출(나머지)", f"{len(res['requests']) - 20}건 생략"))
    for w in res.get("warnings", []):
        p.append(("참고", w))
    for i, n in enumerate(res.get("notes", [])[:10], start=1):
        p.append((f"원천 주석 {i}", n))
    p += [(str(k), str(v)) for k, v in res.get("extra_prov", [])]
    if res.get("pivot") and res.get("layout") != "table":
        p.append(("행·열 배치(가로형)", pv.describe(res["pivot"])))
    p.append(("작성 프로그램", res.get("program", "")))
    return p


def long_table(res: dict) -> tuple[list[str], list[list]]:
    if res.get("layout") == "table":
        id_cols = list(res["ids"].keys())
        head = ["원천"] + id_cols + res["table"]["head"] + ["수집시점"]
        lead = [res["source"]] + [res["ids"][k] for k in id_cols]
        return head, [lead + list(r) + [res["fetched_at"]] for r in res["table"]["rows"]]
    dim_keys = list(res["series"][0]["codes"].keys()) if res["series"] else []
    dim_names = res["dim_names"]
    id_cols = list(res["ids"].keys())
    head = ["원천", "작성기관"] + id_cols + ["통계표명"]
    for n in dim_names:
        head += [n, f"{n}_코드"]
    head += ["단위", "주기", "시점", "값", "수집시점"]
    body = []
    for si, t, v in res["rows"]:
        s = res["series"][si]
        line = [res["source"], res.get("org_name") or ""] + [res["ids"][k] for k in id_cols] + [res["name"]]
        for n, k in zip(dim_names, dim_keys):
            line += [s["names"].get(n), s["codes"].get(k)]
        line += [s.get("unit") or "", res["cycle_label"], t, v, res["fetched_at"]]
        body.append(line)
    return head, body


def wide_table(res: dict) -> tuple[list[str], list[list]]:
    if res.get("layout") == "table":
        return list(res["table"]["head"]), [list(r) for r in res["table"]["rows"]]
    if res.get("layout") == "accounts":           # 행 = 계정, 열 = 연도
        names = res["dim_names"]
        head = names + ["계정ID", "단위"] + list(res["times"])
        idx = {t: i for i, t in enumerate(res["times"])}
        lead = len(head) - len(res["times"])
        grid = [[s["names"].get(n) for n in names] + [s["codes"].get("ACCOUNT"), s.get("unit")] + [None] * len(res["times"])
                for s in res["series"]]
        for si, t, v in res["rows"]:
            grid[si][lead + idx[t]] = v
        return head, grid
    units = {s.get("unit") for s in res["series"]}
    head = ["시점"]
    for s in res["series"]:
        head.append(f"{s['label']} [{s['unit']}]" if s.get("unit") and len(units) > 1 else s["label"])
    idx = {t: i for i, t in enumerate(res["times"])}
    grid = [[t] + [None] * len(res["series"]) for t in res["times"]]
    for si, t, v in res["rows"]:
        grid[idx[t]][si + 1] = v
    return head, grid


def csv_bytes(res: dict, shape: str = "long") -> bytes:
    heads = None
    if shape != "long" and res.get("pivot") and res.get("layout") != "table":    # 사용자가 고른 행·열 배치
        heads, body, _ = pv.table(res, res["pivot"])
        head = None
    else:
        head, body = long_table(res) if shape == "long" else wide_table(res)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    if shape != "long":
        # 가로형은 줄마다 출처를 붙일 수 없어 맨 위에 '#'로 시작하는 출처 줄을 둔다
        # (pandas: read_csv(..., comment="#"), Excel에서는 위 몇 줄로 보임)
        for k, v in provenance(res):
            buf.write("# " + f"{k}: {v}".replace("\r", " ").replace("\n", " ") + "\r\n")
    for h in (heads if heads is not None else [head]):
        w.writerow(["" if x is None else x for x in h])
    w.writerows([["" if x is None else x for x in r] for r in body])
    return buf.getvalue().encode("utf-8-sig")


def _pivot_sheet(ws, res) -> None:
    """가로형 시트를 사용자 배치대로(머리글 여러 줄 가능). 값 칸만 숫자로 바꾼다(원천 문자열 규칙은 _num 그대로)."""
    heads, body, n_left = pv.table(res, res["pivot"])
    acc = res.get("layout") == "accounts"
    for h in heads:
        ws.append(h)
    for row in ws.iter_rows(min_row=1, max_row=len(heads)):
        for c in row:
            c.font = _HEAD_FONT
            c.fill = _HEAD_FILL
            c.alignment = Alignment(wrap_text=True, vertical="top")
    for r in body:
        ws.append(list(r[:n_left]) + [_num(x) for x in r[n_left:]])
        if acc:
            for c in ws[ws.max_row]:
                if isinstance(c.value, int) and not isinstance(c.value, bool):
                    c.number_format = "#,##0"
    ws.freeze_panes = f"{get_column_letter(n_left + 1)}{len(heads) + 1}"
    for i in range(1, n_left + 1):
        ws.column_dimensions[get_column_letter(i)].width = 28 if acc else 16
    for i in range(n_left + 1, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(i)].width = 14


def _num(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return v
    if not d.is_finite():
        return v
    if len(d.as_tuple().digits) > 15:      # Excel 숫자는 유효숫자 15자리까지 → 넘으면 글자 그대로
        return v
    if d == d.to_integral_value() and "." not in s and "e" not in s.lower():
        return int(d)
    f = float(d)
    # Excel 숫자로 그대로 담을 수 없으면(넘침 → 무한대, 아주 작은 수 → 0, Excel 한계 밖) 글자 그대로 둔다
    if not math.isfinite(f) or (f == 0 and d != 0) or abs(f) > 9.99e307 or (f != 0 and abs(f) < 2.3e-308):
        return v
    return f


_HEAD_FONT = Font(bold=True)
_HEAD_FILL = PatternFill("solid", fgColor="F2F4F6")


def _sheet(ws, head, body, numeric_from: int | None, widths: dict | None = None, freeze: str = "B2",
           thousands: bool = False):
    ws.append(head)
    for c in ws[1]:
        c.font = _HEAD_FONT
        c.fill = _HEAD_FILL
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for r in body:
        if numeric_from is not None:
            r = list(r[:numeric_from]) + [_num(x) for x in r[numeric_from:]]
        ws.append(r)
        if thousands:            # 재무제표 금액(원)은 큰 정수라 지수 표기(5.67E+14) 대신 천 단위 쉼표로 보이게(값은 그대로)
            for c in ws[ws.max_row]:
                if isinstance(c.value, int) and not isinstance(c.value, bool):
                    c.number_format = "#,##0"
    ws.freeze_panes = freeze
    for i, h in enumerate(head, start=1):
        ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(i, max(10, min(40, len(str(h)) * 1.6)))


def xlsx_bytes(res: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "데이터(가로형)"
    head, body = wide_table(res)
    if res.get("layout") == "table":
        ws.title = "목록"
        cols = set(res["table"].get("numeric_cols") or [])
        if cols:                       # 업종 비교 금액 칸은 숫자로(값은 그대로, 15자리 넘으면 글자)
            body = [[_num(x) if i in cols else x for i, x in enumerate(r)] for r in body]
            _sheet(ws, head, body, numeric_from=None, widths={2: 24, 7: 28, 9: 26, 10: 18}, freeze="C2", thousands=True)
        else:
            _sheet(ws, head, body, numeric_from=None, widths={1: 12, 2: 48, 3: 22, 4: 18, 7: 60})
    elif res.get("pivot"):
        _pivot_sheet(ws, res)
    else:
        lead = len(res["dim_names"]) + 2 if res.get("layout") == "accounts" else 1
        acc = res.get("layout") == "accounts"
        _sheet(ws, head, body, numeric_from=lead, widths={2: 36, 3: 30, 4: 30} if acc else None,
               freeze=f"{get_column_letter(lead + 1)}2" if acc else "B2", thousands=acc)
    if res.get("layout") != "table":
        ws2 = wb.create_sheet("데이터(세로형)")
        lhead, lbody = long_table(res)
        vpos = lhead.index("값")
        body2 = [r[:vpos] + [_num(r[vpos])] + r[vpos + 1:] for r in lbody]
        _sheet(ws2, lhead, body2, numeric_from=None, thousands=res.get("layout") == "accounts")
    ws3 = wb.create_sheet("출처·조회조건")
    _sheet(ws3, ["구분", "내용"], provenance(res), numeric_from=None, widths={1: 24, 2: 110})
    for row in ws3.iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def zip_bytes(results: list[dict]) -> bytes:
    out = io.BytesIO()
    manifest = io.StringIO()
    mw = csv.writer(manifest, lineterminator="\r\n")
    mw.writerow(["번호", "파일", "원천", "작성기관", "통계표명", "ID", "주기", "조회 기간", "선택 항목·분류",
                 "시계열 수", "행 수", "수집 시점", "원본 화면"])
    used = set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for i, res in enumerate(results, start=1):
            name = f"{i:02d}_{base_name(res)}"
            while name in used:
                name += "_"
            used.add(name)
            z.writestr(f"{name}.xlsx", xlsx_bytes(res))
            z.writestr(f"{name}.csv", csv_bytes(res, "long"))
            ident = " / ".join(f"{k} {v}" for k, v in res["ids"].items())
            mw.writerow([i, name, res["source_label"], res.get("org_name") or "", res["name"], ident, res["cycle_label"],
                         f"{res['start']} ~ {res['end']}", res.get("selection_text"), len(res["series"]),
                         res["row_count"], res["fetched_at"], res.get("url")])
        z.writestr("00_출처_조회조건.csv", manifest.getvalue().encode("utf-8-sig"))
    return out.getvalue()


def log_download(res_list: list[dict], kind: str) -> None:
    new = not DOWNLOAD_LOG.exists()
    try:
        with open(DOWNLOAD_LOG, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["내려받은 시각", "형식", "원천", "통계표명", "ID", "주기", "기간", "선택", "행 수", "수집 시점"])
            from .catalog import now_kst
            for r in res_list:
                w.writerow([now_kst(), kind, r["source"], r["name"], r["id"], r["cycle_label"],
                            f"{r['start']}~{r['end']}", r.get("selection_text"), r["row_count"], r["fetched_at"]])
    except OSError:
        pass
