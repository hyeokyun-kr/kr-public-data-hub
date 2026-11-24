"""공정위 의결서 검색('의결서' 탭) 테스트. 작은 색인을 실제와 같은 구조로 만들어 쓴다(네트워크 없음).

    python3 -m pytest -q tests/test_decisions.py
"""
from __future__ import annotations

import asyncio
import sqlite3
import zlib

import httpx
import pytest

from datahub import catalog as cat
from datahub import config, decisions as dc
from datahub.errors import SourceError

# 의결서 색인 구조(표 설명은 datahub/decisions.py 머리말)
SCHEMA = """
CREATE TABLE info (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE decisions (
  id INTEGER PRIMARY KEY, 의결번호 TEXT, 의결일 TEXT, 연도 INT, 사건번호 TEXT, 사건명 TEXT,
  대표위반유형 TEXT, 대표조치유형 TEXT, fileId TEXT, fileSn TEXT, 경로 TEXT,
  쪽수 INT, 글자수 INT, 상태 TEXT);
CREATE TABLE page (rowid INTEGER PRIMARY KEY, doc INT, page INT, z BLOB);
CREATE INDEX page_doc ON page(doc, page);
CREATE INDEX dec_date ON decisions(의결일);
CREATE VIRTUAL TABLE pg USING fts5(body, content='', tokenize='unicode61');
"""

DOCS = [
    # id, 의결번호, 의결일, 사건번호, 사건명, 유형, 조치, 상태, {쪽: 본문}
    (1, "의결2020-101", "2020-05-01", "2019카경1234", "가나다(주)의 부당한 공동행위에 대한 건", "부당한 공동행위", "과징금 / 시정명령", "정상",
     {1: "피심인 가나다는 입찰에서 부당한 공동행위를 하였다.", 2: "과징금 산정\n자진신고에 따른 감경을 한다.", 4: "끝 쪽 <b>표</b> & 기호"}),
    (2, "의결2021-202", "2021-07-15", "2020서경0001, 2020서경0002", "라마바 프랜차이즈의 가맹사업법 위반행위에 대한 건", "가맹사업거래의공정화에관한법령 위반관련",
     "시정명령", "정상", {1: "가맹본부 라마바는 정보공개서를 늦게 주었다.", 2: "공동행위 이야기는 없다. 감경 사유도 없다."}),
    (3, "의결2004-313", "2004-11-02", "2003경촉0999", "사아자의부당한공동행위에대한건", "부당한 공동행위", "과징금", "정상",
     {1: "피심인들의 부당한공동행위는 붙여 썼다.", 2: "감경 없음. 공동 행위라고 띄어 쓴 곳."}),
    (4, "의결2022-001", "2022-01-03", "2021기업0001", "차카타 기업결합 건", "기업결합", "시정명령", "사이트 원문 생략 안내(유사 사건 참조)",
     {1: "기업결합 심사 결과 경쟁제한성이 없다. 공동행위와 무관하다."}),
]


def build(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    rowid = 0
    for i, no, date, case, name, ty, act, stt, pages in DOCS:
        chars = sum(len(b) for b in pages.values())
        con.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (i, no, date, int(date[:4]), case, name, ty, act, f"F{i}", "1", f"{date[:4]}/{date}_{no}.pdf",
                     len(pages), chars, stt))
        for p, body in pages.items():
            rowid += 1
            con.execute("INSERT INTO pg(rowid, body) VALUES (?,?)", (rowid, body))
            con.execute("INSERT INTO page VALUES (?,?,?,?)", (rowid, i, p, zlib.compress(body.encode("utf-8"))))
    con.executemany("INSERT INTO info VALUES (?,?)", [("만든때", "2026-11-17 09:45"), ("출처", "시험")])
    con.commit()
    con.close()


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cat, "CATALOG_DB", tmp_path / "c.sqlite")
    monkeypatch.setattr(config, "_find_api_key", lambda s: None)
    config._KEY_CACHE.clear()
    db = tmp_path / "decisions.sqlite"
    build(db)
    pdfs = tmp_path / "pdf"
    pdfs.mkdir()
    monkeypatch.setenv("DATAHUB_DECISION_DB", str(db))
    monkeypatch.setenv("DATAHUB_DECISION_PDF_DIR", str(pdfs))
    monkeypatch.setattr(dc, "_index", None)
    monkeypatch.setattr(dc, "_ftc_client", None)
    yield {"db": db, "pdfs": pdfs}
    config._KEY_CACHE.clear()


def ids(res):
    return [r["id"] for r in res["results"]]


def joined(segs):
    return "".join(s for s, _ in segs)


def test_parse_terms_phrases_minus_and_noise():
    pos, neg, ign = dc.parse('공동행위  "부당한 공동행위" -입찰 가 a-b "열림')
    assert [t.text for t in pos] == ["공동행위", "부당한 공동행위", "a-b", "열림"]
    assert [t.fts for t in pos] == ['"공동행위"*', '"부당한 공동행위"*', '"a b"*', '"열림"*']
    assert [t.text for t in neg] == ["입찰"] and ign == ["가"]          # 한 글자는 빼고 알려 줌
    pos, neg, ign = dc.parse('OR * NEAR( AND -')                            # FTS 문법 글자는 모두 낱말로만
    assert [t.fts for t in pos] == ['"or"*', '"near"*', '"and"*'] and not neg
    pos, _, _ = dc.parse('" OR * NEAR( AND -')                             # 닫히지 않은 따옴표는 끝까지 한 구절
    assert [t.fts for t in pos] == ['"or near and"*']
    assert dc.need().search('" OR * NEAR( AND -')["count"] == 0          # 오류 없이 0건
    assert dc.parse("") == ([], [], [])


def test_search_and_across_pages_prefix_and_phrase():
    idx = dc.need()
    assert idx.status()["count"] == 4 and idx.status()["pages"] == 8 and idx.status()["problems"] == 1
    r = idx.search("공동행위 감경")                    # 문서 안 다른 쪽에 있어도 '모두 든' 의결서
    assert ids(r) == [2, 1, 3]                          # 최신순. 3번은 '공동행위'가 사건명(붙여 씀)에, '감경'은 본문에
    one = {x["id"]: x for x in r["results"]}[1]
    assert one["hit_pages"] == 2 and one["together_pages"] == 0
    assert [s["page"] for s in one["snippets"]] == [1, 2]
    assert any(h for s in one["snippets"] for _, h in s["segs"])
    ph = idx.search('"부당한 공동행위"')
    assert ids(ph) == [1, 3]                                  # 본문은 1번만 붙어 있는 구절. 3번은 사건명(띄어쓰기 무시)으로만
    assert [x["hit_pages"] for x in ph["results"]] == [1, 0] and idx.doc(3, '"부당한 공동행위"')["hits"] == []
    assert ids(idx.search("공동")) == [4, 2, 1, 3]      # 앞부분 일치: 공동행위를·공동행위와·'공동 행위'
    assert ids(idx.search("공동행위 -입찰")) == [4, 2, 3]   # 빼기(1번은 본문에 '입찰')
    assert idx.search("공동행위")["count"] == 4          # 3번은 본문엔 붙여 써서 없지만 사건명(띄어쓰기 무시)에 있음
    t3 = {x["id"]: x for x in idx.search("공동행위")["results"]}[3]
    assert t3["title_hit"] and t3["hit_pages"] == 0 and t3["snippets"] == []


def test_metadata_numbers_filters_facets_sort_paging():
    idx = dc.need()
    assert ids(idx.search("2020서경0002")) == [2]          # 사건번호(여러 개 중 하나)
    assert ids(idx.search("2004-313")) == [3]               # 의결번호 일부
    r = idx.search("공동행위", types=["부당한 공동행위"])
    assert ids(r) == [1, 3]
    assert dict(r["facets"]["types"])["기업결합"] == 1      # 유형별 건수는 유형 필터를 빼고 셈(바꿔 볼 수 있게)
    assert ids(idx.search("공동행위", acts=["과징금"])) == [1, 3]
    assert ids(idx.search("공동행위", y1=2021, y2=2022)) == [4, 2]
    assert ids(idx.search("", types=["기업결합"])) == [4]   # 검색어 없이 조건만
    assert idx.search("", types=["기업결합"])["results"][0].get("snippets") is None
    with pytest.raises(SourceError) as e:
        idx.search("")
    assert e.value.kind == "bad_request"
    with pytest.raises(SourceError):
        idx.search("가 -입찰")                                # 찾을 말이 없음
    h = idx.search("감경 공동행위", sort="hits")
    assert ids(h)[0] == 2                                     # 두 말이 한 쪽에 같이 나온 의결서가 먼저
    a = idx.search("공동", limit=2)
    b = idx.search("공동", limit=2, offset=2)
    assert ids(a) + ids(b) == ids(idx.search("공동")) and a["count"] == 4
    assert "snippets" not in idx.search("공동", snippets=False)["results"][0]
    s = idx.snippets("공동 감경", [1, 2, 99])
    assert set(s["snippets"]) == {"1", "2"} and s["snippets"]["2"][0]["page"] == 2


def test_doc_page_and_highlight_text_is_untouched():
    idx = dc.need()
    d = idx.doc(1, "공동행위 감경")
    assert d["page_list"] == [1, 2, 4] and [h["page"] for h in d["hits"]] == [1, 2]
    assert [h["terms"] for h in d["hits"]] == [[0], [1]] and not any(h["together"] for h in d["hits"])
    assert d["site_url"].startswith(dc.FTC_LIST) and "caseNo=2019" in d["site_url"]
    assert d["pdf_local"] is False
    p = idx.page(1, 2, "감경")
    assert joined(p["segs"]) == DOCS[0][8][2]                 # 강조 조각을 이으면 원문 그대로(줄바꿈 포함)
    assert [s for s, h in p["segs"] if h] == ["감경"]
    raw = idx.page(1, 4)
    assert joined(raw["segs"]) == "끝 쪽 <b>표</b> & 기호"      # HTML은 서버가 건드리지 않음(화면이 이스케이프)
    with pytest.raises(SourceError) as e:
        idx.page(1, 3)                                        # 빈 쪽
    assert e.value.kind == "not_found"
    with pytest.raises(SourceError):
        idx.doc(999)
    sn = dc.snippet("앞" * 200 + " 공동행위 " + "뒤" * 300, dc._regex(dc.parse("공동행위")[0]))
    assert sn["pre"] and sn["post"] and sn["found"] and [s for s, h in sn["segs"] if h] == ["공동행위"]


def test_missing_index_and_reload(env, monkeypatch):
    monkeypatch.setenv("DATAHUB_DECISION_DB", str(env["db"].with_name("없음.sqlite")))
    assert dc.load() is None and dc.status()["available"] is False
    with pytest.raises(SourceError) as e:
        dc.need()
    assert e.value.kind == "not_found" and "decisions.sqlite" in e.value.message
    monkeypatch.setenv("DATAHUB_DECISION_DB", str(env["db"]))
    assert dc.need().status()["available"]


def fake_ftc(calls, script):
    """script: POST 응답 차례(예: ['html', 'pdf']). GET(목록 화면)은 쿠키를 준다."""
    seq = list(script)

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.method == "GET":
            return httpx.Response(200, text="<html>목록</html>", headers={"Set-Cookie": "JSESSIONID=abc; Path=/"})
        kind = seq.pop(0) if seq else "html"
        if kind == "pdf":
            return httpx.Response(200, content=b"%PDF-1.4 fake", headers={"Content-Type": "application/pdf"})
        if kind == "307":
            return httpx.Response(307, headers={"Location": "/ocp/main.do"})
        return httpx.Response(200, text="<html>main</html>")
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=dc.FTC_HEADERS)


def test_server_endpoints_and_pdf(env, monkeypatch):
    from fastapi.testclient import TestClient
    from datahub import server
    (env["pdfs"] / "2020").mkdir()
    (env["pdfs"] / "2020" / "2020-05-01_의결2020-101.pdf").write_bytes(b"%PDF-local")
    with TestClient(server.app) as tc:
        # 켜자마자 온 검색도 (뒤에서 읽는 중이면 기다렸다가) 색인을 써서 답한다
        r = tc.get("/api/decision/search", params={"q": "공동행위", "type": "부당한 공동행위|기업결합", "snip": 0}).json()
        assert ids(r) == [4, 1, 3] and "snippets" not in r["results"][0]
        s = tc.get("/api/status").json()["decision"]
        assert s["available"] and s["count"] == 4 and s["types"][0][0] == "부당한 공동행위"
        sn = tc.get("/api/decision/snippets", params={"q": "공동행위", "ids": "4,1,x"}).json()["snippets"]
        assert set(sn) == {"4", "1"}
        bad = tc.get("/api/decision/search", params={"q": ""})
        assert bad.status_code == 400 and bad.json()["error"]["source"] == "FTC"
        d = tc.get("/api/decision/doc", params={"id": 1, "q": "감경"}).json()
        assert d["pdf_local"] is True and d["hits"][0]["page"] == 2
        assert tc.get("/api/decision/page", params={"id": 1, "p": 9}).status_code == 404
        local = tc.get("/api/decision/pdf", params={"id": 1})
        assert local.content == b"%PDF-local" and local.headers["content-type"] == "application/pdf"
        assert local.headers["content-disposition"].startswith("inline;")
        # 받아 둔 파일이 없으면 공정위에서 한 건: 세션이 끊겨 화면이 오면 새 세션으로 한 번 더
        calls = []
        monkeypatch.setattr(dc, "_ftc_client", fake_ftc(calls, ["html", "pdf"]))
        r = tc.get("/api/decision/pdf", params={"id": 2})
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        assert [m for m, _ in calls] == ["GET", "POST", "GET", "POST"]
        calls.clear()
        monkeypatch.setattr(dc, "_ftc_client", fake_ftc(calls, ["307", "html"]))
        r = tc.get("/api/decision/pdf", params={"id": 2})
        assert r.status_code == 502 and "text/html" in r.headers["content-type"] and "caseNo=2020" in r.text
        assert len(calls) == 4                                  # 두 번까지만(공정위 서버에 부담 주지 않게)


def test_fetch_pdf_network_error(monkeypatch):
    def boom(req):
        raise httpx.ConnectError("blocked")
    monkeypatch.setattr(dc, "_ftc_client", httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    with pytest.raises(SourceError) as e:
        asyncio.run(dc.fetch_pdf("F1", "1"))
    assert e.value.kind == "network"


# ------------------------------------------------------------------ AI 질문(가짜 AI 함수로, 실제 호출 없음)
def fake_llm(plan, answer, calls):
    async def llm(system, user, schema, effort, usage):
        calls.append({"system": system, "user": user, "schema": schema, "effort": effort})
        usage.input += 100
        usage.output += 10
        return plan if "검색어를 만든다" in system else answer(user)
    return llm


PLAN = {"understanding": "감경 적용", "types": ["부당한 공동행위"], "year_from": 0, "year_to": 0,
        "searches": [{"query": "자진신고 감경", "purpose": "핵심"}, {"query": "공동행위", "purpose": "넓게"}, {"query": "가", "purpose": "한 글자"}]}


def test_ai_ask_pages_prompt_and_refs():
    from datahub import decision_ai as da
    calls = []
    ans = {"answerable": True, "summary": "감경을 적용했습니다 [2]. 없는 근거 [9].",
           "points": [{"text": "입찰 담합 [1]", "sources": [1, 7]}, {"text": "[42]", "sources": []}], "gaps": ""}
    r = asyncio.run(da.ask("공동행위 감경은?", model="claude-haiku-5-5", llm=fake_llm(PLAN, lambda u: ans, calls)))
    assert [s["query"] for s in r["searches"]] == ["자진신고 감경", "공동행위"]          # 한 글자 검색어는 버림
    assert r["filters"]["types"] == ["부당한 공동행위"] and not r["filters"]["from_user"]
    assert [(s["id"], s["page"]) for s in r["sources"]] == [(1, 1), (1, 2)]           # 유형 필터 안에서, 같은 의결서는 쪽 순서
    assert r["sources"][1]["queries"] == ["자진신고 감경"] and any(h for _, h in r["sources"][1]["segs"])
    user = calls[1]["user"]
    assert "[1] 의결2020-101 (2020-05-01) 가나다(주)의 부당한 공동행위에 대한 건 — 1쪽" in user
    assert "<발췌>\n과징금 산정\n자진신고에 따른 감경을 한다.\n</발췌>" in user           # 쪽 본문 그대로
    assert calls[0]["effort"] == "low" and calls[1]["effort"] == "medium"
    assert "부당한 공동행위" in calls[0]["schema"]["properties"]["types"]["items"]["enum"]
    assert r["summary"] == "감경을 적용했습니다 [2]. 없는 근거."                       # 보내지 않은 번호는 지움
    assert r["points"] == [{"text": "입찰 담합 [1]", "sources": [1]}]                 # 번호만 남은 항목은 버림
    assert r["dropped_refs"] == 3 and [s["cited"] for s in r["sources"]] == [True, True]
    assert r["usage"]["input_tokens"] == 200
    assert r["model"] == "gpt-6.1-sol" and r["model_label"] == "GPT-6.1 Sol"       # Haiku는 의결서 질문 목록에 없음 → 기본(Sol)


def test_ai_ask_user_filters_widening_and_nothing_found():
    from datahub import decision_ai as da
    calls = []
    ans = {"answerable": True, "summary": "[1]", "points": [], "gaps": ""}
    r = asyncio.run(da.ask("공동행위", types=["기업결합"], llm=fake_llm(PLAN, lambda u: ans, calls)))
    assert r["filters"]["from_user"] and [s["id"] for s in r["sources"]] == [4]       # 화면 조건이 AI 짐작보다 우선
    narrow = {**PLAN, "types": ["기업결합"], "searches": [{"query": "자진신고 감경", "purpose": ""}]}
    r = asyncio.run(da.ask("감경", llm=fake_llm(narrow, lambda u: ans, [])))
    assert r["filters"]["widened"] and [s["id"] for s in r["sources"]] == [1]          # AI 짐작 조건으로 0건이면 조건 없이
    calls = []
    none = {**PLAN, "types": [], "searches": [{"query": "없는말입니다", "purpose": ""}]}
    r = asyncio.run(da.ask("없는 것", llm=fake_llm(none, lambda u: ans, calls)))
    assert r["sources"] == [] and not r["answerable"] and len(calls) == 1             # 근거가 없으면 답 단계는 부르지 않음(비용)
    with pytest.raises(SourceError):
        asyncio.run(da.ask("가", llm=fake_llm(PLAN, lambda u: ans, [])))
    assert da.clean_refs("a [1][3] b [0] [12]", 3) == ("a [1][3] b", {1, 3}, 2)
    assert da.clean_refs("그대로 [1]  둠", 3) == ("그대로 [1]  둠", {1}, 0)                 # 지운 것이 없으면 글을 건드리지 않음


def test_ai_ask_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    from datahub import decision_ai as da, server
    calls = []
    llm = fake_llm(PLAN, lambda u: {"answerable": False, "summary": "부족 [1]", "points": [], "gaps": "더 찾기"}, calls)

    models_seen = []

    async def fake_ask_model(model, system, user, schema, effort, usage):
        models_seen.append({"model": model})
        return await llm(system, user, schema, effort, usage)
    monkeypatch.setattr(da, "ask_model", fake_ask_model)
    with TestClient(server.app) as tc:
        r = tc.post("/api/decision/ask", json={"question": "공동행위 감경", "model": "gpt-6.1-terra", "type": "", "y1": "2021"}).json()
        assert r["model"] == "gpt-6.1-terra" and r["filters"]["y1"] == 2021 and r["filters"]["from_user"]
        assert [s["id"] for s in r["sources"]] == [4, 2]                                  # 2021년 이후만, 점수 같으면 최근 의결 먼저
        assert r["answerable"] is False and r["gaps"] == "더 찾기"
        r = tc.post("/api/decision/ask", json={"question": "공동행위 감경"}).json()
        assert r["model"] == "gpt-6.1-sol"                                                # 모델을 안 고르면 기본 Sol
        assert [c["model"] for c in models_seen] == ["gpt-6.1-terra"] * 2 + ["gpt-6.1-sol"] * 2   # 실제 호출 모델
        bad = tc.post("/api/decision/ask", json={"question": "가"})
        assert bad.status_code == 400 and bad.json()["error"]["source"] == "AI"
        m = tc.get("/api/decision/models").json()                                        # 의결서 질문용 목록: Haiku 없음, 기본 Sol
        assert m["default"] == "gpt-6.1-sol" and [x["id"] for x in m["models"]] == ["gpt-6.1-sol", "claude-sonnet-5-5", "gpt-6.1-terra"]
        assert [x["id"] for x in m["models"] if x["default"]] == ["gpt-6.1-sol"]
        a = tc.get("/api/ai/models").json()                                              # AI 추천은 그대로 네 개, 기본 Sonnet
        assert a["default"] == "claude-sonnet-5-5" and "claude-haiku-5-5" in [x["id"] for x in a["models"]]
