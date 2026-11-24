"""원천 기관 API 연결 모듈. 기관마다 파일 하나(ecos.py, kosis.py, dart.py)로 나눠 두어
나중에 오피넷·공공데이터포털 등을 같은 모양으로 붙일 수 있게 한다."""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from ..errors import SourceError

TIMEOUT = httpx.Timeout(60.0, connect=15.0)
HEADERS = {"User-Agent": "kr-public-data-hub/0.4"}
_LIMITERS: dict = {}


def limiter(source: str, n: int = 4) -> asyncio.Semaphore:
    """기관별 동시 호출 수 제한(이벤트 루프마다 따로 만든다)."""
    loop = asyncio.get_running_loop()
    key = (id(loop), source)
    sem = _LIMITERS.get(key)
    if sem is None:
        sem = _LIMITERS[key] = asyncio.Semaphore(n)
    return sem


# 기관별 호출 속도 제한(슬라이딩 창). ECOS는 '3분간 300회 이상' 부르면 30분 동안 막히고,
# KOSIS는 '1분간 200건'이 한도다(2026-11-13 둘 다 실제로 걸림).
# OpenDART는 하루 2만 건 한도(오류 020)만 공개돼 있어 분당 300회로 여유 있게 둔다.
RATE_LIMITS = {"ECOS": (240, 180.0), "KOSIS": (170, 60.0), "DART": (300, 60.0), "DATAGO": (60, 60.0), "EGROUP": (300, 60.0),
               "FTC": (20, 60.0)}   # 공정위 사건처리시스템: 의결서 원문 PDF를 열 때만(누를 때마다 1~2번)
_CALLS: dict[str, list[float]] = {}


async def rate_wait(source: str) -> None:
    limit = RATE_LIMITS.get(source)
    if not limit:
        return
    n, period = limit
    q = _CALLS.setdefault(source, [])
    while True:
        now = time.monotonic()
        while q and now - q[0] > period:
            q.pop(0)
        if len(q) < n:
            q.append(now)
            return
        await asyncio.sleep(min(5.0, period - (now - q[0]) + 0.05))


async def request(client: httpx.AsyncClient, source: str, url: str, params: dict | None, secret: str | None,
                  retries: int = 2) -> httpx.Response:
    """GET 후 응답을 돌려준다. 연결 오류는 network, 5xx는 server, 401·403은 auth, 429는 rate_limit 로 바꾼다."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        await rate_wait(source)
        try:
            r = await client.get(url, params=params, timeout=TIMEOUT, headers=HEADERS)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = e
            if attempt < retries:
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            kind = "network"
            msg = "원천 서버에 연결하지 못했습니다(시간 초과 또는 네트워크 차단)." if isinstance(e, httpx.TimeoutException) \
                else "원천 서버에 연결하지 못했습니다(네트워크 오류)."
            raise SourceError(source, kind, msg) from None
        if r.status_code >= 500:
            last = None
            if attempt < retries:
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            raise SourceError(source, "server", f"원천 서버가 오류를 돌려주었습니다(HTTP {r.status_code}).", str(r.status_code))
        if r.status_code in (401, 403):
            raise SourceError(source, "auth", f"원천 서버가 인증·권한 문제로 거절했습니다(HTTP {r.status_code}).", str(r.status_code))
        if r.status_code == 429:
            wait = r.headers.get("Retry-After")
            raise SourceError(source, "rate_limit", "원천 서버 호출 한도를 넘었습니다(HTTP 429)."
                              + (f" {wait}초 뒤 다시 해 보세요." if wait else ""), "429")
        if r.status_code >= 400:
            raise SourceError(source, "bad_request", f"원천 서버가 요청을 거절했습니다(HTTP {r.status_code}).", str(r.status_code))
        return r
    raise SourceError(source, "network", f"원천 서버에 연결하지 못했습니다: {type(last).__name__}")


async def get_json(client: httpx.AsyncClient, source: str, url: str, params: dict | None, secret: str | None,
                   retries: int = 2):
    """GET 후 JSON을 돌려준다. 오류 메시지에 키가 섞이지 않게 가린다."""
    r = await request(client, source, url, params, secret, retries)
    text = r.text
    try:
        return json.loads(text)
    except ValueError:
        snippet = text[:200].replace(secret or "\0", "<KEY>")
        raise SourceError(source, "server", f"원천 서버 응답을 읽지 못했습니다: {snippet}") from None
