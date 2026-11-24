"""네트워크 없이 도는 테스트: 기간 형식, 동의어, 검색 순위, 내보내기, 오류 분류·키 가림, 응답 정리.

    python3 -m pytest -q tests/test_offline.py
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import zipfile

import httpx
import pytest
from openpyxl import load_workbook

from datahub import catalog as cat
from datahub import config, exporter, periods, query, synonyms
from datahub.errors import SourceError
from datahub.sources import ecos, kosis

FAKE_ECOS = "ECOSFAKEKEY123456789"
FAKE_KOSIS = "KOSISFAKEKEY_abcdefghijklmnopqrstuvwxyz12345"


@pytest.fixture(autouse=True)
def fake_keys(monkeypatch):
    monkeypatch.setenv("ECOS_API_KEY", FAKE_ECOS)
    monkeypatch.setenv("KOSIS_API_KEY", FAKE_KOSIS)
    config._KEY_CACHE.clear()
    yield
    config._KEY_CACHE.clear()


# ------------------------------------------------------------------ 기간
@pytest.mark.parametrize("cycle,raw,disp,ecos_fmt,kosis_fmt", [
    ("A", "2025", "2025", "2025", "2025"),
    ("M", "202501", "2025-01", "202501", "202501"),
    ("M", "1965.01", "1965-01", "196501", "196501"),
    ("Q", "2025Q3", "2025-Q3", "2025Q3", "202503"),
    ("Q", "1965 1/4", "1965-Q1", "1965Q1", "196501"),
    ("Q", "202404", "2024-Q4", "2024Q4", "202404"),
    ("S", "2025S2", "2025-H2", "2025S2", "202502"),
    ("D", "20250102", "2025-01-02", "20250102", "20250102"),
    ("SM", "202501S2", "2025-01-S2", "202501S2", None),
])
def test_period_formats(cycle, raw, disp, ecos_fmt, kosis_fmt):
    assert periods.to_display(cycle, raw) == disp
    assert periods.to_ecos(cycle, disp) == ecos_fmt
    if kosis_fmt:
        assert periods.to_kosis(cycle, disp) == kosis_fmt


def test_period_bad():
    with pytest.raises(periods.PeriodError):
        periods.to_display("M", "2025-13")
    with pytest.raises(periods.PeriodError):
        periods.to_display("Q", "2025Q5")
    with pytest.raises(ValueError):
        periods.to_display("D", "2025-02-30")


def test_shift_count_default():
    assert periods.shift("M", "2025-01", -1) == "2024-12"
    assert periods.shift("Q", "2025-Q1", -5) == "2023-Q4"
    assert periods.shift("D", "2024-03-01", -1) == "2024-02-29"
    assert periods.count("M", "2016-01", "2025-12") == 120
    assert periods.count("Q", "2024-Q3", "2025-Q2") == 4
    assert periods.default_range("M", "1965-01", "2026-10") == ("2016-11", "2026-10")
    assert periods.default_range("A", "2020", "2025") == ("2020", "2025")   # 수록 시작보다 앞으로 가지 않음


# ------------------------------------------------------------------ 동의어
def test_synonym_expand():
    terms = synonyms.expand("CPI")
    names = [t for t, _, _ in terms]
    assert names[0] == "CPI"
    assert "소비자물가지수" in names
    rel = [t for t, why, w in terms if why.startswith("관련어")]
    assert "생활물가지수" in rel and all(w == 0.7 for t, why, w in terms if why.startswith("관련어"))
    multi = [t for t, _, _ in synonyms.expand("CPI 서울")]
    assert "소비자물가지수 서울" in multi
    info = synonyms.related_info("기업 규모")
    assert "종사자 수" in info["related"]


# ------------------------------------------------------------------ 검색 순위
@pytest.fixture
def small_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(cat, "CATALOG_DB", tmp_path / "c.sqlite")
    con = cat.connect()
    rows = [
        dict(source="KOSIS", ds_id="101/DT_2IFS002", org_id="101", org_name="국가데이터처", name="소비자물가지수",
             path="국제기구별 통계 > IMF", ranges={"*": ["1960", "2026"]}, end_year=2026, vw_cd="MT_RTITLE"),
        dict(source="KOSIS", ds_id="101/DT_1J22003", org_id="101", org_name="국가데이터처", name="소비자물가지수(2020=100)",
             path="물가 > 소비자물가조사", ranges={"*": ["1965", "2026"]}, end_year=2026, vw_cd="MT_ZTITLE"),
        dict(source="KOSIS", ds_id="101/DT_OLD", org_id="101", org_name="국가데이터처", name="소비자물가지수(2010=100)",
             path="물가 > 소비자물가조사 > 중단", ranges={"*": ["1965", "2016"]}, end_year=2016, vw_cd="MT_ZTITLE"),
        dict(source="ECOS", ds_id="901Y009", org_id="BOK", org_name="한국은행", name="소비자물가지수",
             path="물가 > 소비자물가지수(2020=100)", ranges={"M": ["1965-01", "2026-10"]}, end_year=2026),
        dict(source="ECOS", ds_id="731Y001", org_id="BOK", org_name="한국은행", name="주요국 통화의 대원화환율",
             path="환율", ranges={"D": ["1964-05-01", "2026-11-12"]}, end_year=2026),
    ]
    for r in rows:
        cat.upsert_dataset(con, r)
    con.execute("INSERT INTO ecos_items (stat_code,grp_code,grp_name,item_code,item_name,cycle) VALUES "
                "('731Y001','Group1','통화별','0000001','원/미국달러(매매기준율)','D')")
    con.commit()
    con.close()
    return cat.Catalog()


def test_search_ranking(small_catalog):
    hits = small_catalog.search(synonyms.expand("CPI"))
    ids = [h.entry.ds_id for h in hits]
    # 국내 통계가 국제(IMF) 표보다, 지금 나오는 표가 중단된 표보다 위
    assert ids.index("101/DT_1J22003") < ids.index("101/DT_2IFS002")
    assert ids.index("101/DT_1J22003") < ids.index("101/DT_OLD")
    assert "동의어" in hits[0].reason
    hits = small_catalog.search(synonyms.expand("원달러 환율"))
    top = hits[0]
    assert top.entry.ds_id == "731Y001"
    assert top.items and top.items[0][0] == "0000001"
    only_ecos = small_catalog.search(synonyms.expand("소비자물가지수"), source="ECOS")
    assert {h.entry.source for h in only_ecos} == {"ECOS"}
    recent = small_catalog.search(synonyms.expand("소비자물가지수"), recent_only=True)
    assert "101/DT_OLD" not in [h.entry.ds_id for h in recent]


# ------------------------------------------------------------------ 오류 분류·키 가림
def run(coro):
    return asyncio.run(coro)


def client_with(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_ecos_errors_and_masking():
    def auth(req):
        return httpx.Response(200, json={"RESULT": {"CODE": "INFO-100", "MESSAGE": "인증키가 유효하지 않습니다."}})

    async def go(handler, coro_fn):
        async with client_with(handler) as c:
            return await coro_fn(c)

    with pytest.raises(SourceError) as e:
        run(go(auth, lambda c: ecos.search(c, "901Y009", "M", "202501", "202502", ["0"])))
    assert e.value.kind == "auth" and FAKE_ECOS not in e.value.message

    def nodata(req):
        return httpx.Response(200, json={"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}})
    assert run(go(nodata, lambda c: ecos.search(c, "901Y009", "M", "209901", "209902", ["0"]))) == []

    def limited(req):
        return httpx.Response(200, json={"RESULT": {"CODE": "ERROR-602", "MESSAGE": "과도한 OpenAPI호출"}})
    with pytest.raises(SourceError) as e:
        run(go(limited, lambda c: ecos.search(c, "901Y009", "M", "202501", "202502", ["0"])))
    assert e.value.kind == "rate_limit"

    def broken(req):
        return httpx.Response(200, text=f"<html>error {FAKE_ECOS}</html>")
    with pytest.raises(SourceError) as e:
        run(go(broken, lambda c: ecos.item_list(c, "901Y009")))
    assert e.value.kind == "server" and FAKE_ECOS not in e.value.message

    def down(req):
        return httpx.Response(503)
    with pytest.raises(SourceError) as e:
        run(go(down, lambda c: ecos.item_list(c, "901Y009")))
    assert e.value.kind == "server"

    def timeout(req):
        raise httpx.ConnectTimeout("timeout", request=req)
    with pytest.raises(SourceError) as e:
        run(go(timeout, lambda c: ecos.item_list(c, "901Y009")))
    assert e.value.kind == "network"

    log = []

    def ok(req):
        return httpx.Response(200, json={"StatisticItemList": {"list_total_count": 0, "row": []}})
    run(go(ok, lambda c: ecos.item_list(c, "901Y009", log=log)))
    assert log and FAKE_ECOS not in log[0] and "{ECOS_API_KEY}" in log[0]


def test_kosis_errors_and_masking():
    async def go(handler, coro_fn):
        async with client_with(handler) as c:
            return await coro_fn(c)

    def err(code, msg):
        return lambda req: httpx.Response(200, json={"err": code, "errMsg": msg})

    with pytest.raises(SourceError) as e:
        run(go(err("11", "유효하지 않은 인증KEY입니다."), lambda c: kosis.meta(c, "101", "DT_1J22003", "ITM")))
    assert e.value.kind == "auth"
    assert run(go(err("30", "데이터가 존재하지 않습니다."), lambda c: kosis.data(c, "101", "X", ["T"], [["T10"]], "M", "209901", "209912"))) == []
    with pytest.raises(SourceError) as e:
        run(go(err("21", "해당 통계표가 존재하지 않습니다."), lambda c: kosis.data(c, "101", "X", ["T"], [["T10"]], "M", "202501", "202512")))
    assert e.value.kind == "bad_request"
    with pytest.raises(SourceError) as e:
        run(go(err("40", "1분간 호출가능건수(200건)가 초과되었습니다."), lambda c: kosis.search(c, "물가")))
    assert e.value.kind == "rate_limit"

    seen = {}
    log = []

    def capture(req):
        raw = req.url.query.decode()
        seen.update(dict(x.split("=", 1) for x in raw.split("&")))
        return httpx.Response(200, json=[])
    run(go(capture, lambda c: kosis.data(c, "101", "DT_1J22003", ["T"], [["T10", "A.*AA"], "ALL"], "M", "202501", "202512", log=log)))
    # '+'와 '*'는 인코딩하지 않고 그대로 보낸다(KOSIS가 %2A를 거절)
    assert seen["itmId"] == "T+" and seen["objL1"] == "T10+A.*AA+" and seen["objL2"] == "ALL"
    assert seen["jsonVD"] == "Y" and seen["prdSe"] == "M"
    assert FAKE_KOSIS not in log[0] and "{KOSIS_API_KEY}" in log[0]


# ------------------------------------------------------------------ 응답 정리
KOSIS_DET = {
    "source": "KOSIS", "id": "101/DT_1J22003", "dims": [
        {"key": "ITEM", "name": "항목", "items": [{"code": "T", "name": "소비자물가지수(총지수)"}]},
        {"key": "C1", "name": "시도별", "items": [{"code": "T10", "name": "전국"}, {"code": "T11", "name": "서울특별시"}]},
    ],
}


def test_kosis_fetch_normalize():
    def handler(req):
        p = dict(x.split("=", 1) for x in req.url.query.decode().split("&"))
        assert p["objL1"] == "T10+T11+"
        rows = []
        for c, n in (("T10", "전국"), ("T11", "서울특별시")):
            for prd, v in (("202501", "115.71"), ("202502", "116.08")):
                rows.append({"ORG_ID": "101", "TBL_ID": "DT_1J22003", "ITM_ID": "T", "ITM_NM": "소비자물가지수(총지수)",
                             "C1": c, "C1_NM": n, "C1_OBJ_NM": "시도별", "UNIT_NM": "2020＝100", "PRD_SE": "M",
                             "PRD_DE": prd, "DT": v, "LST_CHN_DE": "2026-07-22"})
        return httpx.Response(200, json=rows)

    async def go():
        async with client_with(handler) as c:
            return await query._fetch_kosis(c, KOSIS_DET, {"sel": {"ITEM": ["T"], "C1": ["T10", "T11"]}},
                                             "M", "2025-01", "2025-02", [], [])
    series, rows = run(go())
    assert len(series) == 2 and series[0]["unit"] == "2020=100"
    assert rows[0] == [0, "2025-01", "115.71"]
    query._label(series, ["항목", "시도별"])
    assert [s["label"] for s in series] == ["전국", "서울특별시"]


def test_kosis_cell_limit_split():
    calls = []

    def handler(req):
        p = dict(x.split("=", 1) for x in req.url.query.decode().split("&"))
        calls.append((p["startPrdDe"], p["endPrdDe"]))
        return httpx.Response(200, json=[])
    big = {"source": "KOSIS", "id": "101/X", "dims": [
        {"key": "ITEM", "name": "항목", "items": [{"code": f"I{i}", "name": f"i{i}"} for i in range(50)]},
        {"key": "C1", "name": "지역", "items": [{"code": f"R{i}", "name": f"r{i}"} for i in range(20)]}]}

    async def go():
        async with client_with(handler) as c:
            return await query._fetch_kosis(c, big, {"sel": {"ITEM": "ALL", "C1": "ALL"}}, "M", "2001-01", "2025-12", [], [])
    with pytest.raises(SourceError) as e:   # 자료가 비어 있으면 결과 없음
        run(go())
    assert e.value.kind == "no_data"
    # 1,000칸/시점 → 한 번에 40시점까지 → 300개월이면 8번
    assert len(calls) == 8 and calls[0] == ("200101", "200404") and calls[-1][1] == "202512"


def test_ecos_fetch_normalize():
    det = {"source": "ECOS", "id": "141Y002", "dims": [
        {"key": "Group1", "name": "계정항목", "items": [{"code": "100000", "name": "원화예금"}]},
        {"key": "Group2", "name": "지역", "items": [{"code": "X00", "name": "전국"}, {"code": "A00", "name": "서울"}]}]}
    urls = []

    def handler(req):
        urls.append(str(req.url))
        return httpx.Response(200, json={"StatisticSearch": {"list_total_count": 1, "row": [
            {"STAT_CODE": "141Y002", "ITEM_CODE1": "100000", "ITEM_NAME1": "원화예금", "ITEM_CODE2": "X00", "ITEM_NAME2": "전국",
             "UNIT_NAME": "십억원", "WGT": None, "TIME": "2025", "DATA_VALUE": "2189735.6"}]}})

    async def go():
        async with client_with(handler) as c:
            return await query._fetch_ecos(c, det, {"sel": {"Group1": ["100000"], "Group2": "ALL"}}, "A", "2025", "2025", [], [])
    series, rows = run(go())
    assert len(urls) == 1 and urls[0].endswith("/141Y002/A/2025/2025/100000/%3F")   # '?'(전체)는 경로 안에 %3F
    assert rows == [[0, "2025", "2189735.6"]]


# ------------------------------------------------------------------ 내보내기
def sample_result():
    return {
        "spec": {}, "hash": "x", "source": "KOSIS", "source_label": "KOSIS 국가통계포털", "id": "101/DT_1J22003",
        "ids": {"기관ID": "101", "통계표ID": "DT_1J22003"}, "name": "소비자물가지수(2020=100)", "org_name": "국가데이터처",
        "url": "https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_1J22003", "path": "물가",
        "cycle": "M", "cycle_label": "월", "start": "2025-01", "end": "2025-02", "dim_names": ["항목", "시도별"],
        "series": [
            {"codes": {"ITEM": "T", "C1": "T10"}, "names": {"항목": "소비자물가지수(총지수)", "시도별": "전국"}, "unit": "2020=100", "label": "전국"},
            {"codes": {"ITEM": "T", "C1": "T11"}, "names": {"항목": "소비자물가지수(총지수)", "시도별": "서울특별시"}, "unit": "2020=100", "label": "서울특별시"}],
        "rows": [[0, "2025-01", "115.71"], [0, "2025-02", "116.08"], [1, "2025-01", "115.90"], [1, "2025-02", "-"]],
        "times": ["2025-01", "2025-02"], "row_count": 4, "fetched_at": "2026-11-13 10:00:00",
        "requests": ["https://kosis.kr/openapi/Param/statisticsParameterData.do?apiKey={KOSIS_API_KEY}&x=1"],
        "warnings": [], "selection_text": "항목: 소비자물가지수(총지수)[T] / 시도별: 전국[T10], 서울특별시[T11]",
        "notes": ["주석 하나"], "program": "KR Public Data Hub test",
    }


def test_exports():
    res = sample_result()
    text = exporter.csv_bytes(res, "long").decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    head = rows[0]
    assert head[:5] == ["원천", "작성기관", "기관ID", "통계표ID", "통계표명"]
    assert "수집시점" in head and "시도별_코드" in head
    vi = head.index("값")
    assert [r[vi] for r in rows[1:]] == ["115.71", "116.08", "115.90", "-"]   # 원천 문자열 그대로(115.90의 0 유지)
    wide_text = exporter.csv_bytes(res, "wide").decode("utf-8-sig")
    lines = wide_text.splitlines()
    assert lines[0].startswith("# 원천 시스템: KOSIS") and any(l.startswith("# 통계표ID: DT_1J22003") for l in lines)
    assert any(l.startswith("# 선택 항목·분류: 항목: 소비자물가지수(총지수)[T] / 시도별: 전국[T10], 서울특별시[T11]") for l in lines)
    wide = list(csv.reader(io.StringIO("\n".join(l for l in lines if not l.startswith("#")))))
    assert wide[0] == ["시점", "전국", "서울특별시"] and wide[2] == ["2025-02", "116.08", "-"]
    import pandas as pd
    df = pd.read_csv(io.StringIO(wide_text), comment="#", dtype=str)
    assert list(df.columns) == ["시점", "전국", "서울특별시"] and df.iloc[0, 1] == "115.71"
    wb = load_workbook(io.BytesIO(exporter.xlsx_bytes(res)))
    assert wb.sheetnames == ["데이터(가로형)", "데이터(세로형)", "출처·조회조건"]
    assert wb["데이터(가로형)"]["B2"].value == 115.71 and wb["데이터(가로형)"]["C3"].value == "-"
    prov = {r[0].value: r[1].value for r in wb["출처·조회조건"].iter_rows(min_row=2)}
    assert prov["통계표ID"] == "DT_1J22003" and prov["수집 시점(한국시간)"] == "2026-11-13 10:00:00"
    assert "{KOSIS_API_KEY}" in prov["API 호출 1(인증키 가림)"]
    z = zipfile.ZipFile(io.BytesIO(exporter.zip_bytes([res, res])))
    names = z.namelist()
    assert "00_출처_조회조건.csv" in names and sum(n.endswith(".xlsx") for n in names) == 2
    assert len(set(names)) == len(names)


def test_spec_hash_stable():
    a = {"source": "KOSIS", "id": "1", "cycle": "M", "start": "2020-01", "end": "2020-12", "sel": {"A": ["1"]}, "name": "x"}
    b = dict(a, name="다른 이름")
    assert query.spec_hash(a) == query.spec_hash(b)


# ------------------------------------------------------------------ 독립 검증(codex) 지적 회귀 테스트
def test_bad_dates_are_period_errors():
    for cyc, v in (("D", "2025-02-30"), ("SM", "2025-13-S1"), ("M", "2025-00")):
        with pytest.raises(periods.PeriodError):
            periods.to_display(cyc, v)


def test_more_error_kinds():
    async def go(handler, coro_fn):
        async with client_with(handler) as c:
            return await coro_fn(c)
    with pytest.raises(SourceError) as e:
        run(go(lambda r: httpx.Response(200, json={"err": "12", "errMsg": "인증키 기간만료"}), lambda c: kosis.search(c, "물가")))
    assert e.value.kind == "auth"
    for status, kind in ((401, "auth"), (403, "auth"), (429, "rate_limit")):
        with pytest.raises(SourceError) as e:
            run(go(lambda r, s=status: httpx.Response(s, headers={"Retry-After": "60"}), lambda c: ecos.item_list(c, "901Y009")))
        assert e.value.kind == kind


def test_source_message_scrubbed():
    from urllib.parse import quote
    config.get_api_key("ECOS")
    config.get_api_key("KOSIS")

    def echo(req):   # 원천이 오류 메시지에 키를 되돌려 주는 경우
        return httpx.Response(200, json={"err": "21", "errMsg": f"bad key {FAKE_KOSIS} / {quote(FAKE_KOSIS, safe='')}"})

    async def go():
        async with client_with(echo) as c:
            return await kosis.search(c, "x")
    with pytest.raises(SourceError) as e:
        run(go())
    assert FAKE_KOSIS not in e.value.message and "<KEY>" in e.value.message
    assert FAKE_KOSIS not in json.dumps(e.value.to_dict())
    # 소문자 %hh 인코딩, 오류 code 칸에 되돌아온 키도 가린다(codex 재검증 N4)
    import re as _re
    special = "AbC+/=xyz"
    monkey = dict(config._KEY_CACHE)
    config._KEY_CACHE["KOSIS"] = special
    try:
        low = _re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), quote(special, safe=""))
        err = SourceError("KOSIS", "server", f"x {low} y", code=f"E{special}")
        assert special not in json.dumps(err.to_dict()) and low not in err.message and "<KEY>" in (err.code or "")
    finally:
        config._KEY_CACHE.clear(); config._KEY_CACHE.update(monkey)


def test_conflicting_duplicates_warn():
    def handler(req):
        row = {"ITM_ID": "T", "ITM_NM": "x", "C1": "T10", "C1_NM": "전국", "UNIT_NM": "u", "PRD_DE": "202501"}
        return httpx.Response(200, json=[{**row, "DT": "10"}, {**row, "DT": "99"}, {**row, "DT": "10"}])
    warnings = []

    async def go():
        async with client_with(handler) as c:
            return await query._fetch_kosis(c, KOSIS_DET, {"sel": {"ITEM": ["T"], "C1": ["T10"]}}, "M", "2025-01", "2025-01", [], warnings)
    series, rows = run(go())
    assert rows == [[0, "2025-01", "10"]] and warnings and "1번" in warnings[0]


def _pages(pages, service="StatisticSearch"):
    """요청 순서대로 페이지 응답을 돌려주는 가짜 ECOS. 'INFO200'이면 결과 없음 오류."""
    calls = []

    def handler(req):
        calls.append(str(req.url))
        p = pages[min(len(calls) - 1, len(pages) - 1)]
        if p == "INFO200":
            return httpx.Response(200, json={"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}})
        total, n, start = p
        return httpx.Response(200, json={service: {"list_total_count": total,
                                                   "row": [{"ITEM_CODE1": "A", "TIME": str(start + i), "DATA_VALUE": str(i)}
                                                           for i in range(n)]}})
    return handler, calls


def test_ecos_paging_policy():
    async def go(handler, notes):
        async with client_with(handler) as c:
            return await ecos.search(c, "X", "A", "2013", "2025", ["A"], notes=notes)
    # 1) total 13, 첫 페이지 9행 → 한 번 더 확인 요청 → 나머지 4행까지 받음(codex 재현: 예전엔 9행에서 멈춤)
    h, calls = _pages([(13, 9, 0), (13, 4, 9)])
    notes = []
    assert len(run(go(h, notes))) == 13 and len(calls) == 2 and notes == []
    # 2) 10,000 → 3 → 9,997 행: 끝까지 받음(예전엔 10,003행에서 멈춤)
    h, calls = _pages([(20000, 10000, 0), (20000, 3, 10000), (20000, 9997, 10003)])
    assert len(run(go(h, []))) == 20000
    # 3) 10,000행 뒤 INFO-200: 앞 10,000행을 버리지 않고 끝 + 차이를 notes에(예전엔 빈 목록)
    h, calls = _pages([(20000, 10000, 0), "INFO200"])
    notes = []
    assert len(run(go(h, notes))) == 10000 and notes == [(20000, 10000)]
    # 4) ECOS가 건수를 부풀려 알림(13이라 하고 9행, 다음 확인은 빈 결과) → 9행 + notes
    h, calls = _pages([(13, 9, 0), "INFO200"])
    notes = []
    assert len(run(go(h, notes))) == 9 and notes == [(13, 9)]
    # 5) 첫 페이지부터 결과 없음 → 빈 목록
    h, calls = _pages(["INFO200"])
    assert run(go(h, [])) == []


def test_live_search_keeps_known_cycles(tmp_path, monkeypatch):
    monkeypatch.setattr(cat, "CATALOG_DB", tmp_path / "c.sqlite")
    con = cat.connect()
    base = dict(source="KOSIS", ds_id="101/X", org_id="101", name="연간 표", path="p")
    cat.upsert_dataset(con, {**base, "ranges": {"*": ["2000", "2025"]}, "end_year": 2025})
    cat.upsert_dataset(con, {**base, "ranges": {"A": ["2000", "2025"]}, "end_year": 2025})   # 상세로 확인
    cat.upsert_dataset(con, {**base, "ranges": {"*": ["2000", "2025"]}, "end_year": 2025,
                             "description": "검색 보강"})                                       # 실시간 검색 보강
    row = con.execute("SELECT ranges, description FROM datasets WHERE ds_id='101/X'").fetchone()
    assert json.loads(row[0]) == {"A": ["2000", "2025"]} and row[1] == "검색 보강"
    con.commit(); con.close()
    c = cat.Catalog()
    hits = c.search([("연간 표", "입력어", 1.0)], cycle="M")
    assert not hits      # 연간 전용으로 확인된 표는 월 필터에서 빠진다


def test_excel_precision_guard():
    assert exporter._num("123.45") == 123.45
    assert exporter._num("1e309") == "1e309" and exporter._num("1e-400") == "1e-400"   # Excel에 못 담는 범위는 글자로
    assert exporter._num("0") == 0 and exporter._num("-0.5") == -0.5
    assert exporter._num("12345678901234567") == "12345678901234567"   # 17자리는 글자 그대로
    assert exporter._num("-") == "-" and exporter._num(None) is None



def test_scrub_mixed_case_percent():
    from urllib.parse import quote, quote_plus
    special = "Ab C+/=xy%z"
    saved = dict(config._KEY_CACHE)
    config._KEY_CACHE.clear()
    config._KEY_CACHE["X"] = special
    try:
        q = quote(special, safe="")
        import re as _re
        hexlow = _re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), q)   # 16진수만 소문자
        variants = [special, q, hexlow, quote_plus(special),
                    q.replace("%2B", "%2b"), quote_plus(special).replace("%2F", "%2f")]   # 혼합 대소문자(codex 3차 N4)
        for v in variants:
            assert "<KEY>" in config.scrub(f"a {v} b") and v not in config.scrub(f"a {v} b"), v
        assert config.scrub("ab c+/=xy%z") == "ab c+/=xy%z"     # 키 본문 대소문자가 다르면 건드리지 않음
    finally:
        config._KEY_CACHE.clear(); config._KEY_CACHE.update(saved)


def test_incomplete_item_list_warns_and_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(cat, "CATALOG_DB", tmp_path / "c.sqlite")
    monkeypatch.setattr(ecos, "PAGE", 10)
    calls = []

    def handler(req):
        u = str(req.url)
        calls.append(u)
        if "StatisticWord" in u:
            return httpx.Response(200, json={"RESULT": {"CODE": "INFO-200", "MESSAGE": "없음"}})
        if "/1/10/" in u:      # 첫 페이지: 11개라 알리고 10개
            rows = [{"GRP_CODE": "Group1", "GRP_NAME": "항목", "ITEM_CODE": str(i), "ITEM_NAME": f"i{i}", "CYCLE": "M",
                     "START_TIME": "202001", "END_TIME": "202512", "UNIT_NAME": "u"} for i in range(10)]
            return httpx.Response(200, json={"StatisticItemList": {"list_total_count": 11, "row": rows}})
        return httpx.Response(200, json={"StatisticItemList": {"list_total_count": 11, "row": []}})

    con = cat.connect()
    c = cat.Catalog()

    async def go():
        async with client_with(handler) as cl:
            d1 = await query.detail(cl, con, c, "ECOS", "ZZZ")
            d2 = await query.detail(cl, con, c, "ECOS", "ZZZ")
            return d1, d2
    d1, d2 = run(go())
    assert d1["warnings"] and "11" in d1["warnings"][0]
    item_calls = [u for u in calls if "StatisticItemList" in u]
    assert len(item_calls) == 4           # 두 번 모두 새로 받음(덜 온 상세는 캐시하지 않음)



def test_scrub_literal_key_exact():
    saved = dict(config._KEY_CACHE)
    config._KEY_CACHE.clear()
    config._KEY_CACHE["X"] = "ab%2Bcd"
    try:
        assert config.scrub("x ab%2Bcd y") == "x <KEY> y"
        assert config.scrub("x ab%2bcd y") == "x ab%2bcd y"     # 원문과 다른 글자(codex 4차 N6)는 가리지 않음
        assert config.scrub("x ab%252Bcd y") == "x <KEY> y"      # 인코딩형(%25 = '%')은 가림
        assert config.scrub("x ab%252bcd y") == "x ab%252bcd y"  # 풀면 'ab%2bcd' = 다른 문자열이므로 가리지 않음
    finally:
        config._KEY_CACHE.clear(); config._KEY_CACHE.update(saved)


def test_build_skips_incomplete_item_lists(tmp_path, monkeypatch):
    from datahub import build_catalog
    monkeypatch.setattr(cat, "CATALOG_DB", tmp_path / "c.sqlite")
    monkeypatch.setattr(ecos, "PAGE", 10)

    def handler(req):
        u = str(req.url)
        if "StatisticTableList" in u:
            return httpx.Response(200, json={"StatisticTableList": {"list_total_count": 2, "row": [
                {"STAT_CODE": "OK1", "STAT_NAME": "1. 정상", "P_STAT_CODE": "*", "SRCH_YN": "Y", "CYCLE": "M"},
                {"STAT_CODE": "BAD", "STAT_NAME": "2. 덜 옴", "P_STAT_CODE": "*", "SRCH_YN": "Y", "CYCLE": "M"}]}})
        item = lambda i: {"GRP_CODE": "Group1", "GRP_NAME": "g", "ITEM_CODE": str(i), "ITEM_NAME": f"i{i}", "CYCLE": "M",
                          "START_TIME": "202001", "END_TIME": "202512", "UNIT_NAME": "u"}
        if "/OK1" in u:
            return httpx.Response(200, json={"StatisticItemList": {"list_total_count": 3, "row": [item(i) for i in range(3)]}})
        if "/1/10/BAD" in u:
            return httpx.Response(200, json={"StatisticItemList": {"list_total_count": 11, "row": [item(i) for i in range(10)]}})
        return httpx.Response(200, json={"StatisticItemList": {"list_total_count": 11, "row": []}})

    con = cat.connect()

    async def go():
        async with client_with(handler) as c:
            await build_catalog.build_ecos(c, con)
    run(go())
    have = {r[0] for r in con.execute("SELECT DISTINCT stat_code FROM ecos_items")}
    assert have == {"OK1"}            # 덜 온 BAD 표의 항목은 저장하지 않음 → 상세에서 새로 받아 경고


def test_rebuild_invalidates_old_detail_cache(tmp_path, monkeypatch):
    """codex 5차 N5 잔존: 정상 상세 캐시 → 재구축 때 덜 옴 → 상세가 예전 캐시를 내면 안 됨 → ecos-missing 회복 뒤 새 목록."""
    from datahub import build_catalog
    monkeypatch.setattr(cat, "CATALOG_DB", tmp_path / "c.sqlite")
    monkeypatch.setattr(ecos, "PAGE", 10)
    state = {"mode": "ok3"}
    calls = []
    item = lambda i: {"GRP_CODE": "Group1", "GRP_NAME": "g", "ITEM_CODE": str(i), "ITEM_NAME": f"i{i}", "CYCLE": "M",
                      "START_TIME": "202001", "END_TIME": "202512", "UNIT_NAME": "u"}

    def handler(req):
        u = str(req.url)
        if "StatisticWord" in u:
            return httpx.Response(200, json={"RESULT": {"CODE": "INFO-200", "MESSAGE": "없음"}})
        if "StatisticTableList" in u:
            return httpx.Response(200, json={"StatisticTableList": {"list_total_count": 1, "row": [
                {"STAT_CODE": "T1", "STAT_NAME": "1. 표", "P_STAT_CODE": "*", "SRCH_YN": "Y", "CYCLE": "M"}]}})
        calls.append(u)
        m = state["mode"]
        if m == "ok3":
            return httpx.Response(200, json={"StatisticItemList": {"list_total_count": 3, "row": [item(i) for i in range(3)]}})
        if m == "short":      # 11개라 알리고 10개 뒤 빈 응답
            rows = [item(i) for i in range(10)] if "/1/10/" in u else []
            return httpx.Response(200, json={"StatisticItemList": {"list_total_count": 11, "row": rows}})
        rows = [item(i) for i in range(10)] if "/1/10/" in u else [item(10)]   # 정상 11개
        return httpx.Response(200, json={"StatisticItemList": {"list_total_count": 11, "row": rows}})

    con = cat.connect()

    async def go():
        async with client_with(handler) as cl:
            await build_catalog.build_ecos(cl, con)
            c = cat.Catalog()
            d0 = await query.detail(cl, con, c, "ECOS", "T1")          # 정상 3개 → 캐시
            state["mode"] = "short"
            await build_catalog.build_ecos(cl, con)                     # 전체 재구축, 덜 옴 → 저장 안 함
            n_before = len(calls)
            d1 = await query.detail(cl, con, c, "ECOS", "T1")
            d2 = await query.detail(cl, con, c, "ECOS", "T1")
            fetched = len(calls) - n_before
            state["mode"] = "ok11"
            await build_catalog.build_ecos(cl, con, missing_only=True)  # 회복
            d3 = await query.detail(cl, con, c, "ECOS", "T1")
            return d0, d1, d2, fetched, d3
    d0, d1, d2, fetched, d3 = run(go())
    assert len(d0["dims"][0]["items"]) == 3 and not d0["warnings"]
    assert d1["warnings"] and d2["warnings"] and fetched == 4          # 예전 3개짜리 캐시가 아니라 새로 받아 경고
    assert len(d3["dims"][0]["items"]) == 11 and not d3["warnings"]    # 회복 뒤 새 목록
    assert con.execute("SELECT COUNT(*) FROM ecos_items WHERE stat_code='T1'").fetchone()[0] == 11
