"""금융감독원 OpenDART(전자공시) Open API.

쓰는 서비스 (https://opendart.fss.or.kr/api/{서비스}?crtfc_key=...)
- corpCode.xml           회사 고유번호 전체 목록(zip 안의 XML, 약 12만 개). 회사명 검색은 이 목록으로 PC 안에서만 한다.
- company.json           기업개황(대표자·법인구분·업종코드·결산월 등)
- fnlttSinglAcntAll.json 단일회사 전체 재무제표(2015년 이후, 사업·반기·분기보고서, 연결/별도)
- fnlttMultiAcnt.json    다중회사 주요계정(한 번에 최대 100개 회사)
- list.json              공시 검색(회사·기간별 공시 목록)
응답 status: 000 정상, 013 결과 없음, 020 요청 한도 초과 등(errors.DART_CODES).
"""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile

import httpx

from ..config import get_api_key
from ..errors import DART_CODES, SourceError
from . import get_json, limiter, request

BASE = "https://opendart.fss.or.kr/api"
SOURCE = "DART"
REPRT = {"11011": "사업보고서", "11012": "반기보고서", "11013": "1분기보고서", "11014": "3분기보고서"}
FS_DIV = {"CFS": "연결재무제표", "OFS": "별도(개별)재무제표"}
CORP_CLS = {"Y": "유가증권시장", "K": "코스닥", "N": "코넥스", "E": "기타(비상장 등)"}
MULTI_MAX = 100


def _key() -> str:
    k = get_api_key(SOURCE)
    if not k:
        raise SourceError(SOURCE, "auth", "OpenDART 인증키가 없습니다. README의 '시작하기'를 보세요.")
    return k


def masked(service: str, params: dict) -> str:
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{BASE}/{service}?crtfc_key={{OPENDART_API_KEY}}" + (f"&{q}" if q else "")


def _check(data: dict, service: str) -> dict:
    code = str(data.get("status", ""))
    if code == "000":
        return data
    msg = str(data.get("message", "")).strip()
    kind = DART_CODES.get(code, "server")
    raise SourceError(SOURCE, kind, msg or f"OpenDART 오류({service})", code)


async def call(client: httpx.AsyncClient, service: str, params: dict, log: list | None = None) -> dict:
    key = _key()
    if log is not None:
        log.append(masked(service, params))
    async with limiter(SOURCE):
        data = await get_json(client, SOURCE, f"{BASE}/{service}", {"crtfc_key": key, **params}, key)
    if not isinstance(data, dict):
        raise SourceError(SOURCE, "server", f"예상하지 못한 응답 형식입니다({service}).")
    return _check(data, service)


async def corp_codes(client: httpx.AsyncClient, log: list | None = None) -> list[dict]:
    """회사 고유번호 전체 목록. 키 말고는 아무것도 보내지 않는다."""
    key = _key()
    if log is not None:
        log.append(masked("corpCode.xml", {}))
    async with limiter(SOURCE):
        r = await request(client, SOURCE, f"{BASE}/corpCode.xml", {"crtfc_key": key}, key)
    body = r.content
    if not body.startswith(b"PK"):          # 오류면 zip 대신 <result><status>..</status> XML(또는 JSON)이 온다
        try:
            root = ET.fromstring(body)
            _check({"status": root.findtext("status"), "message": root.findtext("message")}, "corpCode.xml")
        except ET.ParseError:
            pass
        raise SourceError(SOURCE, "server", "회사 목록 응답을 읽지 못했습니다(zip이 아님).")
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
        root = ET.fromstring(z.read(z.namelist()[0]))
    except (zipfile.BadZipFile, ET.ParseError, IndexError) as e:
        raise SourceError(SOURCE, "server", f"회사 목록 파일을 읽지 못했습니다({type(e).__name__}).") from None
    rows = []
    for e in root.iter("list"):
        d = {ch.tag: (ch.text or "").strip() for ch in e}
        if d.get("corp_code"):
            rows.append(d)
    if not rows:
        raise SourceError(SOURCE, "server", "회사 목록이 비어 있습니다.")
    return rows


async def company(client: httpx.AsyncClient, corp_code: str, log: list | None = None) -> dict:
    return await call(client, "company.json", {"corp_code": corp_code}, log)


async def fin_all(client: httpx.AsyncClient, corp_code: str, year: int | str, reprt: str, fs_div: str,
                  log: list | None = None) -> dict:
    """단일회사 전체 재무제표. 결과 없음(013)은 SourceError(no_data)."""
    return await call(client, "fnlttSinglAcntAll.json",
                      {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt, "fs_div": fs_div}, log)


async def multi(client: httpx.AsyncClient, corp_codes_: list[str], year: int | str, reprt: str,
                log: list | None = None) -> list[dict]:
    if len(corp_codes_) > MULTI_MAX:
        raise SourceError(SOURCE, "too_large", f"다중회사 조회는 한 번에 {MULTI_MAX}개 회사까지입니다.")
    data = await call(client, "fnlttMultiAcnt.json",
                      {"corp_code": ",".join(corp_codes_), "bsns_year": str(year), "reprt_code": reprt}, log)
    return data.get("list") or []


async def disclosures(client: httpx.AsyncClient, corp_code: str, bgn_de: str, end_de: str, max_pages: int = 10,
                      log: list | None = None, notes: list | None = None) -> list[dict]:
    """공시 목록(최신순). 100건씩 최대 max_pages 쪽. 다 받지 못하면 notes 에 (전체, 받은 수)."""
    out: list[dict] = []
    page, total = 1, None
    while True:
        try:
            data = await call(client, "list.json", {"corp_code": corp_code, "bgn_de": bgn_de, "end_de": end_de,
                                                    "page_no": str(page), "page_count": "100"}, log)
        except SourceError as e:
            if e.kind == "no_data" and out:
                break
            raise
        rows = data.get("list") or []
        out.extend(rows)
        total = int(data.get("total_count") or len(out))
        total_page = int(data.get("total_page") or 1)
        if page >= total_page or not rows:
            break
        if page >= max_pages:
            break
        page += 1
    if notes is not None and total is not None and total != len(out):
        notes.append((total, len(out)))
    return out
