"""공정위 기업집단포털 테스트(모의 XML).

    python3 -m pytest -q tests/test_egroup.py
"""
from __future__ import annotations

import asyncio
import io
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from openpyxl import load_workbook

from datahub import catalog as cat
from datahub import config, egroup_query, exporter, query
from datahub.errors import SourceError
from datahub.sources import egroup

FAKE = "PUBLICFAKEKEY0123456789abcdef0123456789abcdef0123456789abcdef01"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cat, "CATALOG_DB", tmp_path / "c.sqlite")
    monkeypatch.setattr(config, "_find_api_key", lambda s: FAKE if s == "PUBLIC" else None)
    monkeypatch.setattr(egroup, "PAGE", 2)          # 쪽 나눔을 시험하려고 작게
    config._KEY_CACHE.clear()
    query._results.clear()
    yield
    config._KEY_CACHE.clear()


def run(coro):
    return asyncio.run(coro)


def xml(root, item, rows, total=None, code="00"):
    body = "".join(f"<{item}>" + "".join(f"<{k}>{v}</{k}>" for k, v in r.items()) + f"</{item}>" for r in rows)
    return (f'<?xml version="1.0" encoding="UTF-8"?><{root}><numOfRows>2</numOfRows><pageNo>1</pageNo>'
            f"<resultCode>{code}</resultCode><resultMsg>SUCCESS</resultMsg><totalCount>{total if total is not None else len(rows)}</totalCount>"
            f"{body}</{root}>").encode()


GROUPS = {"202605": [{"unityGrupNm": "삼성", "unityGrupCode": "K1", "smerNm": "이재용", "repreCmpny": "삼성전자(주)", "sumCmpnyCo": "3",
                      "invstmntLmtt": "상호출자제한집단", "entrprsCl": "일반"},
                     {"unityGrupNm": "가나", "unityGrupCode": "K2", "smerNm": "홍길동", "repreCmpny": "가나(주)", "sumCmpnyCo": "1",
                      "invstmntLmtt": "공시대상기업집단", "entrprsCl": "일반"}],
          "202505": [{"unityGrupNm": "삼성", "unityGrupCode": "K1", "smerNm": "이재용", "repreCmpny": "삼성전자(주)", "sumCmpnyCo": "2",
                      "invstmntLmtt": "상호출자제한집단", "entrprsCl": "일반"}]}
AFFS = {"202605": [{"unityGrupNm": "삼성", "entrprsNm": "삼성전자(주)", "jurirno": "1301110006246", "bizrno": "1248100998", "rprsntvNm": "전영현",
                    "fondDe": "19690113", "grinil": "19690113"},
                   {"unityGrupNm": "삼성", "entrprsNm": "(주)동명회사", "jurirno": "1111111111111", "bizrno": "1", "rprsntvNm": "a", "fondDe": "", "grinil": "20200101"},
                   {"unityGrupNm": "삼성", "entrprsNm": "새회사(주)", "jurirno": "2222222222222", "bizrno": "2", "rprsntvNm": "b", "fondDe": "", "grinil": "20260301"},
                   {"unityGrupNm": "가나", "entrprsNm": "가나(주)", "jurirno": "3333333333333", "bizrno": "3", "rprsntvNm": "c", "fondDe": "", "grinil": "20000101"}],
        "202505": [{"unityGrupNm": "삼성", "entrprsNm": "삼성전자(주)", "jurirno": "1301110006246", "bizrno": "1248100998", "rprsntvNm": "한종희",
                    "fondDe": "19690113", "grinil": "19690113"},
                   {"unityGrupNm": "삼성", "entrprsNm": "옛회사(주)", "jurirno": "4444444444444", "bizrno": "4", "rprsntvNm": "d", "fondDe": "", "grinil": "20100101"}]}
ASSETS = {"202605": [{"unityGrupNm": "삼성", "unityGrupCode": "K1", "rn": "1", "assetsTotamt": "695,785"},
                     {"unityGrupNm": "가나", "unityGrupCode": "K2", "rn": "2", "assetsTotamt": "5,000"}],
          "202505": [{"unityGrupNm": "삼성", "unityGrupCode": "K1", "rn": "1", "assetsTotamt": "600,000"}]}
CHANGES = [{"unityGrupNm": "삼성", "entrprsNm": "새회사(주)", "jurirno": "2222222222222", "psitnCmpnyChangeSeCode": "0001", "incrprDe": "20260301",
            "incrprPrvonshCode": "0002", "bizrno": "2"},
           {"unityGrupNm": "삼성", "entrprsNm": "옛회사(주)", "jurirno": "4444444444444", "psitnCmpnyChangeSeCode": "0002", "exclDe": "20260115",
            "exclPrvonshCode": "0002", "exclTrgetJobCode": "0009", "bizrno": "4"},
           {"unityGrupNm": "가나", "entrprsNm": "x", "jurirno": "5", "psitnCmpnyChangeSeCode": "0003", "postpneBeginDe": "20250101",
            "postpnePrvonshTyCode": "0006", "postpneEndPrearngeDe": "20300101"}]


def portal(calls=None, gateway=None):
    def page(rows, req):
        p = {k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()}
        n, size = int(p["pageNo"]), int(p["numOfRows"])
        return rows[(n - 1) * size: n * size], len(rows)

    def h(req):
        u = str(req.url)
        if calls is not None:
            calls.append(u)
        if gateway:
            return httpx.Response(200, content=gateway.encode())
        p = {k: v[0] for k, v in parse_qs(urlparse(u).query).items()}
        assert p.get("ServiceKey", p.get("serviceKey")) == FAKE
        ym = p.get("presentnYear")
        if "publicYmList" in u:
            rows = [{"othbcYm": "202605", "jobSeCode": "0001"}, {"othbcYm": "202505", "jobSeCode": "0001"}]
            r, t = page(rows, req)
            return httpx.Response(200, content=xml("publicYmList", "publicYm", r, t))
        for svc, item, data in (("appnGroupSttusList", "appnGroupSttus", GROUPS), ("appnGroupAssetsList", "appnGroupAssets", ASSETS),
                                ("appnGroupAffiList", "appnGroupAffi", AFFS)):
            if svc in u:
                r, t = page(data[ym], req)
                return httpx.Response(200, content=xml(svc, item, r, t))
        if "tyAssetsRentDelngDtlsList" in u:
            r, t = page(CHANGES, req)
            return httpx.Response(200, content=xml("tyAssetsRentDelngDtlsList", "tyAssetsRentDelngDtls", r, t))
        return httpx.Response(404)
    return h


def built():
    con = cat.connect()
    con.executemany("INSERT INTO dart_corps VALUES (?,?,?,?,?,?,?,?)", [
        ("00126380", "삼성전자", "", "005930", "", "삼성전자", "264", "Y"),
        ("00000001", "동명회사", "", "", "", "동명회사", None, None), ("00000002", "동명회사", "", "", "", "동명회사", None, None)])
    con.commit()
    calls = []

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(portal(calls))) as c:
            return await egroup_query.refresh(c, con)
    return con, run(go()), calls


def test_refresh_paging_and_mapping():
    con, res, calls = built()
    assert res["latest"] == "202605" and res["groups"] == 2 and res["affiliates"] == 4 and res["changes"] == 3
    assert res["count_mismatch"] == [] and res["failed_ym"] == []
    assert any("pageNo=2" in u for u in calls)                                # 쪽을 넘겨 끝까지 받음
    assert all(FAKE not in u for u in egroup_query.fetch(con, {"kind": "groups"})["requests"])
    codes = {r[0] for r in con.execute("SELECT group_code FROM egroup_affiliates WHERE ym='202605'")}
    assert codes == {"K1", "K2"}                                              # 집단 이름 → 코드
    g = con.execute("SELECT * FROM egroup_groups WHERE ym='202605' AND code='K1'").fetchone()
    assert g["asset_rank"] == 1 and g["asset_total"] == "695,785" and g["n_comp"] == 3


def test_company_lookup_and_history():
    con, _, _ = built()
    eg = egroup_query.for_company(con, "130111-0006246")                     # 하이픈 있어도
    assert eg["group_name"] == "삼성" and eg["current"] and eg["ym"] == "202605" and eg["joined"] == "19690113"
    assert eg["history"] == [{"group_name": "삼성", "group_code": "K1", "from": "202505", "to": "202605", "from_label": "2025-05", "to_label": "2026-05"}]
    old = egroup_query.for_company(con, "4444444444444")
    assert old["current"] is False and old["ym"] == "202505"                  # 지금은 지정 소속 아님
    assert egroup_query.for_company(con, "9999999999999") is None
    assert egroup_query.for_company(con, "") is None


def test_group_detail_search_and_downloads():
    con, _, _ = built()
    d = egroup_query.group(con, "K1")
    names = {a["company"]: a["dart"] for a in d["affiliates"]}
    assert names["삼성전자(주)"]["corp_code"] == "00126380" and names["삼성전자(주)"]["listed"]
    assert names["(주)동명회사"] is None                                         # 동명 회사는 잇지 않음
    con.execute("INSERT INTO dart_jurir VALUES ('00000002', '1111111111111')")    # 법인등록번호를 알면 동명이어도 정확히 이음
    con.commit()
    linked = {a["company"]: a["dart"] for a in egroup_query.group(con, "K1")["affiliates"]}
    assert linked["(주)동명회사"]["corp_code"] == "00000002" and linked["(주)동명회사"]["how"] == "법인등록번호"
    assert linked["삼성전자(주)"]["how"] == "이름"
    assert [c["change"] for c in d["changes"]] == ["편입", "제외"] and d["changes"][0]["reason"] == "지분취득"
    assert d["changes"][1]["reason"] == "지분매각"
    assert [y["ym"] for y in egroup_query.group(con, "K1", "202505")["yms"]] == ["202605", "202505"]
    assert egroup_query.group(con, "K1", "202505")["group"]["n_comp"] == 2
    assert [g["code"] for g in egroup_query.search_groups(con, "삼성")] == ["K1"]
    assert [g["code"] for g in egroup_query.search_groups(con, "가나(주)")] == ["K2"]   # 대표회사 이름으로도
    with pytest.raises(SourceError) as e:
        egroup_query.group(con, "K999")
    assert e.value.kind == "not_found"

    async def fetch(spec):
        return await query.fetch(None, con, None, spec)
    aff = run(fetch({"source": "EGROUP", "kind": "affiliates", "id": "K1", "ym": "202605"}))
    by_name = {r[3]: r for r in aff["table"]["rows"]}
    assert aff["row_count"] == 3 and by_name["삼성전자(주)"][9] == "00126380" and by_name["삼성전자(주)"][10] == "005930"
    grp = run(fetch({"source": "EGROUP", "kind": "groups", "ym": "202605"}))
    wb = load_workbook(io.BytesIO(exporter.xlsx_bytes(grp)))
    assert wb["목록"].cell(2, 9).value == "695,785" and wb["목록"].cell(2, 6).value == 3     # 자산총액은 원천 표기 그대로
    ch = run(fetch({"source": "EGROUP", "kind": "changes", "id": "K2"}))
    assert ch["table"]["rows"][0][4] == "유예" and ch["table"]["rows"][0][6] == "벤처기업"


def test_gateway_errors():
    for body, kind in (('<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>'
                        '<returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>', "auth"),
                       ('<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</errMsg>'
                        '<returnReasonCode>22</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>', "rate_limit"),
                       ('<appnGroupSttusList><resultCode>97</resultCode><resultMsg>presentnYear ::: null</resultMsg></appnGroupSttusList>', "bad_request"),
                       ("not xml", "server")):
        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(portal(gateway=body))) as c:
                return await egroup.call(c, "appnGroupSttusList", presentnYear="202605")
        with pytest.raises(SourceError) as e:
            run(go())
        assert e.value.kind == kind and FAKE not in e.value.message
