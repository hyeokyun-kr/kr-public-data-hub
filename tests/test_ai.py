"""AI 추천 테스트(실제 Claude를 부르지 않고 가짜 함수로).

    python3 -m pytest -q tests/test_ai.py
"""
from __future__ import annotations

import asyncio

import pytest

from datahub import ai_recommend, catalog as cat, config
from datahub.errors import SourceError


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cat, "CATALOG_DB", tmp_path / "c.sqlite")
    monkeypatch.setattr(config, "_find_api_key", lambda s: None)   # 환경변수·.env 의 실제 키로 실제 API를 부르지 않게
    config._KEY_CACHE.clear()
    yield
    config._KEY_CACHE.clear()


def small_catalog():
    con = cat.connect()
    rows = [("KOSIS", "101/A", "면류 출하액", "광업제조업조사", {"A": ["2010", "2024"]}),
            ("KOSIS", "101/B", "라면 생산량", "식품", {"A": ["2015", "2024"]}),
            ("ECOS", "404Y014", "생산자물가지수(품목별)", "물가", {"M": ["2010-01", "2026-10"]}),
            ("DATAGO", "15000001", "한국농수산식품유통공사_가공식품 세분시장 현황(라면)", "농림 - 식품", {})]
    for src, ds, name, path, ranges in rows:
        cat.upsert_dataset(con, {"source": src, "ds_id": ds, "name": name, "path": path, "ranges": ranges,
                                 "end_year": 2024, "org_name": "기관", "keywords": "라면" if src == "DATAGO" else None,
                                 "extra": {"kind": "FILE", "fmt": "csv", "modified": "2025-01-01"} if src == "DATAGO" else None})
    con.commit()
    con.close()
    return cat.Catalog()


def run(coro):
    return asyncio.run(coro)


def fake_llm(plan, picks_fn, calls):
    async def ask(system, user, schema, effort, usage):
        calls.append((system[:20], user, effort))
        if schema is ai_recommend.PLAN_SCHEMA:
            return plan
        return picks_fn(user)
    return ask


def test_recommend_flow_and_guards():
    c = small_catalog()
    calls = []
    plan = {"understanding": "라면 시장 규모", "cycle_hint": "any", "clarify": "",
            "searches": [{"query": "면류 출하액", "source": "KOSIS", "purpose": "규모"},
                         {"query": "라면", "source": "any", "purpose": "직접"},
                         {"query": "생산자물가", "source": "ECOS", "purpose": "가격"}]}

    def picks(user):
        assert "[1]" in user and "면류 출하액" in user and "숫자" not in user[:0]
        return {"summary": "규모·가격", "missing": "",
                "picks": [{"n": 1, "reason": "출하액", "caution": ""}, {"n": 99, "reason": "없는 번호", "caution": ""},
                          {"n": 1, "reason": "중복", "caution": ""}, {"n": 3, "reason": "가격", "caution": "기준연도"}]}
    res = run(ai_recommend.recommend("라면 시장이 커졌는지 볼 자료", c, llm=fake_llm(plan, picks, calls)))
    names = [r["name"] for r in res["recommendations"]]
    assert names[0] == "면류 출하액" and len(names) == 2 and res["dropped_picks"] == 2     # 목록 밖 번호·중복은 버림
    assert [r["rank"] for r in res["recommendations"]] == [1, 2]
    assert res["recommendations"][1]["ai_caution"] == "기준연도"
    assert {s["query"] for s in res["searches"]} == {"면류 출하액", "라면", "생산자물가"}
    assert res["candidates"] >= 3 and calls[0][2] == "low" and calls[1][2] == "medium"
    # 후보 목록 줄에는 기관·분류·기간이 들어가고 값은 없다
    line = ai_recommend.candidate_line(1, {"source": "DATAGO", "name": "x", "org_name": "y", "path": "p",
                                           "extra": {"kind": "FILE", "fmt": "csv", "modified": "2025-01-01"}})
    assert line == "[1] 공공데이터포털 | x | 기관: y | 분류: p | 파일데이터 csv | 수정 2025-01-01"


def test_no_candidates_and_input_checks():
    c = small_catalog()
    plan = {"understanding": "?", "cycle_hint": "any", "clarify": "무엇을 보려는지?", "searches": [{"query": "존재하지않는말", "source": "any", "purpose": ""}]}
    calls = []
    res = run(ai_recommend.recommend("아무거나", c, llm=fake_llm(plan, lambda u: {}, calls)))
    assert res["recommendations"] == [] and len(calls) == 1 and res["clarify"]          # 후보가 없으면 두 번째 호출 안 함
    for bad in ("", "a", "가" * 501):
        with pytest.raises(SourceError) as e:
            run(ai_recommend.recommend(bad, c, llm=fake_llm(plan, lambda u: {}, [])))
        assert e.value.kind == "bad_request"


def test_missing_key_is_auth_error():
    c = small_catalog()
    with pytest.raises(SourceError) as e:
        run(ai_recommend.recommend("라면 시장 자료", c))
    assert e.value.kind == "auth" and e.value.source == "AI"



def test_model_choice_and_name_refs():
    assert ai_recommend.DEFAULT_MODEL == "claude-sonnet-5-5"
    assert set(ai_recommend.MODELS) == {"claude-sonnet-5-5", "claude-haiku-5-5", "gpt-6.1-sol", "gpt-6.1-terra"}   # 11/17 Claude 2·GPT 2
    assert all(v["price"] for v in ai_recommend.MODELS.values())                     # 비용 표시에 쓰는 가격이 모두 있음
    assert ai_recommend.MODELS["claude-haiku-5-5"]["fallbacks"] is False      # Haiku 5.5는 서버 대체 모델 기능 없음
    cands = [{"name": "가 표"}, {"name": "나 표"}, {"name": "다 표"}]
    assert ai_recommend.name_refs("[2]와 3번 계열, 2번째, 99번", cands) == "'나 표'와 '다 표' 계열, 2번째, 99번"
    c = small_catalog()
    plan = {"understanding": "x", "cycle_hint": "any", "clarify": "", "searches": [{"query": "면류 출하액", "source": "any", "purpose": ""}]}
    res = run(ai_recommend.recommend("라면 자료", c, model="없는모델",
                                     llm=fake_llm(plan, lambda u: {"summary": "[1] 중심", "missing": "", "picks": [{"n": 1, "reason": "[1] 참고", "caution": ""}]}, [])))
    assert res["model"] == "claude-sonnet-5-5"                                   # 모르는 모델은 기본으로
    assert res["summary"] == "'면류 출하액' 중심" and res["recommendations"][0]["ai_reason"] == "'면류 출하액' 참고"
