"""OpenDART 연결 테스트(네트워크 없이 모의 응답): 회사 목록·검색, 재무제표 연도 잇기, 주요계정, 공시 목록, 내보내기, 키 가림.

    python3 -m pytest -q tests/test_dart.py
"""
from __future__ import annotations

import asyncio
import csv
import io
import zipfile
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from openpyxl import load_workbook

from datahub import catalog as cat
from datahub import config, dart_query, exporter, query
from datahub.errors import SourceError
from datahub.sources import dart

FAKE_DART = "DARTFAKEKEY0123456789abcdefABCDEF0123456"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENDART_API_KEY", FAKE_DART)
    monkeypatch.setattr(cat, "CATALOG_DB", tmp_path / "c.sqlite")
    config._KEY_CACHE.clear()
    query._results.clear()
    yield
    config._KEY_CACHE.clear()
    query._results.clear()


def run(coro):
    return asyncio.run(coro)


def client_with(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def q(req) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()}


def corp_zip(rows) -> bytes:
    xml = "<result>" + "".join(
        f"<list><corp_code>{c}</corp_code><corp_name>{n}</corp_name><corp_eng_name>{e}</corp_eng_name>"
        f"<stock_code>{s}</stock_code><modify_date>20260101</modify_date></list>" for c, n, e, s in rows) + "</result>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("CORPCODE.xml", xml.encode("utf-8"))
    return buf.getvalue()


CORPS = [("00126380", "삼성전자", "SAMSUNG ELECTRONICS CO,.LTD", "005930"),
         ("00252074", "삼성전자판매", "Samsung Electronics Sales", " "),
         ("00999999", "주식회사 삼성전자서비스", "", ""),
         ("00111111", "LG생활건강", "LG HOUSEHOLD AND HEALTH CARE LTD.", "051900")]

COMPANY = {"status": "000", "message": "정상", "corp_code": "00126380", "corp_name": "삼성전자(주)", "stock_name": "삼성전자",
           "corp_name_eng": "SAMSUNG", "stock_code": "005930", "ceo_nm": "대표", "corp_cls": "Y", "induty_code": "264",
           "est_dt": "19690113", "acc_mt": "12"}


def acc(sj, aid, nm, amt, add=None, detail="-", ord_=1, rc="20260310000001"):
    r = {"rcept_no": rc, "sj_div": sj, "sj_nm": sj, "account_id": aid, "account_nm": nm, "account_detail": detail,
         "thstrm_amount": amt, "ord": str(ord_), "currency": "KRW"}
    if add is not None:
        r["thstrm_add_amount"] = add
    return r


# ------------------------------------------------------------------ 회사 목록·검색
def test_corp_list_parse_and_search():
    def h(req):
        assert "crtfc_key" in str(req.url) and "corp_code" not in str(req.url)   # 키 말고 아무것도 보내지 않음
        return httpx.Response(200, content=corp_zip(CORPS))

    con = cat.connect()

    async def go():
        async with client_with(h) as c:
            return await dart_query.refresh_corps(c, con)
    assert run(go()) == 4
    info = dart_query.corps_info(con)
    assert info["count"] == 4 and info["listed"] == 2          # 공백뿐인 종목코드는 비상장
    r = dart_query.search_corps(con, "삼성전자")
    assert [x["corp_code"] for x in r["results"]][:1] == ["00126380"]   # 정확히 같은 이름·상장사 먼저
    assert {x["corp_code"] for x in r["results"]} == {"00126380", "00252074", "00999999"}   # '주식회사' 떼고도 찾음
    assert dart_query.search_corps(con, "005930")["results"][0]["corp_code"] == "00126380"
    assert dart_query.search_corps(con, "household")["results"][0]["corp_code"] == "00111111"
    assert dart_query.search_corps(con, "(주)LG 생활건강")["results"][0]["corp_code"] == "00111111"
    assert [x["corp_code"] for x in dart_query.search_corps(con, "삼성전자", listed_only=True)["results"]] == ["00126380"]
    assert dart_query.search_corps(con, "  ")["count"] == 0


def test_corp_list_errors():
    def bad_key(req):
        return httpx.Response(200, content="<?xml version='1.0'?><result><status>010</status><message>등록되지 않은 키입니다.</message></result>".encode())

    def junk(req):
        return httpx.Response(200, content=b"<html>oops</html>")

    async def go(h):
        async with client_with(h) as c:
            return await dart.corp_codes(c)
    with pytest.raises(SourceError) as e:
        run(go(bad_key))
    assert e.value.kind == "auth" and e.value.code == "010"
    with pytest.raises(SourceError) as e:
        run(go(junk))
    assert e.value.kind == "server"


@pytest.mark.parametrize("code,kind", [("013", "no_data"), ("020", "rate_limit"), ("021", "too_large"), ("011", "auth"),
                                       ("100", "bad_request"), ("800", "server"), ("999", "server")])
def test_status_codes(code, kind):
    def h(req):
        return httpx.Response(200, json={"status": code, "message": f"m{code}"})

    async def go():
        async with client_with(h) as c:
            return await dart.company(c, "00126380")
    with pytest.raises(SourceError) as e:
        run(go())
    assert e.value.kind == kind


def test_key_masking():
    def h(req):     # 원천이 오류 메시지에 키를 되돌려 주는 경우
        return httpx.Response(200, json={"status": "100", "message": f"bad crtfc_key={FAKE_DART}"})

    log: list = []

    async def go():
        async with client_with(h) as c:
            return await dart.fin_all(c, "00126380", 2025, "11011", "CFS", log)
    with pytest.raises(SourceError) as e:
        run(go())
    assert FAKE_DART not in e.value.message and "<KEY>" in e.value.message
    assert log and all(FAKE_DART not in u and "{OPENDART_API_KEY}" in u for u in log)


# ------------------------------------------------------------------ 재무제표(전체 계정)
def fs_handler(by_year, calls=None):
    def h(req):
        p = q(req)
        if calls is not None:
            calls.append(p)
        if "company.json" in str(req.url):
            return httpx.Response(200, json=COMPANY)
        rows = by_year.get(int(p["bsns_year"]))
        if rows is None:
            return httpx.Response(200, json={"status": "013", "message": "조회된 데이타가 없습니다."})
        return httpx.Response(200, json={"status": "000", "message": "정상", "list": rows})
    return h


def fs_spec(**kw):
    s = {"source": "DART", "kind": "fs", "id": "00126380", "fs_div": "CFS", "reprt": "11011", "flow": "cum",
         "start": "2023", "end": "2025", "sel": {"SJ": ["BS", "IS", "CIS", "CF"]}}
    s.update(kw)
    return s


def grid(res):
    g = {}
    for si, t, v in res["rows"]:
        g.setdefault(res["series"][si]["label"], {})[t] = v
    return g


def test_fs_links_years_by_name_then_unique_id():
    by_year = {
        2025: [acc("BS", "ifrs-full_Assets", "자산총계", "300"),
               acc("BS", "ifrs-full_Revenue_X", "기타 유동부채", "7"),
               acc("IS", "ifrs-full_Revenue", "매출액", "100", ord_=1),
               acc("IS", "ifrs-full_ProfitLoss", "당기순이익", "10", ord_=2),
               acc("CIS", "ifrs-full_OCI", "지분 변동", "1", ord_=1), acc("CIS", "ifrs-full_OCI", "지분 변동", "2", ord_=2),
               acc("CF", "dart_X", "빈칸 계정", "")],
        2024: [acc("BS", "dart_TotalAssets", "자산총계", "290"),                       # ID 바뀜 → 이름으로 이음
               acc("BS", "dart_OtherCL", "기타유동부채 (주27)", "6"),                   # 띄어쓰기·주석 번호만 다름
               acc("IS", "ifrs-full_Revenue", "영업수익", "95"),                      # 이름 바뀜 → 하나뿐인 ID로 이음
               acc("IS", "ifrs-full_ProfitLoss", "당기순이익(손실)", "9"),             # '(손실)' 무시
               acc("CIS", "ifrs-full_OCI", "다른 이름", "3"), acc("CIS", "ifrs-full_OCI", "또 다른 이름", "4")],  # 같은 ID 두 번 → ID로 잇지 않음
    }
    calls = []
    con = cat.connect()

    async def go():
        async with client_with(fs_handler(by_year, calls)) as c:
            return await dart_query.fetch_fs(c, con, fs_spec())
    res = run(go())
    g = grid(res)
    assert g["[재무상태표] 자산총계"] == {"2024": "290", "2025": "300"}
    assert g["[재무상태표] 기타 유동부채"] == {"2024": "6", "2025": "7"}
    assert g["[손익계산서] 매출액"] == {"2024": "95", "2025": "100"}
    assert g["[손익계산서] 당기순이익"] == {"2024": "9", "2025": "10"}
    assert g["[포괄손익계산서] 지분 변동"] == {"2025": "1"} and g["[포괄손익계산서] 지분 변동 #2"] == {"2025": "2"}
    assert g["[포괄손익계산서] 다른 이름"] == {"2024": "3"} and g["[포괄손익계산서] 또 다른 이름"] == {"2024": "4"}
    names = {s["label"]: s["names"]["계정"] for s in res["series"]}
    assert names["[손익계산서] 매출액"] == "매출액 (예전 이름: 영업수익)"
    assert names["[재무상태표] 기타 유동부채"] == "기타 유동부채"       # 띄어쓰기·주석만 다른 이름은 적지 않음
    assert not any("빈칸" in s["label"] for s in res["series"])          # 금액 없는 계정은 뺌
    assert res["times"] == ["2024", "2025"]
    assert any("2023" in w for w in res["warnings"])                     # 자료 없는 연도 경고
    assert [s["codes"]["SJ"] for s in res["series"]] == sorted([s["codes"]["SJ"] for s in res["series"]],
                                                               key=dart_query.SJ_ORDER.index)
    assert {c["corp_code"] for c in calls if "bsns_year" in c} == {"00126380"}   # 원천으로 가는 것은 고유번호·연도뿐
    assert all(set(c) <= {"crtfc_key", "corp_code", "bsns_year", "reprt_code", "fs_div"} for c in calls)


def test_fs_quarterly_flow_and_errors():
    by_year = {2025: [acc("IS", "ifrs-full_Revenue", "매출액", "86", add="239"), acc("BS", "ifrs-full_Assets", "자산총계", "500", add=None),
                      acc("CF", "dart_C", "영업활동현금흐름", "70")]}
    con = cat.connect()

    async def go(spec):
        async with client_with(fs_handler(by_year)) as c:
            return await dart_query.fetch_fs(c, con, spec)
    cum = grid(run(go(fs_spec(reprt="11014", start="2025", end="2025"))))
    qq = grid(run(go(fs_spec(reprt="11014", flow="q", start="2025", end="2025"))))
    assert cum["[손익계산서] 매출액"] == {"2025-Q3": "239"} and qq["[손익계산서] 매출액"] == {"2025-Q3": "86"}
    assert cum["[재무상태표] 자산총계"] == qq["[재무상태표] 자산총계"] == {"2025-Q3": "500"}
    assert cum["[현금흐름표] 영업활동현금흐름"] == {"2025-Q3": "70"}
    with pytest.raises(SourceError) as e:
        run(go(fs_spec(start="2016", end="2017")))
    assert e.value.kind == "no_data" and "별도" in e.value.message           # 연결이 없으면 별도를 권함
    for bad, kind in [(dict(start="1999", end="2000"), "bad_period"), (dict(start="20x", end="2025"), "bad_period"),
                      (dict(fs_div="XXX"), "bad_request"), (dict(scope="zzz"), "bad_request")]:
        with pytest.raises(SourceError) as e:
            run(go(fs_spec(**bad)))
        assert e.value.kind == kind, bad
    res = run(go(fs_spec(start="2010", end="2099")))          # 2015년~올해로 잘라서 받음
    assert res["start"] == "2015" and int(res["end"]) <= 2099 and res["times"] == ["2025"]


def test_fs_year_limit(monkeypatch):
    monkeypatch.setattr(dart_query, "MAX_YEARS", 3)
    con = cat.connect()

    async def go():
        async with client_with(fs_handler({})) as c:
            return await dart_query.fetch_fs(c, con, fs_spec(start="2021", end="2025"))
    with pytest.raises(SourceError) as e:
        run(go())
    assert e.value.kind == "too_large"


def test_fs_key_scope_strips_commas_and_dedupes():
    def h(req):
        p = q(req)
        if "company.json" in str(req.url):
            return httpx.Response(200, json=COMPANY)
        assert "fnlttMultiAcnt" in str(req.url) and p["corp_code"] == "00126380"
        rows = [dict(acc("IS", "", "매출액", "333,605,938,000,000"), fs_div="CFS"),
                dict(acc("IS", "", "당기순이익(손실)", "45,206,805,000,000"), fs_div="CFS"),
                dict(acc("IS", "", "당기순이익(손실)", "45,206,805,000,000", ord_=61), fs_div="CFS"),
                dict(acc("IS", "", "매출액", "1,000"), fs_div="OFS"),
                dict(acc("BS", "", "자산총계", "566,942,110,000,000"), fs_div="CFS")]
        return httpx.Response(200, json={"status": "000", "list": rows})

    con = cat.connect()

    async def go():
        async with client_with(h) as c:
            return await dart_query.fetch_fs(c, con, fs_spec(scope="key", start="2025", end="2025"))
    res = run(go())
    g = grid(res)
    assert g == {"[재무상태표] 자산총계": {"2025": "566942110000000"}, "[손익계산서] 매출액": {"2025": "333605938000000"},
                 "[손익계산서] 당기순이익(손실)": {"2025": "45206805000000"}}
    assert "주요계정" in res["name"] and "쉼표" in " ".join(res["notes"])


# ------------------------------------------------------------------ 공시 목록
def test_disclosure_list_pages_and_cap(monkeypatch):
    monkeypatch.setattr(dart_query, "MAX_LIST_PAGES", 2)
    pages = []

    def h(req):
        p = q(req)
        if "company.json" in str(req.url):
            return httpx.Response(200, json=COMPANY)
        pages.append(int(p["page_no"]))
        n = int(p["page_no"])
        rows = [{"corp_code": "00126380", "corp_name": "삼성전자", "corp_cls": "Y", "report_nm": f"보고서{n}-{i} ",
                 "rcept_no": f"2026{n:02d}{i:04d}", "flr_nm": "삼성전자", "rcept_dt": "20261112", "rm": "유"} for i in range(100)]
        return httpx.Response(200, json={"status": "000", "total_count": 250, "total_page": 3, "list": rows})

    con = cat.connect()

    async def go():
        async with client_with(h) as c:
            return await dart_query.fetch_list(c, con, {"source": "DART", "kind": "list", "id": "00126380",
                                                         "start": "2026.11.13", "end": "20251113"})
    res = run(go())
    assert pages == [1, 2] and res["row_count"] == 200
    assert res["start"] == "2025-11-13" and res["end"] == "2026-11-13"          # 앞뒤가 바뀌어도 바로잡음
    assert any("250" in w and "200" in w for w in res["warnings"])
    r0 = res["table"]["rows"][0]
    assert r0[0] == "2026-11-12" and r0[1] == "보고서1-0 " and r0[6].endswith("rcpNo=2026010000")
    csv_text = exporter.csv_bytes(res, "long").decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(csv_text)))
    assert rows[0][:3] == ["원천", "회사 고유번호", "종목코드"] and len(rows) == 201
    wb = load_workbook(io.BytesIO(exporter.xlsx_bytes(res)))
    assert wb.sheetnames == ["목록", "출처·조회조건"] and wb["목록"].max_row == 201


# ------------------------------------------------------------------ 내보내기·조회 연결
def test_fs_export_and_query_dispatch():
    big = "566942110000000"
    by_year = {2024: [acc("BS", "a", "자산총계", "514531948000000"), acc("IS", "b", "매출액", "-5")],
               2025: [acc("BS", "a", "자산총계", big), acc("IS", "b", "매출액", "7")]}
    con = cat.connect()

    async def go(spec):
        async with client_with(fs_handler(by_year)) as c:
            return await query.fetch(c, con, None, spec)
    res = run(go(fs_spec(start="2024", end="2025")))
    assert res["hash"] and res["layout"] == "accounts"
    head, body = exporter.wide_table(res)
    assert head == ["재무제표", "계정", "세부", "계정ID", "단위", "2024", "2025"]
    assert body[0] == ["재무상태표", "자산총계", "", "a", "원", "514531948000000", big]
    wb = load_workbook(io.BytesIO(exporter.xlsx_bytes(res)))
    ws = wb["데이터(가로형)"]
    assert ws["G2"].value == int(big) and ws["G2"].number_format == "#,##0" and ws.freeze_panes == "F2"
    prov = dict(exporter.provenance(res))
    assert "2025 접수번호" in prov and "rcpNo=" in prov["2025 접수번호"]
    long_rows = list(csv.reader(io.StringIO(exporter.csv_bytes(res, "long").decode("utf-8-sig"))))
    assert long_rows[0][-4:] == ["주기", "시점", "값", "수집시점"] and len(long_rows) == 5
    # 조건이 다르면 결과 캐시도 다르다
    assert query.spec_hash(fs_spec()) != query.spec_hash(fs_spec(fs_div="OFS"))
    assert query.spec_hash(fs_spec()) != query.spec_hash(fs_spec(scope="key"))
    assert query.spec_hash({"source": "ECOS", "id": "X", "cycle": "M", "start": "a", "end": "b", "sel": {}}) == \
        query.spec_hash({"source": "ECOS", "id": "X", "cycle": "M", "start": "a", "end": "b", "sel": {}, "name": "n"})


def test_company_cache_and_bad_code():
    calls = []

    def h(req):
        calls.append(str(req.url))
        return httpx.Response(200, json=COMPANY)

    con = cat.connect()

    async def go(code):
        async with client_with(h) as c:
            return await dart_query.company(c, con, code)
    d1 = run(go("00126380"))
    d2 = run(go("00126380"))
    assert d1 == d2 and len(calls) == 1 and d1["est_dt"] == "1969-01-13" and d1["market"] == "유가증권시장"
    with pytest.raises(SourceError) as e:
        run(go("1234"))
    assert e.value.kind == "bad_request"


# ------------------------------------------------------------------ 업종 비교
def setup_corps(con, rows):
    con.executemany("INSERT INTO dart_corps VALUES (?,?,?,?,?,?,?,?)",
                    [(c, n, "", s, "20260101", dart_query.norm_name(n), ind, cls) for c, n, s, ind, cls in rows])
    con.commit()


def test_industry_refresh_and_levels():
    con = cat.connect()
    setup_corps(con, [("00000001", "가", "000001", None, None), ("00000002", "나", "000002", None, None),
                      ("00000003", "비상장", "", None, None)])
    seen = []

    def h(req):
        cc = q(req)["corp_code"]
        seen.append(cc)
        if cc == "00000002":
            return httpx.Response(200, json={"status": "013", "message": "없음"})
        return httpx.Response(200, json={**COMPANY, "corp_code": cc, "induty_code": "26422", "corp_cls": "K"})

    async def go():
        async with client_with(h) as c:
            return await dart_query.refresh_industry(c, con)
    res = run(go())
    assert sorted(seen) == ["00000001", "00000002"]                  # 상장사만
    assert res["with_code"] == 1 and res["failed"] == 1 and res["at"]
    lv = dart_query.industry_levels(con, "26422")
    assert [x["code"] for x in lv] == ["26422", "2642", "264", "26"] and all(x["count"] == 1 for x in lv)
    assert lv[2]["name"] == "통신 및 방송장비 제조업" and lv[0]["level"] == "세세분류"     # 제11차 이름

    def auth(req):
        return httpx.Response(200, json={"status": "010", "message": "키 없음"})

    async def go2():
        async with client_with(auth) as c:
            return await dart_query.refresh_industry(c, con)
    with pytest.raises(SourceError) as e:                              # 인증 오류는 4천 번 다 돌지 않고 멈춤
        run(go2())
    assert e.value.kind == "auth"


def test_industry_compare_table():
    con = cat.connect()
    setup_corps(con, [("00000001", "큰회사", "000001", "10801", "Y"), ("00000002", "작은회사", "000002", "10802", "K"),
                      ("00000003", "별도만", "000003", "108", "K"), ("00000004", "자료없음", "000004", "1080", "K"),
                      ("00000005", "다른업종", "000005", "264", "Y")])
    calls = []

    def row(cc, fs, sj, nm, amt, dt="2025.01.01 ~ 2025.12.31"):
        return {"corp_code": cc, "fs_div": fs, "sj_div": sj, "account_nm": nm, "thstrm_amount": amt, "thstrm_dt": dt,
                "rcept_no": f"R{cc}", "currency": "KRW"}

    def h(req):
        p = q(req)
        calls.append(p)
        assert "fnlttMultiAcnt" in str(req.url)
        rows = [row("00000001", "CFS", "IS", "매출액", "300,000"), row("00000001", "CFS", "IS", "영업이익", "30"),
                row("00000001", "CFS", "IS", "당기순이익(손실)", "20"), row("00000001", "CFS", "IS", "당기순이익(손실)", "20"),
                row("00000001", "CFS", "BS", "자산총계", "1,000"), row("00000001", "OFS", "IS", "매출액", "5"),
                row("00000002", "CFS", "IS", "매출액", "100,000"), row("00000002", "CFS", "BS", "부채총계", "-7"),
                row("00000003", "OFS", "IS", "매출액", "100,000", dt="2024.07.01 ~ 2025.06.30")]
        return httpx.Response(200, json={"status": "000", "list": rows})

    async def go(spec):
        async with client_with(h) as c:
            return await query.fetch(c, con, None, spec)
    res = run(go({"source": "DART", "kind": "industry", "id": "108", "start": "2025", "end": "2025", "reprt": "11011", "fs_div": "CFS"}))
    assert len(calls) == 1 and set(calls[0]["corp_code"].split(",")) == {"00000001", "00000002", "00000003", "00000004"}
    rows = res["table"]["rows"]
    assert [r[1] for r in rows] == ["큰회사", "별도만", "작은회사", "자료없음"]       # 같은 매출이면 이름순, 자료 없는 회사는 맨 아래
    assert rows[-1][0] is None and rows[-1][7] == "자료 없음" and rows[-1][10:17] == [None] * 7   # 빼지 않고 빈칸으로 남김
    assert [r[0] for r in rows[:3]] == [1, 2, 3]
    big = rows[0]
    assert big[10:17] == ["300000", "30", "20", "1000", None, None, "60.00"]       # 매출 비중 = 300,000 / 500,000
    assert {r[1]: r[7] for r in rows}["별도만"] == "별도" and {r[1]: r[16] for r in rows}["작은회사"] == "20.00"
    assert any("자료없음" in w for w in res["warnings"]) and any("별도" in w for w in res["warnings"])
    assert any("결산 기간" in w for w in res["warnings"])
    assert res["ids"]["업종명"] == "기타 식품 제조업"          # 제11차 108(제10차 이름 '동물용 사료'가 아님)
    wb = load_workbook(io.BytesIO(exporter.xlsx_bytes(res)))
    ws = wb["목록"]
    assert ws.cell(2, 11).value == 300000 and ws.cell(2, 11).number_format == "#,##0" and ws.cell(2, 17).value == 60.0
    assert ws.cell(2, 3).value == "000001"                              # 종목코드는 글자 그대로(앞 0 유지)
    with pytest.raises(SourceError) as e:
        run(go({"source": "DART", "kind": "industry", "id": "999", "start": "2025", "end": "2025"}))
    assert e.value.kind == "no_data"
    with pytest.raises(SourceError) as e:
        run(go({"source": "DART", "kind": "industry", "id": "1a", "start": "2025", "end": "2025"}))
    assert e.value.kind == "bad_request"


def test_search_wildcards_literal():
    con = cat.connect()
    setup_corps(con, [("00000001", "가나", "000001", None, None), ("00000002", "가_나%", "", None, None)])
    assert [r["corp_code"] for r in dart_query.search_corps(con, "%")["results"]] == ["00000002"]
    assert [r["corp_code"] for r in dart_query.search_corps(con, "_")["results"]] == ["00000002"]


def test_delisted_excluded():
    con = cat.connect()
    setup_corps(con, [("00000001", "현재상장", "000001", "26422", "Y"), ("00000002", "상장폐지", "000002", "26422", "E"),
                      ("00000003", "모름", "000003", None, None)])
    assert dart_query.industry_levels(con, "26422")[0]["count"] == 1
    info = dart_query.industry_info(con)
    assert info["listed"] == 2 and info["with_code"] == 1             # 폐지(E)는 빼고, 아직 모르는 회사는 넣고 셈
    r = {x["corp_code"]: x for x in dart_query.search_corps(con, "상장폐지")["results"]}
    assert r["00000002"]["listed"] is False
    assert dart_query.search_corps(con, "상장폐지", listed_only=True)["count"] == 0



# ------------------------------------------------------------------ codex 7차 지적 회귀
def test_revenue_and_expense_not_joined():
    """'이자(수익)'과 '이자(비용)'은 이름·ID가 모두 달라 한 줄로 잇지 않는다(codex 7차 높음 1)."""
    by_year = {2025: [acc("IS", "ifrs-full_InterestIncome", "이자(수익)", "100")],
               2024: [acc("IS", "ifrs-full_FinanceCosts", "이자(비용)", "90")]}
    con = cat.connect()

    async def go():
        async with client_with(fs_handler(by_year)) as c:
            return await dart_query.fetch_fs(c, con, fs_spec(start="2024", end="2025"))
    g = grid(run(go()))
    assert g == {"[손익계산서] 이자(수익)": {"2025": "100"}, "[손익계산서] 이자(비용)": {"2024": "90"}}


def test_id_repeated_in_table_not_used_for_linking():
    """자본변동표에서 같은 ID가 세부(열)만 달리 두 번 나오면, 이름이 다른 계정을 ID로 잇지 않는다(codex 7차 높음 2)."""
    by_year = {2025: [acc("SCE", "x", "새 이름", "1", detail="자본금"), acc("SCE", "x", "새 이름2", "2", detail="이익잉여금")],
               2024: [acc("SCE", "x", "옛 이름", "3", detail="자본금"), acc("SCE", "x", "옛 이름2", "4", detail="이익잉여금")]}
    con = cat.connect()

    async def go():
        async with client_with(fs_handler(by_year)) as c:
            return await dart_query.fetch_fs(c, con, fs_spec(start="2024", end="2025", sel={"SJ": ["SCE"]}))
    res = run(go())
    assert all(len({t for si, t, _ in res["rows"] if si == i}) == 1 for i in range(len(res["series"])))
    assert len(res["series"]) == 4


def test_alnum_stock_code_and_raw_strings():
    con = cat.connect()
    setup_corps(con, [("01933981", "새코드회사", "0068Y0", None, "K")])
    assert [r["corp_code"] for r in dart_query.search_corps(con, "0068y0")["results"]] == ["01933981"]
    by_year = {2025: [acc("BS", "a", "자산총계", " 00100 ")]}

    async def go():
        async with client_with(fs_handler(by_year)) as c:
            return await dart_query.fetch_fs(c, con, fs_spec(start="2025", end="2025"))
    assert grid(run(go()))["[재무상태표] 자산총계"] == {"2025": " 00100 "}      # 원천 문자열 그대로


def test_no_transitive_linking_across_years():
    """2023↔2025 이름 같음, 2024↔2025 ID 같음, 2023↔2024는 둘 다 다름 → 2023은 따로(codex 8차 높음, 셀트리온 사례)."""
    by_year = {2025: [acc("CF", "dart_P", "관계기업 투자의 증가", "16")],
               2024: [acc("CF", "dart_P", "종속기업 및 관계기업투자의 증가", "17")],
               2023: [acc("CF", "ifrs-full_P", "관계기업투자의 증가", "22")]}
    con = cat.connect()

    async def go(a):
        async with client_with(fs_handler(by_year)) as c:
            return await dart_query.fetch_fs(c, con, fs_spec(start=a, end="2025", sel={"SJ": ["CF"]}))
    res = run(go("2023"))
    g = grid(res)
    # 이름이 같은 2023·2025가 한 줄(codex 9차: ID를 먼저 붙여 이 연결이 끊기면 안 됨), 2023과 맞지 않는 2024는 따로
    assert sorted(map(sorted, g.values())) == [["2023", "2025"], ["2024"]]
    assert g["[현금흐름표] 관계기업 투자의 증가"] == {"2023": "22", "2025": "16"}
    assert run(go("2024")) and sorted(map(sorted, grid(run(go("2024"))).values())) == [["2024", "2025"]]   # 2023이 없으면 ID로 이음


def test_id_merge_needs_same_unique_id_everywhere():
    """이름이 다른 묶음은 양쪽 모든 행이 같은 유일 ID일 때만 합친다: 매출액·영업수익·수익(매출액) 세 이름 → 한 줄."""
    by_year = {2025: [acc("IS", "ifrs-full_Revenue", "매출액", "5")], 2024: [acc("IS", "ifrs-full_Revenue", "매출액", "4")],
               2023: [acc("IS", "ifrs-full_Revenue", "영업수익", "3")], 2022: [acc("IS", "ifrs-full_Revenue", "수익(매출액)", "2")],
               2021: [acc("IS", "other", "기타영업수익", "9")]}
    con = cat.connect()

    async def go():
        async with client_with(fs_handler(by_year)) as c:
            return await dart_query.fetch_fs(c, con, fs_spec(start="2021", end="2025"))
    res = run(go())
    g = grid(res)
    assert g["[손익계산서] 매출액"] == {"2022": "2", "2023": "3", "2024": "4", "2025": "5"}
    assert g["[손익계산서] 기타영업수익"] == {"2021": "9"}
    name = next(s["names"]["계정"] for s in res["series"] if s["label"] == "[손익계산서] 매출액")
    assert name == "매출액 (예전 이름: 영업수익, 수익(매출액))"


def test_list_keeps_raw_report_name():
    def h(req):
        if "company.json" in str(req.url):
            return httpx.Response(200, json=COMPANY)
        return httpx.Response(200, json={"status": "000", "total_count": 1, "total_page": 1, "list": [
            {"corp_cls": "Y", "report_nm": "주요사항보고서 ", "rcept_no": "1", "flr_nm": "x", "rcept_dt": "20261112", "rm": ""}]})
    con = cat.connect()

    async def go():
        async with client_with(h) as c:
            return await dart_query.fetch_list(c, con, {"id": "00126380", "start": "2026-11-01", "end": "2026-11-13"})
    assert run(go())["table"]["rows"][0][1] == "주요사항보고서 "
