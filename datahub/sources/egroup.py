"""공정거래위원회 기업집단포털 OpenAPI(공공데이터포털, https://apis.data.go.kr/1130000/...).

쓰는 서비스(모두 XML, 키 이름은 서비스마다 ServiceKey 또는 serviceKey)
- publicYmList/publicYmListApi                    사용 가능 공개년월(jobSeCode 0001 지정, 0003 지주)
- appnGroupSttusList/appnGroupSttusListApi        지정된 대규모기업집단(presentnYear=공개년월)
- appnGroupAssetsList/appnGroupAssetsListApi      지정 집단 자산총액 순위(단위 10억원)
- appnGroupAffiList/appnGroupAffiListApi          지정 집단 소속회사(법인등록번호·사업자번호·대표자·설립일·편입일)
- tyAssetsRentDelngDtlsList/tyAssetsRentDelngDtlsListApi   계열 편입·제외·유예 변경내역(전체 기간)
키: 공공데이터포털 서비스키(DATA_GO_KR_SERVICE_KEY). 포털에서 위 서비스들을 활용신청해야 쓸 수 있다.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from ..config import get_api_key
from ..errors import SourceError
from . import limiter, request

BASE = "https://apis.data.go.kr/1130000"
SOURCE = "EGROUP"
PAGE = 1000
# 서비스마다 키 인자 이름이 다르다(포털 명세 그대로)
KEY_PARAM = {"publicYmList": "ServiceKey", "appnGroupSttusList": "ServiceKey", "appnGroupAssetsList": "ServiceKey",
             "appnGroupAffiList": "ServiceKey", "tyAssetsRentDelngDtlsList": "serviceKey"}
META = {"numOfRows", "pageNo", "resultCode", "resultMsg", "totalCount"}
CHANGE = {"0001": "편입", "0002": "제외", "0003": "유예"}
INCRPR = {"0001": "회사설립", "0002": "지분취득", "0003": "기타"}
EXCL = {"0001": "흡수합병", "0002": "지분매각", "0003": "청산종결", "0004": "기타"}
EXCL_JOB = {"0001": "계열유예", "0002": "독립경영", "0009": "해당없음"}
POSTPONE = {"0001": "산학연협력기술지주회사", "0002": "산학연협력기술지주회사의 자회사", "0003": "신기술창업전문회사",
            "0004": "신기술창업전문회사의 자회사", "0005": "R&D중소기업", "0006": "벤처기업", "0007": "민자SPC"}


def _key() -> str:
    k = get_api_key("PUBLIC")
    if not k:
        raise SourceError(SOURCE, "auth", "공공데이터포털 서비스키가 없습니다(DATA_GO_KR_SERVICE_KEY). README의 '시작하기'를 보세요.")
    return k


def masked(svc: str, op: str, params: dict) -> str:
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{BASE}/{svc}/{op}?{KEY_PARAM[svc]}={{DATA_GO_KR_SERVICE_KEY}}" + (f"&{q}" if q else "")


def parse(body: bytes, svc: str) -> tuple[int, list[dict]]:
    """XML → (전체 건수, 행 목록). 포털 공통 오류(OpenAPI_ServiceResponse)와 결과코드를 SourceError로."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise SourceError(SOURCE, "server", "기업집단포털 응답을 읽지 못했습니다(XML 아님).") from None
    if root.tag == "OpenAPI_ServiceResponse":       # 포털 게이트웨이 오류(키 미등록·한도 초과 등)
        msg = root.findtext(".//errMsg") or root.findtext(".//returnAuthMsg") or "공공데이터포털 오류"
        reason = root.findtext(".//returnReasonCode") or ""
        kind = "rate_limit" if reason in ("22", "23") or "LIMITED" in msg else \
            "auth" if reason in ("20", "30", "31", "32", "33") or "KEY" in msg else "server"
        raise SourceError(SOURCE, kind, f"공공데이터포털: {msg}", reason)
    code = root.findtext("resultCode") or ""
    if code not in ("00", "0", "03"):
        msg = (root.findtext("resultMsg") or "").strip()
        kind = "bad_request" if code in ("10", "11", "97") else "server"
        raise SourceError(SOURCE, kind, f"기업집단포털 오류({code}): {msg}", code)
    rows = [{ch.tag: (ch.text or "") for ch in item} for item in root if item.tag not in META]
    try:
        total = int(root.findtext("totalCount") or len(rows))
    except ValueError:
        total = len(rows)
    return total, rows


async def call(client: httpx.AsyncClient, svc: str, page: int = 1, size: int = PAGE, log: list | None = None, **params) -> tuple[int, list[dict]]:
    key = _key()
    p = {"pageNo": str(page), "numOfRows": str(size), **{k: v for k, v in params.items() if v not in (None, "")}}
    if log is not None:
        log.append(masked(svc, f"{svc}Api", p))
    async with limiter(SOURCE):
        r = await request(client, SOURCE, f"{BASE}/{svc}/{svc}Api", {KEY_PARAM[svc]: key, **p}, key)
    return parse(r.content, svc)


async def fetch_all(client: httpx.AsyncClient, svc: str, log: list | None = None, notes: list | None = None, **params) -> list[dict]:
    """모든 쪽을 차례로 받는다. 알린 건수와 받은 건수가 다르면 notes 에 (알림, 받음)."""
    total, rows = await call(client, svc, 1, PAGE, log, **params)
    page = 1
    while len(rows) < total:
        page += 1
        _, more = await call(client, svc, page, PAGE, log, **params)
        if not more:
            break
        rows += more
    if notes is not None and len(rows) != total:
        notes.append((total, len(rows)))
    return rows
