"""공공데이터포털(data.go.kr) 목록과 파일 받기.

- 목록: 포털이 매달 올리는 '공공데이터활용지원센터_공공데이터포털 목록개방현황'(파일데이터 15062804, CSV 약 10만 행).
  파일·오픈API·표준데이터 목록의 이름·제공기관·분류·설명·키워드·수정일·형식·이용허락범위 등이 들어 있다.
- 파일 받기: 포털 화면이 쓰는 순서 그대로 — 목록 페이지에서 파일 키 확인 → selectFileDataDownload.do → check-limit.json
  → fileDownload.do. 포털이 보안 확인(캡차)을 요구하면 받지 않고 원본 페이지로 안내한다(우회하지 않음).
- 인증키가 필요 없다(로그인 없이 받을 수 있는 공개 파일만). 오픈API는 API마다 포털 '활용신청'이 따로 필요해 여기서 부르지 않는다.
"""
from __future__ import annotations

import csv
import io
import re
from urllib.parse import quote

import httpx

from ..errors import SourceError
from . import HEADERS, TIMEOUT, rate_wait

BASE = "https://www.data.go.kr"
SOURCE = "DATAGO"
CATALOG_KEY = "15062804"
KIND_LABEL = {"FILE": "파일데이터", "API": "오픈API", "STD": "표준데이터"}
PORTAL_FILE = "공공데이터포털에서 다운로드(원문파일등록)"
_DOWN = re.compile(r"fn_fileDataDown\(\s*'(\d+)'\s*,\s*'([^']+)'\s*,\s*'([^']*)'\s*,\s*'(\d+)'\s*,\s*'(\d+)'\s*\)")


def page_url(key: str, kind: str = "FILE") -> str:
    return f"{BASE}/data/{key}/{'openapi' if kind == 'API' else 'fileData'}.do"


async def _send(client: httpx.AsyncClient, method: str, url: str, data: dict | None = None) -> httpx.Response:
    await rate_wait(SOURCE)
    try:
        r = await client.request(method, url, data=data, timeout=TIMEOUT, headers=HEADERS)
    except (httpx.TimeoutException, httpx.TransportError):
        raise SourceError(SOURCE, "network", "공공데이터포털에 연결하지 못했습니다(시간 초과 또는 네트워크 차단).") from None
    if r.status_code in (403, 429):
        raise SourceError(SOURCE, "rate_limit", "공공데이터포털이 요청을 막았습니다(다운로드 한도·보안 확인). 원본 페이지에서 받으세요.",
                          str(r.status_code))
    if r.status_code >= 500:
        raise SourceError(SOURCE, "server", f"공공데이터포털 서버 오류(HTTP {r.status_code}).", str(r.status_code))
    if r.status_code >= 400:
        raise SourceError(SOURCE, "not_found" if r.status_code == 404 else "bad_request",
                          f"공공데이터포털이 요청을 거절했습니다(HTTP {r.status_code}).", str(r.status_code))
    return r


async def file_info(client: httpx.AsyncClient, key: str, log: list | None = None) -> dict:
    """파일데이터의 내려받기 정보(첨부파일 ID·원래 파일 이름·확장자·행 수). 포털에 올린 파일이 아니면 SourceError."""
    if not re.fullmatch(r"\d{5,10}", key or ""):
        raise SourceError(SOURCE, "bad_request", "공공데이터포털 목록키는 숫자입니다.")
    url = page_url(key)
    if log is not None:
        log.append(url)
    page = (await _send(client, "GET", url)).text
    m = _DOWN.search(page)
    if not m:
        if key not in page:          # 목록 화면이 아니라 오류 화면을 받은 경우(codex 11차)
            raise SourceError(SOURCE, "server", "공공데이터포털 목록 화면을 읽지 못했습니다. 잠시 뒤 다시 하거나 원본 페이지를 여세요.")
        raise SourceError(SOURCE, "not_found", "이 목록은 포털에서 바로 받는 파일이 아닙니다(기관 사이트 링크·API 등). 원본 페이지를 여세요.")
    pk, detail_pk, atch, sn, _hist = m.groups()
    form = {"publicDataDetailPk": detail_pk, "publicDataPk": pk, "atchFileId": atch, "fileDetailSn": sn,
            "publicDataTyCode": "PR0051"}
    if log is not None:
        log.append(f"{BASE}/tcs/dss/selectFileDataDownload.do (publicDataPk={pk})")
    r = await _send(client, "POST", f"{BASE}/tcs/dss/selectFileDataDownload.do", form)
    try:
        j = r.json()
    except ValueError:
        raise SourceError(SOURCE, "server", "공공데이터포털 응답을 읽지 못했습니다(파일 정보).") from None
    if not j.get("status") or not j.get("atchFileId"):
        raise SourceError(SOURCE, "not_found", str(j.get("error") or "공공데이터포털이 파일 정보를 주지 않았습니다."))
    info = j.get("dataSetFileDetailInfo") or {}
    return {"key": pk, "atchFileId": j["atchFileId"], "fileDetailSn": str(j.get("fileDetailSn") or sn),
            "data_name": info.get("dataNm") or info.get("publicDataSj") or pk,
            "file_name": info.get("orginlFileNm") or "", "ext": (info.get("atchFileExtsn") or "").lower(),
            "rows": info.get("atchFileCo"), "registered": info.get("registDt"), "page": url}


async def check_limit(client: httpx.AsyncClient, info: dict) -> None:
    r = await _send(client, "POST", f"{BASE}/cmm/cmm/check-limit.json",
                    {"atchFileId": info["atchFileId"], "fileDetailSn": info["fileDetailSn"]})
    # 보안 확인이 필요 없다는 답(JSON needCaptcha=false)을 분명히 받았을 때만 받는다. 모르면 멈춘다(codex 11차)
    try:
        j = r.json()
    except ValueError:
        j = None
    if not isinstance(j, dict) or not isinstance(j.get("needCaptcha"), bool):
        raise SourceError(SOURCE, "server", "공공데이터포털의 다운로드 한도 확인 응답을 읽지 못해 받지 않았습니다. 원본 페이지에서 받으세요.")
    if j["needCaptcha"]:
        raise SourceError(SOURCE, "rate_limit", "공공데이터포털이 보안 확인(캡차)을 요구합니다. 원본 페이지에서 직접 받으세요.", "captcha")


def download_url(info: dict) -> str:
    return (f"{BASE}/cmm/cmm/fileDownload.do?atchFileId={quote(info['atchFileId'])}&fileDetailSn={quote(info['fileDetailSn'])}"
            f"&dataNm={quote(info['data_name'])}")


async def catalog_rows(client: httpx.AsyncClient, log: list | None = None) -> tuple[list[dict], dict]:
    """목록개방현황 CSV를 받아 행 목록과 파일 정보를 돌려준다."""
    info = await file_info(client, CATALOG_KEY, log)
    await check_limit(client, info)
    if log is not None:
        log.append(download_url(info))
    r = await _send(client, "GET", download_url(info))
    raw = r.content
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SourceError(SOURCE, "server", "공공데이터포털 목록 파일의 글자 인코딩을 읽지 못했습니다.")
    rows = list(csv.DictReader(io.StringIO(text, newline="")))
    if len(rows) < 1000 or "목록키" not in rows[0]:
        raise SourceError(SOURCE, "server", f"공공데이터포털 목록 파일 모양이 예상과 다릅니다({len(rows)}행).")
    return rows, info
