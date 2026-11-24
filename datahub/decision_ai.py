"""의결서 AI 검색: 질문 → 근거 쪽을 붙인 답.

1) 찾을 말 만들기: AI가 질문을 의결서 본문에 실제로 쓰일 법률·실무 용어 조합 3~6개로 바꾼다(위반유형·연도 힌트도).
2) 근거 쪽 모으기(이 프로그램 안 색인, 비용 없음): 찾을 말마다 '많이 나온 순' 상위 의결서에서 찾는 말이 가장 많이
   함께 나온 쪽을 고르고, 여러 찾을 말에 걸린 쪽일수록 앞에 둔다(의결서당 최대 3쪽, 모두 최대 MAX_PAGES쪽).
3) 답하기: AI에 번호 붙인 쪽 본문만 주고 그 안에서만 답하게 하며, 문장마다 근거 번호를 달게 한다.
   보내지 않은 번호는 버린다 → 근거 없는 인용을 화면에 띄우지 않는다.

밖으로 나가는 것: 질문 문장, 찾을 말, 고른 쪽 본문(공개된 의결서)을 고른 AI 회사(Anthropic·OpenAI)로.
공정위 사이트로는 아무것도 가지 않는다. 모델·가격은 ai_recommend.MODELS에서 DEC_MODELS만 쓴다.
11/17 시험(질문 3개 × 4모델, 별도 검토자가 문장마다 근거 쪽 대조)에서 Haiku 5.5는 각주 속 다른 사건 판결을
그 사건 판단처럼 쓰는 등 해석 오류가 반복돼 뺐고, 가장 정확했던 GPT-6.1 Sol을 기본으로 했다.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import date

from . import decisions as dc
from .ai_recommend import MODELS, Usage, ask_model
from .errors import SourceError

MAX_SEARCHES = 6
MAX_PAGES = 20           # AI에 보낼 쪽 수 상한(질문 한 번 입력 약 1.5만~3만 토큰)
PER_DOC = 3              # 의결서 한 건에서 보낼 쪽 수 상한
DOCS_PER_SEARCH = 10     # 찾을 말마다 살펴볼 의결서 수
PAGE_CHARS = 3500        # 한 쪽 본문 상한(거의 모든 쪽은 이보다 짧음)
DEC_MODELS = ["gpt-6.1-sol", "claude-sonnet-5-5", "gpt-6.1-terra"]   # 의결서 질문에서 고를 수 있는 모델(보이는 순서)
DEC_DEFAULT = "gpt-6.1-sol"

PLAN_SYSTEM = """너는 공정거래 실무자를 돕는 '공정위 의결서 찾기' 도우미다. 사용자가 공정위 의결서에 대해 묻는 질문을 받아, 의결서 본문 전문 검색에 쓸 검색어를 만든다.
검색 방식(중요):
- 검색어 안 낱말은 띄어쓰기로 나뉘고, 한 의결서 안에 모든 낱말이 있어야 찾힌다(같은 쪽이 아니어도 됨).
- 낱말은 앞부분이 같으면 찾는다('공동행위' → '공동행위를'·'공동행위의'). 낱말 가운데는 못 찾는다('부당한공동행위' 속 '공동행위').
- 따옴표로 묶은 "구절"은 그 순서로 붙어 있어야 찾는다.
규칙:
- 검색어는 의결서 본문에 실제로 쓰이는 법률·실무 용어로 2~3낱말. 예: '자진신고 감경', '관련매출액 산정', '"부당한 공동행위" 입찰', '재판매가격유지 정당한 이유'.
- 서로 다른 각도로 3~6개: 핵심 쟁점 용어, 그 쟁점이 판단되는 자리의 용어(예: 과징금 산정 단계, 감경 사유, 위법성 판단), 같은 뜻의 다른 표현(예: 자진신고·감면신청).
- 질문에 회사·사건 이름이 있으면 그 이름 하나만으로 된 검색어도 넣는다.
- 한 검색어에 4낱말을 넘기지 않는다(많을수록 덜 찾힌다). 한 글자 낱말은 쓰지 않는다.
- types: 질문이 특정 위반유형에 해당하면 주어진 목록에서 고른다(확실할 때만, 여러 개 가능). 아니면 빈 목록.
- year_from, year_to: 질문에 기간(예: '최근 5년', '2015년 이후')이 있으면 의결 연도로. 없으면 0.
- understanding: 사용자가 알고 싶은 것을 한 줄로 다시 쓴 것(한국어). 이 작업 자체를 설명하지 않는다."""

ANSWER_SYSTEM = """너는 공정거래 실무자를 돕는 공정위 의결서 조사 도우미다. 사용자의 질문과, 공정위 의결서에서 뽑은 번호 붙은 쪽 본문(발췌)을 받는다.
규칙:
- 발췌에 적힌 내용으로만 답한다. 발췌에 없는 사건·사실·숫자·법 조문·판례를 지어내거나 기억으로 보태지 않는다.
- 문장마다 근거 발췌 번호를 [3]처럼 붙인다. 근거가 여럿이면 [3][7]. 근거 없는 문장은 쓰지 않는다(일반적인 연결 문장 제외).
- '공정위는 ○○ 사건(의결번호)에서 ~라고 판단했습니다'처럼 어느 사건의 판단인지 밝힌다. 사건마다 판단이 다르면 나눠 쓴다.
- 숫자(과징금액, 비율, 날짜, 조문 번호)와 단계(예: 1차·2차 조정)는 사건별로 발췌에 적힌 그대로 옮긴다. 사건마다 다른 것을 '1차 또는 2차'처럼 뭉뚱그리지 않는다.
- 발췌 속 각주·참조(예: '이 건과 같이 ~ 사안에서 ~ 판결 참조')는 그 의결서가 '다른 사건'을 끌어온 것이다. 그 다른 사건의 판단·판결을 지금 의결서 사건의 판단으로 쓰지 않는다. 쓸 때는 '○○ 의결서가 각주에서 인용한 다른 사건(서울고법 2008누2851)에서는 ~'처럼 밝힌다.
- 심사기준·법리 설명의 '~할 수 있다', '~로 볼 수 있다'를 실제로 그렇게 처리했다고 바꾸지 않는다. 일반 기준과 그 사건에서 실제로 한 판단을 구분한다.
- 질문에 조건(예: 입찰담합, 자진신고자, 온라인 플랫폼)이 있으면 그 조건에 맞는 사건을 먼저 쓴다. 조건에 맞지 않는 사건을 참고로 쓰면 그 문장에 '(입찰담합 사건은 아님)'처럼 범위 밖임을 밝힌다. 발췌가 질문 조건에 맞는지 확실하지 않으면 그렇다고 쓴다.
- 발췌가 질문에 직접 답하지 못하면 answerable을 false로 하고, 발췌에서 알 수 있는 데까지만 쓰고, 무엇을 더 찾아보면 좋을지 gaps에 쓴다.
- 발췌는 PDF에서 뽑은 글자라 표·줄바꿈이 깨져 있을 수 있다. 표 숫자를 확신할 수 없으면 원문 확인이 필요하다고 쓴다.
- 법률 의견처럼 단정하지 않는다. ~습니다 체.
- summary: 질문에 대한 핵심 답 2~4문장(근거 번호 포함).
- points: 사건별·쟁점별 세부 내용(최대 8개). text에 근거 번호를 넣고, sources에 그 번호들을 숫자로 넣는다.
- gaps: 발췌로 확인되지 않은 것, 더 찾아볼 것(예: 다른 검색어, 다른 위반유형). 없으면 빈 문자열."""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answerable": {"type": "boolean"},
        "summary": {"type": "string"},
        "points": {"type": "array", "items": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "sources": {"type": "array", "items": {"type": "integer"}}},
            "required": ["text", "sources"], "additionalProperties": False}},
        "gaps": {"type": "string"},
    },
    "required": ["answerable", "summary", "points", "gaps"], "additionalProperties": False,
}


def plan_schema(type_names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "understanding": {"type": "string"},
            "searches": {"type": "array", "items": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "purpose": {"type": "string"}},
                "required": ["query", "purpose"], "additionalProperties": False}},
            "types": {"type": "array", "items": {"type": "string", "enum": type_names or ["(없음)"]}},
            "year_from": {"type": "integer"},
            "year_to": {"type": "integer"},
        },
        "required": ["understanding", "searches", "types", "year_from", "year_to"], "additionalProperties": False,
    }


_MARK = re.compile(r"\[(\d{1,3})\]")


def clean_refs(text: str, n_max: int) -> tuple[str, set[int], int]:
    """본문 속 [n] 가운데 보낸 범위 밖 번호는 지운다 → (고친 글, 남은 번호, 지운 수)."""
    kept: set[int] = set()
    dropped = 0

    def sub(m):
        nonlocal dropped
        n = int(m.group(1))
        if 1 <= n <= n_max:
            kept.add(n)
            return m.group(0)
        dropped += 1
        return ""
    out = _MARK.sub(sub, text or "")
    if dropped:                     # 지운 자리에 남은 빈칸 정리('근거 .' → '근거.')
        out = re.sub(r"[ \t]{2,}", " ", out)
        out = re.sub(r"[ \t]+([.,;:)])", r"\1", out)
    return out.strip(), kept, dropped


def source_header(n: int, d: dict, page: int) -> str:
    return f"[{n}] {d['의결번호']} ({d['의결일']}) {d['사건명']} — {page}쪽"


async def ask(question: str, model: str | None = None, types: list[str] | None = None, acts: list[str] | None = None,
              y1: int | None = None, y2: int | None = None, llm=None) -> dict:
    """types·acts·y1·y2: 화면에서 고른 조건(있으면 AI의 힌트보다 우선).
    llm: 시험용으로 바꿔 끼우는 함수 (system, user, schema, effort, usage) → dict."""
    model = model if model in DEC_MODELS else DEC_DEFAULT       # 목록에 없는 모델(Haiku 등)은 기본으로
    q = " ".join((question or "").split())
    if len(q) < 2:
        raise SourceError("AI", "bad_request", "질문을 두 글자 이상 써 주세요.")
    if len(q) > 500:
        raise SourceError("AI", "bad_request", "질문은 500자 이내로 써 주세요.")
    idx = await asyncio.to_thread(dc.need)
    if llm is None:
        async def call(system, user, schema, effort, usage_):
            return await ask_model(model, system, user, schema, effort, usage_)
    else:
        call = llm
    usage = Usage(model)
    t0 = time.time()
    type_names = [t for t, _ in idx.types.most_common()]
    plan = await call(PLAN_SYSTEM + f"\n올해는 {date.today().year}년이다.",
                      f"위반유형 목록: {', '.join(type_names)}\n\n질문: {q}", plan_schema(type_names), "low", usage)
    searches = [s for s in (plan.get("searches") or []) if dc.parse(s.get("query") or "")[0]][:MAX_SEARCHES]
    if not searches:
        searches = [{"query": q[:60], "purpose": "질문 그대로"}]
    user_filter = bool(types or acts or y1 or y2)
    if user_filter:             # 화면에서 고른 조건이 있으면 그대로
        f_types, f_y1, f_y2 = list(types or []), y1 or None, y2 or None
    else:
        f_types = [t for t in (plan.get("types") or []) if t in idx.types]
        f_y1 = plan.get("year_from") if isinstance(plan.get("year_from"), int) and 1980 <= plan["year_from"] <= 2100 else None
        f_y2 = plan.get("year_to") if isinstance(plan.get("year_to"), int) and 1980 <= plan["year_to"] <= 2100 else None
    f_acts = list(acts or [])

    def gather(f_types, f_y1, f_y2):
        score: dict[tuple, dict] = {}
        used = []
        for s in searches:
            pages = idx.best_pages(s["query"], f_types, f_acts, f_y1, f_y2, max_docs=DOCS_PER_SEARCH, per_doc=PER_DOC)
            used.append({"query": s["query"], "purpose": s.get("purpose", ""), "docs": len({p["doc"] for p in pages})})
            for p in pages:
                k = (p["doc"], p["page"])
                e = score.setdefault(k, {**p, "score": 0.0, "queries": []})
                e["score"] += p["matched"] / p["of"] + (1.0 if p["matched"] == p["of"] else 0.0)
                e["queries"].append(s["query"])
        # 점수 높은 순 → 같으면 최근 의결 → 같은 의결서면 앞쪽
        ranked = sorted(score.values(), key=lambda e: (e["score"], e["date"] or "", -e["page"]), reverse=True)
        picked, per = [], {}
        for e in ranked:
            if per.get(e["doc"], 0) >= PER_DOC:
                continue
            per[e["doc"]] = per.get(e["doc"], 0) + 1
            picked.append(e)
            if len(picked) >= MAX_PAGES:
                break
        return picked, used

    picked, used = await asyncio.to_thread(gather, f_types, f_y1, f_y2)
    widened = False
    if not picked and not user_filter and (f_types or f_y1 or f_y2):   # AI가 짚은 유형·연도가 너무 좁았으면 조건 없이 한 번 더
        picked, used = await asyncio.to_thread(gather, [], None, None)
        f_types, f_y1, f_y2, widened = [], None, None, True
    # 같은 의결서의 쪽은 붙여서, 의결서는 점수 순서대로 보낸다(읽기 쉽게)
    order = {}
    for e in picked:
        order.setdefault(e["doc"], len(order))
    picked.sort(key=lambda e: (order[e["doc"]], e["page"]))
    bodies = await asyncio.to_thread(idx.texts, [e["rowid"] for e in picked])
    terms = []
    for s in searches:
        for t in dc.parse(s["query"])[0]:
            if t.text not in terms:
                terms.append(t.text)
    highlight = " ".join(f'"{t}"' if " " in t else t for t in terms[:dc.MAX_TERMS])
    rx = dc._regex(dc.parse(highlight)[0])
    sources = []
    for n, e in enumerate(picked, start=1):
        d = idx.docs[e["doc"]]
        sources.append({"n": n, "id": e["doc"], "page": e["page"], "no": d["의결번호"], "date": d["의결일"], "name": d["사건명"],
                        "types": d["types"], "status": d["상태"], "queries": e["queries"],
                        **dc.snippet(bodies.get(e["rowid"], ""), rx)})
    out = {"question": q, "understanding": plan.get("understanding", ""), "searches": used,
           "filters": {"types": f_types, "acts": f_acts, "y1": f_y1, "y2": f_y2, "from_user": user_filter, "widened": widened},
           "highlight": highlight, "sources": sources, "model": model, "model_label": MODELS[model]["label"]}
    if not picked:
        out.update({"answerable": False, "summary": "의결서에서 관련 쪽을 찾지 못했습니다. 표현을 바꾸거나 '낱말 검색'으로 직접 찾아 보세요.",
                    "points": [], "gaps": "", "dropped_refs": 0, "usage": usage.to_dict(), "elapsed_s": round(time.time() - t0, 1)})
        return out
    blocks = []
    for s, e in zip(sources, picked):
        text = bodies.get(e["rowid"], "")
        if len(text) > PAGE_CHARS:
            text = text[:PAGE_CHARS] + " …(이 쪽 뒷부분 생략)"
        blocks.append(f"{source_header(s['n'], idx.docs[e['doc']], e['page'])}\n<발췌>\n{text}\n</발췌>")
    ans = await call(ANSWER_SYSTEM, f"질문: {q}\n\n발췌 {len(sources)}개:\n\n" + "\n\n".join(blocks), ANSWER_SCHEMA, "medium", usage)
    n_max = len(sources)
    summary, cited, dropped = clean_refs(ans.get("summary", ""), n_max)
    points = []
    for p in ans.get("points") or []:
        text, kept, dr = clean_refs(p.get("text", ""), n_max)
        srcs = sorted({n for n in (p.get("sources") or []) if isinstance(n, int) and 1 <= n <= n_max} | kept)
        dropped += dr + sum(1 for n in (p.get("sources") or []) if not (isinstance(n, int) and 1 <= n <= n_max))
        if text:
            points.append({"text": text, "sources": srcs})
            cited |= set(srcs)
    gaps, kept, dr = clean_refs(ans.get("gaps", ""), n_max)
    cited |= kept
    dropped += dr
    for s in sources:
        s["cited"] = s["n"] in cited
    out.update({"answerable": bool(ans.get("answerable")), "summary": summary, "points": points, "gaps": gaps,
                "dropped_refs": dropped, "usage": usage.to_dict(), "elapsed_s": round(time.time() - t0, 1)})
    return out
