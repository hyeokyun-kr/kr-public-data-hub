"""한국은행 ECOS Open API.

쓰는 서비스
- StatisticTableList  통계표 목록(폴더 포함, SRCH_YN=Y 인 것만 자료 조회 가능)
- StatisticItemList   통계표의 항목 목록(항목×주기마다 한 줄: 수록 시작·끝, 단위, 건수)
- StatisticSearch     자료 조회 /통계표/주기/시작/끝/항목1/항목2/항목3/항목4  ('?' = 그 자리 전체)
- StatisticWord       통계용어사전
URL 형식: https://ecos.bok.or.kr/api/{서비스}/{인증키}/json/kr/{시작건}/{끝건}/...
"""
from __future__ import annotations

import asyncio
from urllib.parse import quote

import httpx

from ..config import get_api_key
from ..errors import ECOS_CODES, SourceError
from . import get_json, limiter

BASE = "https://ecos.bok.or.kr/api"
SOURCE = "ECOS"
PAGE = 10000


def _key() -> str:
    k = get_api_key(SOURCE)
    if not k:
        raise SourceError(SOURCE, "auth", "ECOS 인증키가 없습니다. README의 '시작하기'를 보세요.")
    return k


def _url(service: str, key: str, start: int, end: int, *parts: str) -> str:
    tail = "/".join(quote(str(p), safe="") for p in parts)   # '?'(전체)도 %3F 로 경로 안에 둔다
    return f"{BASE}/{service}/{key}/json/kr/{start}/{end}/" + tail


def masked(url: str, key: str) -> str:
    return url.replace(f"/{key}/", "/{ECOS_API_KEY}/")


async def _call(client: httpx.AsyncClient, service: str, *parts: str, start: int = 1, end: int = PAGE,
                log: list | None = None) -> tuple[list, int]:
    key = _key()
    url = _url(service, key, start, end, *parts)
    if log is not None:
        log.append(masked(url, key))
    async with limiter(SOURCE):
        data = await get_json(client, SOURCE, url, None, key)
    if "RESULT" in data:
        code = str(data["RESULT"].get("CODE", ""))
        msg = str(data["RESULT"].get("MESSAGE", "")).strip()
        kind = ECOS_CODES.get(code, "server")
        raise SourceError(SOURCE, kind, msg or code, code)
    body = data.get(service)
    if not body:
        raise SourceError(SOURCE, "server", f"예상하지 못한 응답 형식입니다({service}).")
    return body.get("row", []) or [], int(body.get("list_total_count", 0) or 0)


async def _call_all(client: httpx.AsyncClient, service: str, *parts: str, log: list | None = None,
                    notes: list | None = None) -> list:
    """모든 페이지를 받는다.

    ECOS의 list_total_count는 '?'(전체) 조회 때 실제 행 수보다 크게 나오는 일이 있다
    (2026-11-13: 141Y002 '?/X00/?' total 13·실제 9행, 132Y003 '?/?/?' total 18,098·실제 17,234행).
    그래서 total만 믿을 수도, 짧은 페이지만 믿을 수도 없다. 정책:
    - 받은 수가 total보다 적으면, 페이지가 짧았어도 다음 범위를 한 번 더 요청한다(더 오면 이어 받음).
    - 더 오지 않으면(빈 목록 또는 INFO-200) 거기서 끝내고, 차이를 notes에 남겨 결과에 경고로 보인다.
    - 앞 페이지를 받은 뒤의 INFO-200은 '끝'으로 보고, 이미 받은 행을 버리지 않는다.
    """
    rows, total = await _call(client, service, *parts, start=1, end=PAGE, log=log)
    got = len(rows)
    while got < total:
        try:
            more, _ = await _call(client, service, *parts, start=got + 1, end=got + PAGE, log=log)
        except SourceError as e:
            if e.kind != "no_data":
                raise
            more = []
        if not more:
            break
        rows.extend(more)
        got += len(more)
    if got != total and notes is not None:
        notes.append((total, got))
    return rows


async def table_list(client: httpx.AsyncClient, notes: list | None = None) -> list[dict]:
    return await _call_all(client, "StatisticTableList", "", notes=notes)


async def item_list(client: httpx.AsyncClient, stat_code: str, log: list | None = None,
                    notes: list | None = None) -> list[dict]:
    return await _call_all(client, "StatisticItemList", stat_code, log=log, notes=notes)


async def search(client: httpx.AsyncClient, stat_code: str, cycle: str, start: str, end: str,
                 codes: list[str], log: list | None = None, notes: list | None = None) -> list[dict]:
    """codes: 그룹 순서대로 항목 코드('?' = 전체). 첫 페이지부터 결과 없음(INFO-200)이면 빈 목록."""
    try:
        return await _call_all(client, "StatisticSearch", stat_code, cycle, start, end, *codes, log=log, notes=notes)
    except SourceError as e:
        if e.kind == "no_data":
            return []
        raise


async def word(client: httpx.AsyncClient, term: str) -> str | None:
    try:
        rows, _ = await _call(client, "StatisticWord", term, start=1, end=3)
    except SourceError:
        return None
    for r in rows:
        if r.get("WORD", "").replace(" ", "") == term.replace(" ", ""):
            return r.get("CONTENT")
    return rows[0].get("CONTENT") if rows else None
