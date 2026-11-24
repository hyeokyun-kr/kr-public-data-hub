"""공공데이터포털 목록·파일 받기 테스트(네트워크 없이 모의 응답).

    python3 -m pytest -q tests/test_datago.py
"""
from __future__ import annotations

import asyncio
import csv
import io

import httpx
import pytest

from datahub import catalog as cat
from datahub import config, datago_query, query
from datahub.errors import SourceError
from datahub.sources import datago

HEAD = ["목록키", "목록유형", "목록명", "파일데이터명", "분류체계", "제공기관코드", "제공기관", "관리 부서명", "관리부서 전화번호",
        "보유근거", "수집방법", "업데이트 주기", "차기 등록 예정일", "매체유형", "전체행", "확장자(데이터포맷)", "키워드",
        "다운로드_활용신청건수", "등록일", "수정일", "데이터 한계", "제공형태", "설명", "기타 유의사항", "공간범위", "시간범위",
        "비용부과유무", "비용부과기준 및 단위", "이용허락범위", "API 유형", "신청가능 트래픽", "심의 유형", "조회수", "목록 URL",
        "국가중점여부", "표준데이터여부"]


def row(key, kind, name, org, form, desc="설명", kw="", fmt="csv", mod="2026-10-01"):
    d = {h: "" for h in HEAD}
    d.update({"목록키": key, "목록유형": kind, "목록명": name, "제공기관": org, "제공기관코드": "1", "분류체계": "산업 - 유통",
              "제공형태": form, "설명": desc, "키워드": kw, "확장자(데이터포맷)": fmt, "수정일": mod, "업데이트 주기": "연간",
              "관리부서 전화번호": "010-0000-0000", "목록 URL": f"https://www.data.go.kr/data/{key}/fileData.do"})
    return d


def catalog_csv(rows) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HEAD, lineterminator="\r\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


ROWS = [row("15000001", "FILE", "공정거래위원회_가맹사업 정보공개서", "공정거래위원회", datago.PORTAL_FILE,
            desc="가맹본부의 정보공개서\n여러 줄 설명", kw="가맹,프랜차이즈"),
        row("15000002", "API", "공정거래위원회_가맹사업 정보 조회 서비스", "공정거래위원회", "", fmt="JSON+XML"),
        row("15000003", "FILE", "소비자물가지수 파일", "어느시청", "기관자체에서 다운로드(제공데이터URL기재)")]
ROWS += [row(str(16000000 + i), "FILE", f"기타 자료 {i}", "기관", datago.PORTAL_FILE) for i in range(1000)]

PAGE = "<a onclick=\"fileDetailObj.fn_fileDataDown('{key}', 'uddi:abc-{key}', '','1', '1')\">다운로드</a>"


def portal(files: dict, captcha: bool = False, html_instead: bool = False, calls: list | None = None,
           limit_html: bool = False, error_page: bool = False, final_status: int = 200):
    """files: 목록키 → (내용 bytes, 원래 파일 이름)."""
    def h(req):
        u = str(req.url)
        if calls is not None:
            calls.append((req.method, u))
        if "/fileData.do" in u:
            key = u.split("/data/")[1].split("/")[0]
            if error_page:
                return httpx.Response(200, text="<html>잠시 후 다시 이용해 주세요</html>")
            if key not in files:
                return httpx.Response(200, text=f"<html>목록 {key} 원본은 기관 사이트에</html>")
            return httpx.Response(200, text=PAGE.format(key=key))
        if "selectFileDataDownload" in u:
            form = dict(x.split("=") for x in req.content.decode().split("&"))
            key = form["publicDataPk"]
            return httpx.Response(200, json={"status": True, "atchFileId": f"FILE_{key}", "fileDetailSn": "1",
                                             "dataSetFileDetailInfo": {"dataNm": f"자료{key}", "orginlFileNm": files[key][1],
                                                                       "atchFileExtsn": "csv", "atchFileCo": "3"}})
        if "check-limit" in u:
            if limit_html:
                return httpx.Response(200, text="<html>오류</html>", headers={"content-type": "text/html"})
            return httpx.Response(200, json={"needCaptcha": captcha})
        if "fileDownload.do" in u:
            if final_status != 200:
                return httpx.Response(final_status, text="no")
            if html_instead:
                return httpx.Response(200, text="<html>오류</html>", headers={"content-type": "text/html"})
            key = u.split("atchFileId=FILE_")[1].split("&")[0]
            return httpx.Response(200, content=files[key][0], headers={"content-type": "application/octet-stream",
                                                                      "content-disposition": 'attachment; filename="x.csv"'})
        return httpx.Response(404)
    return h


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cat, "CATALOG_DB", tmp_path / "c.sqlite")
    from datahub import exporter
    monkeypatch.setattr(exporter, "DOWNLOAD_LOG", tmp_path / "download_log.csv")      # 실제 기록 파일에 쓰지 않게
    monkeypatch.setattr(config, "_find_api_key", lambda s: None)   # 시험 서버가 실제 기관 API(DART 회사 목록 등)를 부르지 않게
    config._KEY_CACHE.clear()
    yield
    config._KEY_CACHE.clear()


def run(coro):
    return asyncio.run(coro)


def client_with(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def refreshed():
    con = cat.connect()
    cat_file = {datago.CATALOG_KEY: (catalog_csv(ROWS), "목록개방현황_20261031.csv")}

    async def go():
        async with client_with(portal(cat_file)) as c:
            return await datago_query.refresh(c, con)
    return con, run(go())


def test_refresh_and_detail():
    con, res = refreshed()
    assert res["rows"] == len(ROWS)
    r = con.execute("SELECT * FROM datasets WHERE source='DATAGO' AND ds_id='15000001'").fetchone()
    assert r["name"] == "공정거래위원회_가맹사업 정보공개서" and r["org_name"] == "공정거래위원회" and r["end_year"] == 2026
    d = query.detail  # 상세는 원천을 부르지 않는다
    det = run(d(None, con, None, "DATAGO", "15000001"))
    assert det["portal_file"] is True and det["info"]["설명"] == "가맹본부의 정보공개서\n여러 줄 설명"
    assert all(k != "관리부서 전화번호" for k, _ in det["fields"])                     # 전화번호는 저장하지 않음
    assert datago_query.meta_path().exists() and datago_query.meta_path().parent == cat.CATALOG_DB.parent
    api = run(d(None, con, None, "DATAGO", "15000002"))
    assert api["portal_file"] is False and "활용신청" in api["notes"][0]
    ext = run(d(None, con, None, "DATAGO", "15000003"))
    assert ext["portal_file"] is False and "기관자체" in ext["notes"][0]
    with pytest.raises(SourceError) as e:
        run(d(None, con, None, "DATAGO", "99999999"))
    assert e.value.kind == "not_found"


def test_search_with_datago():
    con, _ = refreshed()
    cat.upsert_dataset(con, {"source": "KOSIS", "ds_id": "101/X", "org_id": "101", "org_name": "국가데이터처",
                             "name": "소비자물가지수 파일", "path": "물가", "ranges": {"M": ["2020-01", "2026-10"]}, "end_year": 2026})
    con.commit()
    c = cat.Catalog()
    hits = c.search([("가맹사업", "", 1.0)])
    assert [h.entry.ds_id for h in hits][:2] == ["15000001", "15000002"]
    assert all(h.entry.source == "DATAGO" for h in c.search([("가맹", "", 1.0)], source="DATAGO"))
    assert not c.search([("가맹사업", "", 1.0)], cycle="M")                       # 주기 필터면 공공데이터포털은 뺌
    same = c.search([("소비자물가지수 파일", "", 1.0)])
    assert [h.entry.source for h in same][:2] == ["KOSIS", "DATAGO"]              # 같은 이름이면 국가통계 먼저
    hd = cat.hit_to_dict(hits[0])
    assert hd["extra"]["kind"] == "FILE" and hd["extra"]["portal_file"] is True and hd["discontinued"] is False
    assert "description" not in hits[0].entry.row or hits[0].entry.row.get("description") is None   # 메모리 절약


def test_file_flow_errors():
    files = {"15000001": (b"a,b\r\n1,2\r\n", "")}

    async def go(h, key="15000001"):
        async with client_with(h) as c:
            info = await datago.file_info(c, key)
            await datago.check_limit(c, info)
            return info
    info = run(go(portal(files)))
    assert info["atchFileId"] == "FILE_15000001" and "fileDownload.do" in datago.download_url(info)
    with pytest.raises(SourceError) as e:
        run(go(portal(files, captcha=True)))
    assert e.value.kind == "rate_limit" and e.value.code == "captcha"              # 캡차는 우회하지 않고 멈춤
    with pytest.raises(SourceError) as e:
        run(go(portal(files), key="15000003"))
    assert e.value.kind == "not_found"
    with pytest.raises(SourceError) as e:
        run(go(portal(files), key="abc"))
    assert e.value.kind == "bad_request"
    with pytest.raises(SourceError) as e:                       # 한도 확인 응답을 못 읽으면 받지 않는다(codex 11차 높음)
        run(go(portal(files, limit_html=True)))
    assert e.value.kind == "server"
    with pytest.raises(SourceError) as e:                       # 목록 화면 대신 오류 화면
        run(go(portal(files, error_page=True)))
    assert e.value.kind == "server"


def test_server_file_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    from datahub import server
    con, _ = refreshed()
    con.close()
    body = "순번,상호\r\n1,가나다\r\n".encode("cp949")
    calls = []
    with TestClient(server.app) as tc:
        server.state["client"] = client_with(portal({"15000001": (body, "")}, calls=calls))
        r = tc.get("/api/datago/file?id=15000001")
        assert r.status_code == 200 and r.content == body                          # 원본 그대로
        assert "filename*=UTF-8''" in r.headers["content-disposition"] and r.headers["content-disposition"].endswith(".csv")
        assert [m for m, u in calls] == ["GET", "POST", "POST", "GET"]
        server.state["client"] = client_with(portal({"15000001": (body, "")}, html_instead=True))
        r = tc.get("/api/datago/file?id=15000001")
        assert r.status_code >= 400 and r.json()["error"]["source"] == "DATAGO"   # 오류 화면을 파일로 넘기지 않음
        server.state["client"] = client_with(portal({"15000001": (body, "")}, captcha=True))
        r = tc.get("/api/datago/file?id=15000001")
        assert r.status_code == 429 and "캡차" in r.json()["error"]["message"]
        calls2 = []
        server.state["client"] = client_with(portal({"15000001": (body, "")}, limit_html=True, calls=calls2))
        r = tc.get("/api/datago/file?id=15000001")
        assert r.status_code == 502 and not any("fileDownload" in u for _, u in calls2)   # 파일 요청 자체를 안 함
        for st_, kind in ((403, "rate_limit"), (404, "not_found")):
            server.state["client"] = client_with(portal({"15000001": (body, "")}, final_status=st_))
            assert tc.get("/api/datago/file?id=15000001").json()["error"]["kind"] == kind
        d = tc.get("/api/detail?source=DATAGO&id=15000002").json()
        assert d["kind_label"] == "오픈API" and d["cycles"] == []



def test_keyword_and_org_scoring_paging_and_raw_strings():
    rows = [row("17000001", "FILE", "가 자료", "지식재산처", datago.PORTAL_FILE, kw="특허청,특허", desc="설명"),
            row("17000002", "FILE", "나 자료", "기관", datago.PORTAL_FILE, desc="특허청 이야기가 설명에만"),
            row("17000003", "FILE", " 앞뒤 공백 이름 ", " 관세청 ", datago.PORTAL_FILE, desc=" 설명 공백 ")]
    rows += [row(str(18000000 + i), "FILE", f"자료 {i}", "기관", datago.PORTAL_FILE) for i in range(1000)]
    con = cat.connect()

    async def go():
        async with client_with(portal({datago.CATALOG_KEY: (catalog_csv(rows), "x.csv")})) as c:
            return await datago_query.refresh(c, con)
    run(go())
    c = cat.Catalog()
    hits = c.search([("특허청", "", 1.0)])
    assert [h.entry.ds_id for h in hits][:2] == ["17000001", "17000002"]          # 키워드 일치가 설명 일치보다 위
    assert hits[0].reason == "키워드에 포함"
    org = c.search([("관세청", "", 1.0)])
    assert org and org[0].entry.ds_id == "17000003" and "기관" in org[0].reason   # 제공기관으로도 찾힘
    d = datago_query.detail(con, "17000003")
    assert d["name"] == " 앞뒤 공백 이름 " and d["org_name"] == " 관세청 " and d["info"]["설명"] == " 설명 공백 "   # 원천 그대로
    from fastapi.testclient import TestClient
    from datahub import server
    con.close()
    with TestClient(server.app) as tc:
        a = tc.get("/api/search?q=자료&source=DATAGO&live=0&syn=0").json()
        b = tc.get("/api/search?q=자료&source=DATAGO&live=0&syn=0&offset=60").json()
        assert a["count"] > 120 and a["shown"] == 60 and b["offset"] == 60 and b["shown"] == 60
        assert not {h["id"] for h in a["results"]} & {h["id"] for h in b["results"]}   # 다음 60개는 겹치지 않음
