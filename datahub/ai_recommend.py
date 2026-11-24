"""자연어 질문 → 색인 속 자료 추천(우선순위 목록). 숫자·값은 말하지 않는다.

1) 질문 풀기: Claude가 질문을 통계표 이름에 쓰일 법한 짧은 검색어 몇 개로 바꾼다(어느 기관이 맞을지도).
2) 후보 모으기: 이 프로그램의 검색(동의어 포함)으로 검색어마다 상위 후보를 모은다(KOSIS·ECOS·공공데이터포털).
3) 고르기: Claude가 '번호 붙인 후보 목록 안에서만' 질문에 맞는 순서로 고르고 이유·주의점을 한 줄씩 단다.
   목록에 없는 번호는 버린다 → 색인에 없는 자료를 지어낼 수 없다.

외부로 나가는 것: 질문 문장과 후보 목록(이름·기관·분류·주기·기간·키워드 일부). 색인 전체나 수치는 보내지 않는다.
모델: 기본 Claude Sonnet 5.5. 화면에서 Claude Haiku 5.5(가장 가벼움), GPT-6.1 Sol, GPT-6.1 Terra를 고를 수 있다(MODELS, 2026-11-17 결정 'Claude 2개·GPT 2개').
Claude Sonnet은 거절 시 서버 쪽 대체 모델 사용(fallbacks "default"), Haiku 5.5는 이 기능이 없어 끈다.
"""
from __future__ import annotations

import json
import re
import time

from . import catalog as cat
from . import synonyms
from .config import get_api_key
from .errors import SourceError

# 고를 수 있는 모델(2026-11-17 결정: Claude 2개·GPT 2개). price = (입력, 출력) 달러/100만 토큰, 2026-11-17 공식 가격표
# 표준 요금(Haiku 5.5는 질문 10만 토큰 이하 요금, GPT는 27.2만 토큰 이하 요금 — 이 프로그램의 질문은 그보다 훨씬 짧음)
MODELS = {
    "claude-sonnet-5-5": {"label": "Claude Sonnet 5.5", "provider": "anthropic", "price": (2.0, 10.0), "fallbacks": True},
    "claude-haiku-5-5": {"label": "Claude Haiku 5.5(가장 가벼움)", "provider": "anthropic", "price": (0.10, 0.50), "fallbacks": False},
    "gpt-6.1-sol": {"label": "GPT-6.1 Sol", "provider": "openai", "price": (2.0, 10.0), "fallbacks": False},
    "gpt-6.1-terra": {"label": "GPT-6.1 Terra(가벼움)", "provider": "openai", "price": (1.0, 5.0), "fallbacks": False},
}
DEFAULT_MODEL = "claude-sonnet-5-5"
KEY_OF = {"anthropic": "AI", "openai": "OPENAI"}


def models_info(ids: list[str] | None = None, default: str = DEFAULT_MODEL) -> list[dict]:
    """ids: 보여 줄 모델(기본은 MODELS 전부). 의결서 AI 질문은 decision_ai.DEC_MODELS만 쓴다."""
    return [{"id": k, "label": MODELS[k]["label"], "available": bool(get_api_key(KEY_OF[MODELS[k]["provider"]])),
             "default": k == default} for k in (ids or list(MODELS))]
PER_QUERY = 20                            # 검색어마다 후보 수
MAX_CANDIDATES = 120
MAX_PICKS = 12
SOURCE_NAME = {"KOSIS": "KOSIS", "ECOS": "ECOS", "DATAGO": "공공데이터포털"}

PLAN_SYSTEM = """너는 공정거래·산업 경제분석 연구자를 돕는 '자료 찾기' 도우미다. 사용자가 필요한 자료를 문장으로 설명하면, 아래 색인에서 찾을 검색어를 만든다.
색인: KOSIS(국가통계포털 통계표), ECOS(한국은행 경제통계), 공공데이터포털(data.go.kr 파일·오픈API 목록).
규칙:
- 숫자나 통계 값은 절대 말하지 않는다. 검색어만 만든다.
- 검색어는 통계표·자료 이름에 실제로 쓰일 법한 짧은 한국어(1~4낱말). 예: '면류 출하액', '소매판매액지수', '가맹점 수'. 영문 약어(CPI 등)도 가능.
- 서로 다른 각도로 4~8개: 시장 규모(출하·생산·매출), 가격(물가지수), 사업체·종사자, 수출입, 관련 공공데이터 목록 등 질문에 맞는 것.
- source: 그 검색어가 주로 맞을 곳(any, KOSIS, ECOS, DATAGO). 모르면 any.
- cycle_hint: 질문이 원하는 시간 단위(any, A=연, Q=분기, M=월, D=일). 말이 없으면 any.
- 질문이 너무 모호해 방향이 크게 갈리면 clarify에 되물을 질문 하나(짧게)를 쓰되, 검색어는 그래도 만든다. 아니면 clarify는 빈 문자열.
- understanding: 사용자가 찾는 자료를 한 줄로 다시 쓴 것(한국어). 예: '라면 시장이 커졌는지 볼 수 있는 출하·판매 통계'. '요청'·'검색어를 만든다'처럼 이 작업 자체를 설명하지 않는다."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "understanding": {"type": "string"},
        "searches": {"type": "array", "items": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "source": {"type": "string", "enum": ["any", "KOSIS", "ECOS", "DATAGO"]},
                           "purpose": {"type": "string"}},
            "required": ["query", "source", "purpose"], "additionalProperties": False}},
        "cycle_hint": {"type": "string", "enum": ["any", "A", "Q", "M", "D"]},
        "clarify": {"type": "string"},
    },
    "required": ["understanding", "searches", "cycle_hint", "clarify"], "additionalProperties": False,
}

RANK_SYSTEM = f"""너는 공정거래·산업 경제분석 연구자를 돕는 '자료 찾기' 도우미다. 사용자의 질문과, 이 프로그램 색인에서 찾은 번호 붙은 후보 목록을 받는다.
할 일: 질문에 가장 도움이 될 자료를 '후보 목록 안에서만' 골라 우선순위대로 최대 {MAX_PICKS}개 추천한다.
규칙:
- 후보 목록에 없는 자료를 만들거나 언급하지 않는다. 고른 것은 반드시 후보의 번호(n)로 가리킨다.
- 숫자나 통계 값은 말하지 않는다(값은 사용자가 직접 열어 확인한다).
- reason: 왜 질문에 맞는지 한 줄(무엇을 볼 수 있는지). caution: 열어서 확인할 점 한 줄(정의·범위 차이, 기준연도, 지역 한정, 마지막 자료 연도가 오래됨, 파일·API 형태 등). 없으면 빈 문자열.
- reason·caution·summary에서 다른 자료를 가리킬 때 후보 번호('1번', '[39]')를 쓰지 않는다. 사용자 화면에는 그 번호가 없다. 대신 '2018–2023 조사통계 계열', '2025년 이후 계열'처럼 이름·기간으로 가리킨다.
- 비슷한 표가 여럿이면 가장 대표적인 것(전국·최신·기간이 긴 것)을 앞에 두고 중복은 줄인다.
- 맞는 후보가 없으면 picks는 빈 목록.
- summary: 방향 제시 두세 문장(~습니다 체). 이 질문을 어떤 관점으로 보면 좋은지(질문을 직접 보여 주는 자료가 색인에 없으면, 대신 무엇으로 간접 확인할 수 있는지)와, 그래서 어떤 자료를 앞에 두었고 어떻게 함께 보면 되는지를 쓴다. 자료 종류만 나열하지 않는다.
  예(라면 시장 질문): '시장이 커졌는지는 출하액·판매액 추이로 먼저 보고, 가격이 오른 효과를 빼고 보려면 물가지수를 함께 보도록 순서를 잡았습니다.'
- missing: 질문에 필요해 보이지만 후보 목록에 없는 자료와 찾아볼 곳을 한두 문장으로. 필요한 것이 있으면 빠뜨리지 말고, 정말 없을 때만 빈 문자열.
  · 이 프로그램 안에 있는 기능이면 그쪽을 먼저 안내한다: '회사(DART)' 화면(회사별 재무제표·공시 목록, 같은 업종 상장사 매출·이익 비교 = 업종 비교), '기업집단' 화면(공정위 지정 대규모기업집단의 소속회사·편입/제외 내역), '의결서' 화면(공정위 의결서 약 2만 건 본문 검색·AI 질문: 과거 사건의 시장획정·과징금 산정·위법성 판단 등). 예: '정유사 재무·매출 비교는 이 프로그램 회사(DART) 화면의 업종 비교', '정유사 담합 선례는 이 프로그램 의결서 화면'.
  · 바깥 출처는 실제로 있는 곳만 이름으로 적는다(주소는 쓰지 않음). 예: 한국석유공사 오피넷(주유소·정유사 유가), 한국석유공사 페트로넷(석유 수급), aT KAMIS(농축수산물 가격), 한국소비자원 참가격(생필품 가격), 관세청 수출입무역통계(품목별 수출입), 조달청 나라장터(입찰·낙찰), 업계 협회 통계."""

RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "picks": {"type": "array", "items": {
            "type": "object",
            "properties": {"n": {"type": "integer"}, "reason": {"type": "string"}, "caution": {"type": "string"}},
            "required": ["n", "reason", "caution"], "additionalProperties": False}},
        "missing": {"type": "string"},
    },
    "required": ["summary", "picks", "missing"], "additionalProperties": False,
}


class Usage:
    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.input = self.output = 0
        self.model = model

    def add(self, u) -> None:
        self.input += (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
            + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        self.output += getattr(u, "output_tokens", 0) or 0

    def to_dict(self) -> dict:
        price = (MODELS.get(self.model) or {}).get("price")
        usd = round(self.input / 1e6 * price[0] + self.output / 1e6 * price[1], 4) if price else None
        return {"input_tokens": self.input, "output_tokens": self.output, "usd_estimate": usd}


async def ask_model(model: str, system: str, user: str, schema: dict, effort: str, usage: Usage) -> dict:
    if MODELS[model]["provider"] == "openai":
        return await openai_json(model, system, user, schema, usage)
    return await claude_json(model, system, user, schema, effort, usage)


async def claude_json(model: str, system: str, user: str, schema: dict, effort: str, usage: Usage) -> dict:
    """Claude에 한 번 묻고 JSON(스키마 고정)을 받는다. 오류는 SourceError(source='AI')로 바꾼다."""
    key = get_api_key("AI")
    if not key:
        raise SourceError("AI", "auth", "Claude API 키가 없습니다(ANTHROPIC_API_KEY). README의 '시작하기'를 보세요.")
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=key, timeout=120.0, max_retries=2)
    extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if MODELS[model]["fallbacks"] else {}
    try:
        r = await client.beta.messages.create(
            model=model, max_tokens=16000, **extra,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            system=system, messages=[{"role": "user", "content": user}],
        )
    except anthropic.AuthenticationError:
        raise SourceError("AI", "auth", "Claude API 키가 맞지 않습니다.") from None
    except anthropic.PermissionDeniedError:
        raise SourceError("AI", "auth", "Claude API 키에 이 모델을 쓸 권한이 없습니다.") from None
    except anthropic.RateLimitError:
        raise SourceError("AI", "rate_limit", "Claude API 호출 한도에 걸렸습니다. 잠시 뒤 다시 해 보세요.", "429") from None
    except anthropic.APIStatusError as e:
        raise SourceError("AI", "server" if e.status_code >= 500 else "bad_request",
                          f"Claude API 오류(HTTP {e.status_code}).", str(e.status_code)) from None
    except anthropic.APIConnectionError:
        raise SourceError("AI", "network", "Claude API에 연결하지 못했습니다(인터넷 연결 확인).") from None
    finally:
        await client.close()
    usage.add(r.usage)
    if r.stop_reason == "refusal":
        raise SourceError("AI", "bad_request", "AI가 이 질문에 답하지 않았습니다. 표현을 바꿔 보세요.", "refusal")
    if r.stop_reason == "max_tokens":
        raise SourceError("AI", "server", "AI 답이 길어 잘렸습니다. 질문을 짧게 해 보세요.")
    text = next((b.text for b in r.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except ValueError:
        raise SourceError("AI", "server", "AI 답을 읽지 못했습니다.") from None


async def openai_json(model: str, system: str, user: str, schema: dict, usage: Usage) -> dict:
    """GPT에 한 번 묻고 JSON(스키마 고정, strict)을 받는다."""
    key = get_api_key("OPENAI")
    if not key:
        raise SourceError("AI", "auth", "OpenAI API 키가 없습니다(OPENAI_API_KEY).")
    import openai

    client = openai.AsyncOpenAI(api_key=key, timeout=120.0, max_retries=2)
    try:
        r = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": "result", "schema": schema, "strict": True}},
        )
    except openai.AuthenticationError:
        raise SourceError("AI", "auth", "OpenAI API 키가 맞지 않습니다.") from None
    except openai.RateLimitError:
        raise SourceError("AI", "rate_limit", "OpenAI API 호출 한도에 걸렸습니다. 잠시 뒤 다시 해 보세요.", "429") from None
    except openai.APIStatusError as e:
        raise SourceError("AI", "server" if e.status_code >= 500 else "bad_request",
                          f"OpenAI API 오류(HTTP {e.status_code}).", str(e.status_code)) from None
    except openai.APIConnectionError:
        raise SourceError("AI", "network", "OpenAI API에 연결하지 못했습니다(인터넷 연결 확인).") from None
    finally:
        await client.close()
    if r.usage:
        usage.input += r.usage.prompt_tokens or 0
        usage.output += r.usage.completion_tokens or 0
    ch = r.choices[0]
    if getattr(ch.message, "refusal", None):
        raise SourceError("AI", "bad_request", "AI가 이 질문에 답하지 않았습니다. 표현을 바꿔 보세요.", "refusal")
    if ch.finish_reason == "length":
        raise SourceError("AI", "server", "AI 답이 길어 잘렸습니다. 질문을 짧게 해 보세요.")
    try:
        return json.loads(ch.message.content or "")
    except ValueError:
        raise SourceError("AI", "server", "AI 답을 읽지 못했습니다.") from None


def candidate_line(i: int, h: dict) -> str:
    x = h.get("extra") or {}
    bits = [f"[{i}] {SOURCE_NAME.get(h['source'], h['source'])}", h["name"]]
    if h.get("org_name"):
        bits.append(f"기관: {h['org_name']}")
    if h.get("path"):
        bits.append(f"분류: {h['path'][:70]}")
    if h["source"] == "DATAGO":
        kind = {"FILE": "파일데이터", "API": "오픈API", "STD": "표준데이터"}.get(x.get("kind"), x.get("kind") or "")
        bits.append(f"{kind} {x.get('fmt') or ''}".strip())
        if x.get("modified"):
            bits.append(f"수정 {x['modified']}")
    else:
        if h.get("cycle_labels"):
            bits.append("주기: " + "·".join(h["cycle_labels"]))
        if h.get("span"):
            bits.append(f"기간: {h['span'][0][:4]}–{h['span'][1][:4]}")
        elif h.get("end_year"):
            bits.append(f"최종 갱신 {h['end_year']}")
        if h.get("discontinued"):
            bits.append("마지막 연도가 3년 넘게 지남")
        if h.get("regional"):
            bits.append("지자체 통계")
    return " | ".join(bits)


_REF = re.compile(r"\[(\d{1,3})\]|(?<![\d.])(\d{1,3})번(?!째)")


def name_refs(text: str, cands: list[dict]) -> str:
    """지시를 어기고 남은 후보 번호('[3]', '3번')를 그 후보 이름으로 바꾼다(화면에는 그 번호가 없으므로)."""
    def sub(m):
        n = int(m.group(1) or m.group(2))
        return f"'{cands[n - 1]['name']}'" if 1 <= n <= len(cands) else m.group(0)
    return _REF.sub(sub, text or "")


async def recommend(question: str, catalog: cat.Catalog, llm=None, model: str | None = None) -> dict:
    """llm: 시험용으로 바꿔 끼우는 함수 (system, user, schema, effort, usage) → dict. 기본은 고른 모델(MODELS)."""
    model = model if model in MODELS else DEFAULT_MODEL
    q = " ".join((question or "").split())
    if len(q) < 2:
        raise SourceError("AI", "bad_request", "질문을 두 글자 이상 써 주세요.")
    if len(q) > 500:
        raise SourceError("AI", "bad_request", "질문은 500자 이내로 써 주세요.")
    if llm is None:
        async def ask(system, user, schema, effort, usage_):
            return await ask_model(model, system, user, schema, effort, usage_)
    else:
        ask = llm
    usage = Usage(model)
    t0 = time.time()
    plan = await ask(PLAN_SYSTEM, f"질문: {q}", PLAN_SCHEMA, "low", usage)
    searches = [s for s in (plan.get("searches") or []) if (s.get("query") or "").strip()][:8]
    if not searches:
        searches = [{"query": q[:40], "source": "any", "purpose": "질문 그대로"}]
    cycle = plan.get("cycle_hint") if plan.get("cycle_hint") in ("A", "Q", "M", "D") else None
    # 후보 모으기(검색어 순서대로, 겹치면 처음 것만)
    cands: list[dict] = []
    seen: set = set()
    used = []
    for s in searches:
        src = s.get("source") if s.get("source") in ("KOSIS", "ECOS", "DATAGO") else None
        terms = synonyms.expand(s["query"])
        hits = catalog.search(terms, source=src, limit=PER_QUERY)
        if not hits and src:                          # 기관을 잘못 짚었으면 전체에서 한 번 더
            hits = catalog.search(terms, source=None, limit=PER_QUERY)
        n_new = 0
        for h in hits:
            key = (h.entry.source, h.entry.ds_id)
            if key in seen or len(cands) >= MAX_CANDIDATES:
                continue
            seen.add(key)
            d = cat.hit_to_dict(h)
            d["found_by"] = s["query"]
            cands.append(d)
            n_new += 1
        used.append({"query": s["query"], "source": src or "any", "purpose": s.get("purpose", ""), "found": len(hits), "new": n_new})
    out = {"question": q, "understanding": plan.get("understanding", ""), "clarify": plan.get("clarify", ""),
           "cycle_hint": cycle, "searches": used, "candidates": len(cands), "model": model,
           "model_label": MODELS[model]["label"]}
    if not cands:
        out.update({"summary": "색인에서 후보를 찾지 못했습니다. 표현을 바꾸거나 검색어로 직접 찾아 보세요.", "missing": "",
                    "recommendations": [], "usage": usage.to_dict(), "elapsed_s": round(time.time() - t0, 1)})
        return out
    lines = "\n".join(candidate_line(i, h) for i, h in enumerate(cands, start=1))
    hint = f"\n원하는 시간 단위: {cycle}" if cycle else ""
    rank = await ask(RANK_SYSTEM, f"질문: {q}{hint}\n\n후보 목록({len(cands)}개):\n{lines}", RANK_SCHEMA, "medium", usage)
    recs, taken = [], set()
    dropped = 0
    for p in rank.get("picks") or []:
        n = p.get("n")
        if not isinstance(n, int) or not (1 <= n <= len(cands)) or n in taken:
            dropped += 1                     # 목록에 없는 번호·중복은 버린다
            continue
        taken.add(n)
        recs.append({**cands[n - 1], "rank": len(recs) + 1, "ai_reason": name_refs(p.get("reason", ""), cands),
                     "ai_caution": name_refs(p.get("caution", ""), cands)})
        if len(recs) >= MAX_PICKS:
            break
    out.update({"summary": name_refs(rank.get("summary", ""), cands), "missing": name_refs(rank.get("missing", ""), cands),
                "recommendations": recs,
                "dropped_picks": dropped, "usage": usage.to_dict(), "elapsed_s": round(time.time() - t0, 1)})
    return out
