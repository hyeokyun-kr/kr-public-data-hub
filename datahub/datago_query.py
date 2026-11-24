"""공공데이터포털 목록을 색인에 넣고, 상세 화면을 만든다(상세는 저장된 목록 정보만 쓰고 원천을 부르지 않는다).

    python3 -m datahub.build_catalog --only datago     # 목록 다시 받기(CSV 약 150MB, 한 달에 한 번쯤)
"""
from __future__ import annotations

import json
import sqlite3
import zlib

import httpx

from . import catalog as cat
from .errors import SourceError
from .sources import datago

SOURCE_LABEL = "공공데이터포털(data.go.kr)"
DESC_IN_INDEX = 120            # 검색 색인에는 설명 앞부분만(메모리 절약). 전체는 datago_meta.sqlite
DROP = {"관리부서 전화번호"}     # 개인 연락처일 수 있어 저장하지 않는다
DETAIL_FIELDS = ["목록유형", "파일데이터명", "분류체계", "제공기관", "관리 부서명", "업데이트 주기", "차기 등록 예정일",
                 "전체행", "확장자(데이터포맷)", "제공형태", "API 유형", "신청가능 트래픽", "심의 유형", "등록일", "수정일",
                 "공간범위", "시간범위", "비용부과유무", "비용부과기준 및 단위", "이용허락범위", "데이터 한계", "기타 유의사항",
                 "보유근거", "수집방법", "매체유형", "다운로드_활용신청건수", "조회수", "국가중점여부", "표준데이터여부"]


def meta_path():
    """목록 한 행 전체(상세 화면용)는 색인과 따로, 압축해 둔다(약 10만 건 × 3KB → 색인 파일·시작 시간이 커지지 않게)."""
    return cat.CATALOG_DB.with_name("datago_meta.sqlite")


def meta_connect() -> sqlite3.Connection:
    con = sqlite3.connect(meta_path(), check_same_thread=False)
    con.execute("CREATE TABLE IF NOT EXISTS meta (ds_id TEXT PRIMARY KEY, body BLOB)")
    return con


def _year(s: str) -> int | None:
    s = (s or "").strip()
    return int(s[:4]) if s[:4].isdigit() else None


def _raw(v):
    """원천 문자열 그대로(앞뒤 공백도 유지). 비어 있거나 공백뿐이면 None."""
    return v if isinstance(v, str) and v.strip() else None


def to_dataset(r: dict) -> dict:
    key = (r.get("목록키") or "").strip()
    kind = (r.get("목록유형") or "").strip()
    return {
        "source": "DATAGO", "ds_id": key, "org_id": _raw(r.get("제공기관코드")),
        "org_name": _raw(r.get("제공기관")), "name": _raw(r.get("목록명")) or key,
        "path": _raw(r.get("분류체계")), "ranges": {}, "end_year": _year(r.get("수정일")),
        "unit": None, "description": (_raw(r.get("설명")) or "")[:DESC_IN_INDEX] or None,
        "keywords": _raw(r.get("키워드")),
        "url": (r.get("목록 URL") or "").strip() or datago.page_url(key, kind), "vw_cd": None, "stat_id": None,
        "origin": "datago_list",
        "extra": {"kind": kind, "fmt": (r.get("확장자(데이터포맷)") or "").strip(), "modified": (r.get("수정일") or "").strip(),
                  "cycle": (r.get("업데이트 주기") or "").strip(), "rows": (r.get("전체행") or "").strip(),
                  "portal_file": kind in ("FILE", "STD") and (r.get("제공형태") or "").strip() == datago.PORTAL_FILE},
    }


async def refresh(client: httpx.AsyncClient, con) -> dict:
    rows, info = await datago.catalog_rows(client)
    return store(con, rows, info)


def store(con, rows: list[dict], info: dict) -> dict:
    """받은 목록개방현황 행을 색인(datasets)과 상세(datago_meta.sqlite)에 넣는다."""
    con.execute("DELETE FROM datasets WHERE source='DATAGO'")
    now = cat.now_kst()
    cols = ["source", "ds_id", "org_id", "org_name", "name", "path", "ranges", "end_year", "unit", "description",
            "keywords", "url", "vw_cd", "stat_id", "origin", "updated_at", "extra"]
    batch, meta = [], []
    for r in rows:
        if not (r.get("목록키") or "").strip():
            continue
        d = to_dataset(r)
        d["ranges"] = "{}"
        d["extra"] = json.dumps(d["extra"], ensure_ascii=False)
        d["updated_at"] = now
        batch.append([d.get(c) for c in cols])
        body = json.dumps({k: v for k, v in r.items() if k not in DROP and k is not None}, ensure_ascii=False)
        meta.append((d["ds_id"], zlib.compress(body.encode("utf-8"), 6)))
    mcon = meta_connect()
    mcon.execute("DELETE FROM meta")
    mcon.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", meta)
    mcon.commit()
    mcon.execute("VACUUM")
    mcon.close()
    con.executemany(f"INSERT OR REPLACE INTO datasets ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", batch)
    con.execute("INSERT OR REPLACE INTO build_info VALUES ('datago_built_at', ?)", (now,))
    con.execute("INSERT OR REPLACE INTO build_info VALUES ('datago_catalog', ?)", (info.get("data_name") or "",))
    con.commit()
    return {"rows": len(batch), "catalog": info.get("data_name")}


def detail(con, key: str) -> dict:
    mcon = meta_connect()
    try:
        r = mcon.execute("SELECT body FROM meta WHERE ds_id=?", (key,)).fetchone()
    finally:
        mcon.close()
    if not r:
        raise SourceError("DATAGO", "not_found", f"공공데이터포털 목록 {key} 가 색인에 없습니다. 목록을 다시 받거나 원본 사이트에서 찾아 보세요.")
    m = json.loads(zlib.decompress(r[0]).decode("utf-8"))
    kind = (m.get("목록유형") or "").strip()
    portal_file = kind in ("FILE", "STD") and (m.get("제공형태") or "").strip() == datago.PORTAL_FILE
    built = con.execute("SELECT v FROM build_info WHERE k='datago_catalog'").fetchone()
    notes = []
    if kind == "API":
        notes.append("오픈API는 공공데이터포털에 로그인해 이 API를 '활용신청'한 뒤 발급 키로 부를 수 있다. 이 프로그램은 부르지 않는다.")
    elif not portal_file:
        notes.append(f"제공형태가 '{(m.get('제공형태') or '정보 없음').strip()}'라 이 프로그램에서 바로 받을 수 없다. 원본 페이지에서 받는다.")
    else:
        notes.append("'파일 받기'는 포털에 올라간 원본 파일을 그대로 받는다(내용을 고치지 않음). 포털이 보안 확인(캡차)을 요구하면 원본 페이지에서 받는다.")
    return {                         # 표시 문자열은 원천 그대로(codex 11차: 공백도 바꾸지 않음)
        "source": "DATAGO", "id": key, "org_name": _raw(m.get("제공기관")) or "", "name": _raw(m.get("목록명")) or key,
        "path": _raw(m.get("분류체계")) or "", "unit": None,
        "url": (m.get("목록 URL") or "").strip() or datago.page_url(key, kind), "dims": [], "cycles": [],
        "info": {"설명": _raw(m.get("설명")) or "", "키워드": _raw(m.get("키워드")) or ""},
        "fields": [(f, m[f]) for f in DETAIL_FIELDS if isinstance(m.get(f), str) and m[f] != ""],   # 공백뿐인 값도 원천 그대로
        "notes": notes, "warnings": [], "source_label": SOURCE_LABEL,
        "ids": {"목록키": key}, "kind": kind, "kind_label": datago.KIND_LABEL.get(kind, kind), "portal_file": portal_file,
        "catalog": built[0] if built else None,
    }
