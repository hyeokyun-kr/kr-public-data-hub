"""한국표준산업분류(KSIC) 코드 → 업종명 표.

출처: KOSIS 101/DT_1K52E01 '시도·산업·사업체구분별 사업체수, 종사자수'(전국사업체조사, 제11차 KSIC)의 산업 분류 항목(메타 ITM).
KOSIS 코드는 'C108'처럼 대분류 글자가 앞에 붙어 있어 글자를 떼고 저장한다.
OpenDART 기업개황의 업종코드(induty_code)는 제11차 기준이다(2026-11-13 확인: 108 기타 식품 = 농심·오뚜기, 109 사료 = 선진·이지바이오).
제10차 이름을 쓰면 틀린다(10차 108 = 동물용 사료). 파일: data/ksic_names.csv (Excel로 볼 수 있음)

    python3 -m datahub.build_catalog --only ksic     # 다시 받기(KOSIS 호출 1번)
"""
from __future__ import annotations

import csv
import functools

import httpx

from .config import DATA_DIR
from .sources import kosis

KSIC_FILE = DATA_DIR / "ksic_names.csv"
KSIC_SOURCE = ("101", "DT_1K52E01")
KSIC_OBJ = "B"
LEVEL = {1: "대분류", 2: "중분류", 3: "소분류", 4: "세분류", 5: "세세분류"}


@functools.lru_cache(maxsize=1)
def names() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(KSIC_FILE, encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(line for line in f if not line.startswith("#")):
                if len(row) >= 2 and row[0] and row[0] != "코드":
                    out[row[0].strip()] = row[1].strip()
    except OSError:
        pass
    return out


def name(code: str | None) -> str | None:
    return names().get((code or "").strip()) if code else None


async def build(client: httpx.AsyncClient) -> int:
    itm = await kosis.meta(client, *KSIC_SOURCE, "ITM")
    rows = []
    for r in itm:
        iid = str(r.get("ITM_ID") or "")
        if r.get("OBJ_ID") != KSIC_OBJ or len(iid) < 3 or not iid[0].isalpha() or not iid[1:].isdigit():
            continue                  # 'C108' → '108' (대분류 글자 'C' 하나만인 것과 '전체 산업'은 뺌)
        code = iid[1:]
        rows.append((code, (r.get("ITM_NM") or "").strip(), iid[0] if len(code) == 2 else code[:-1]))   # 상위: 대분류 글자 또는 한 자리 짧은 코드
    rows = [r for r in rows if 2 <= len(r[0]) <= 5]
    if len(rows) < 1000:
        raise RuntimeError(f"KSIC 항목이 너무 적습니다({len(rows)}개). 원천 표를 확인하세요.")
    from .catalog import now_kst
    with open(KSIC_FILE, "w", encoding="utf-8-sig", newline="") as f:
        f.write(f"# 출처: KOSIS {KSIC_SOURCE[0]}/{KSIC_SOURCE[1]} 전국사업체조사(제11차 KSIC) 산업 분류 항목, "
                f"받은 시각 {now_kst()}\n")
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["코드", "업종명", "상위코드", "단계"])
        for code, nm, up in sorted(rows):
            w.writerow([code, nm, up, LEVEL.get(len(code), "")])
    names.cache_clear()
    return len(rows)
