"""동의어 사전(data/synonyms.csv).

열: 대표어, 같은말, 관련어, 대표통계표, 메모   (여러 개는 '|'로 구분)
- 같은말: 같은 통계를 가리키는 다른 이름(CPI = 소비자물가지수). 검색 가중치 0.9.
- 관련어: 함께 찾아볼 만하지만 통계 정의가 다를 수 있는 말(기업 규모 → 종사자 수). 가중치 0.7,
  화면에 '관련어'로 따로 표시한다.
- 대표통계표: 이 말로 찾으면 맨 위에 올릴 통계표. 'KOSIS:101/DT_1J22003|ECOS:901Y009' 처럼 기관:ID.
파일을 고치면 프로그램을 다시 켜지 않아도 다음 검색부터 반영된다.
"""
from __future__ import annotations

import csv
import threading

from .catalog import norm
from .config import SYNONYM_FILE

_lock = threading.Lock()
_cache: dict = {"mtime": None, "groups": []}


def _load() -> list[dict]:
    try:
        mtime = SYNONYM_FILE.stat().st_mtime
    except OSError:
        return []
    with _lock:
        if _cache["mtime"] == mtime:
            return _cache["groups"]
        groups = []
        with open(SYNONYM_FILE, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                head = (row.get("대표어") or "").strip()
                if not head:
                    continue
                same = [x.strip() for x in (row.get("같은말") or "").split("|") if x.strip()]
                rel = [x.strip() for x in (row.get("관련어") or "").split("|") if x.strip()]
                pins = []
                for x in (row.get("대표통계표") or "").split("|"):
                    if ":" in x:
                        src, ident = x.strip().split(":", 1)
                        pins.append((src.strip().upper(), ident.strip()))
                groups.append({"head": head, "same": [head] + same, "related": rel, "pins": pins,
                               "n_same": {norm(x) for x in [head] + same}})
        _cache.update(mtime=mtime, groups=groups)
        return groups


def expand(query: str, max_terms: int = 12) -> list[tuple[str, str, float]]:
    """[(검색어, 이유, 가중치)] — 첫 줄은 입력 그대로."""
    q = " ".join(query.split())
    out: list[tuple[str, str, float]] = [(q, "입력어", 1.0)]
    seen = {norm(q)}
    groups = _load()
    nq = norm(q)
    toks = q.split()

    def add(term: str, why: str, w: float) -> None:
        n = norm(term)
        if n and n not in seen and len(out) < max_terms:
            seen.add(n)
            out.append((term, why, w))

    tok_groups = [{gi for gi, g in enumerate(groups) if norm(tk) in g["n_same"]} for tk in toks]
    common = set.intersection(*tok_groups) if toks and all(tok_groups) else set()
    for gi, g in enumerate(groups):
        # 입력 전체가 묶음의 한 말이거나, 낱말이 모두 같은 묶음('원달러 환율')이면 묶음 전체를 펼친다
        if nq in g["n_same"] or (len(toks) > 1 and gi in common):
            for s in g["same"]:
                add(s, f"동의어 '{s}'", 0.9)
            for r in g["related"]:
                add(r, f"관련어 '{r}'", 0.7)
    if len(toks) > 1 and not common:   # 낱말 하나씩 바꿔 본다: 'CPI 서울' → '소비자물가지수 서울'
        for i, tk in enumerate(toks):
            others = {norm(x) for j, x in enumerate(toks) if j != i}
            for g in groups:
                if norm(tk) in g["n_same"]:
                    for s in g["same"]:
                        if norm(s) in others:      # '원달러 환율' → '환율 환율' 같은 중복은 만들지 않음
                            continue
                        alt = " ".join(toks[:i] + [s] + toks[i + 1:])
                        add(alt, f"동의어 '{s}'", 0.9)
    return out


def pins_for(query: str) -> list[tuple[str, str]]:
    """검색어가 어떤 묶음의 같은말이면(또는 낱말이 모두 한 묶음이면) 그 묶음의 대표 통계표."""
    q = " ".join(query.split())
    nq = norm(q)
    toks = q.split()
    groups = _load()
    tok_groups = [{gi for gi, g in enumerate(groups) if norm(tk) in g["n_same"]} for tk in toks]
    common = set.intersection(*tok_groups) if toks and all(tok_groups) else set()
    out: list[tuple[str, str]] = []
    for gi, g in enumerate(groups):
        if nq in g["n_same"] or (len(toks) > 1 and gi in common):
            for p in g["pins"]:
                if p not in out:
                    out.append(p)
    return out


def related_info(query: str) -> dict:
    """화면 표시용: 이번 검색에 붙은 동의어·관련어."""
    terms = expand(query)
    return {
        "synonyms": [t for t, why, w in terms[1:] if why.startswith("동의어")],
        "related": [t for t, why, w in terms[1:] if why.startswith("관련어")],
    }


def all_groups() -> list[dict]:
    return [{"head": g["head"], "same": g["same"][1:], "related": g["related"]} for g in _load()]
