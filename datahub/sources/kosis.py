"""KOSIS 국가통계포털 공유서비스(Open API).

쓰는 서비스
- statisticsSearch.do            통합검색(통계표 단위: 기관·통계표ID·수록기간·설명)
- statisticsList.do              통계목록(주제별 트리)
- statisticsData.do?method=getMeta  통계표 메타(TBL 표명, PRD 수록주기·기간, ITM 항목·분류, CMMT 주석,
                                    SOURCE 조사명·담당, NCD 최종 수정일)
- Param/statisticsParameterData.do  자료 조회(itmId·objL1~8 은 '+'로 여러 개, 'ALL' = 전체)
- statisticsExplData.do          통계설명(작성목적 등)
모든 호출에 format=json&jsonVD=Y (jsonVD 없으면 표준 JSON이 아님)
"""
from __future__ import annotations

import asyncio
from urllib.parse import quote, urlencode

import httpx

from ..config import get_api_key
from ..errors import KOSIS_CODES, SourceError
from . import get_json, limiter

BASE = "https://kosis.kr/openapi"
SOURCE = "KOSIS"
CELL_LIMIT = 40000          # KOSIS 한 번 조회 최대 셀 수


def _key() -> str:
    k = get_api_key(SOURCE)
    if not k:
        raise SourceError(SOURCE, "auth", "KOSIS 인증키가 없습니다. README의 '시작하기'를 보세요.")
    return k


# KOSIS는 코드 속 '*'를 %2A로 보내면 '잘못된 요청 변수'(21)로 거절한다(예: 301/DT_404Y016 의
# '13102134764ACC_CD.*AA'). 그래서 '*'와 구분자 '+'는 그대로 두고 나머지만 인코딩해 주소를 직접 만든다.
def query_string(params: dict) -> str:
    return urlencode(params, quote_via=quote, safe="*+")


def masked(url: str, params: dict) -> str:
    p = dict(params)
    p["apiKey"] = "{KOSIS_API_KEY}"
    return url + "?" + query_string(p).replace("%7BKOSIS_API_KEY%7D", "{KOSIS_API_KEY}")


async def _call(client: httpx.AsyncClient, path: str, params: dict, log: list | None = None):
    key = _key()
    p = {**params, "apiKey": key, "format": "json", "jsonVD": "Y"}
    url = f"{BASE}/{path}"
    if log is not None:
        log.append(masked(url, p))
    async with limiter(SOURCE):
        data = await get_json(client, SOURCE, url + "?" + query_string(p), None, key)
    if isinstance(data, dict) and "err" in data:
        code = str(data.get("err"))
        msg = str(data.get("errMsg", "")).strip()
        raise SourceError(SOURCE, KOSIS_CODES.get(code, "server"), msg or f"오류 {code}", code)
    if not isinstance(data, list):
        raise SourceError(SOURCE, "server", "예상하지 못한 응답 형식입니다.")
    return data


async def search(client: httpx.AsyncClient, term: str, count: int = 50) -> list[dict]:
    try:
        return await _call(client, "statisticsSearch.do",
                           {"method": "getList", "searchNm": term, "orgId": "", "sort": "RANK",
                            "startCount": 1, "resultCount": count})
    except SourceError as e:
        if e.kind == "no_data":
            return []
        raise


async def list_children(client: httpx.AsyncClient, vw_cd: str, parent_id: str) -> list[dict]:
    try:
        return await _call(client, "statisticsList.do", {"method": "getList", "vwCd": vw_cd, "parentListId": parent_id})
    except SourceError as e:
        if e.kind == "no_data":
            return []
        raise


async def meta(client: httpx.AsyncClient, org_id: str, tbl_id: str, kind: str) -> list[dict]:
    try:
        return await _call(client, "statisticsData.do", {"method": "getMeta", "type": kind, "orgId": org_id, "tblId": tbl_id})
    except SourceError as e:
        if e.kind == "no_data":
            return []
        raise


async def data(client: httpx.AsyncClient, org_id: str, tbl_id: str, itm_ids: list[str] | str,
               obj_levels: list[list[str] | str], prd_se: str, start: str, end: str,
               log: list | None = None) -> list[dict]:
    """obj_levels: 분류 차원 순서(OBJ_ID_SN)대로 코드 목록 또는 'ALL'. 결과 없음은 빈 목록."""
    def join(v):
        return v if isinstance(v, str) else "+".join(v) + "+"

    params = {"method": "getList", "orgId": org_id, "tblId": tbl_id, "itmId": join(itm_ids),
              "prdSe": prd_se, "startPrdDe": start, "endPrdDe": end}
    for i, lv in enumerate(obj_levels, start=1):
        params[f"objL{i}"] = join(lv)
    try:
        return await _call(client, "Param/statisticsParameterData.do", params, log=log)
    except SourceError as e:
        if e.kind == "no_data":
            return []
        raise


async def explanation(client: httpx.AsyncClient, stat_id: str) -> dict | None:
    try:
        rows = await _call(client, "statisticsExplData.do", {"method": "getList", "statId": stat_id, "metaItm": "All"})
    except SourceError:
        return None
    return rows[0] if rows else None
