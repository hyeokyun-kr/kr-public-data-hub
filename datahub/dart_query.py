"""OpenDART 회사 검색·기업개황·재무제표·공시 목록. 결과는 query.fetch 와 같은 모양(exporter 그대로 사용).

회사명 검색은 회사 목록(corpCode.xml 전체)을 PC 안 SQLite(dart_corps)에 두고 여기서만 한다.
원천으로 나가는 것은 회사 고유번호(8자리)와 연도·보고서 종류뿐이다.

조회 조건(spec)
    재무제표  {"source": "DART", "kind": "fs", "id": "00126380", "fs_div": "CFS", "reprt": "11011",
              "start": "2019", "end": "2025", "flow": "cum", "sel": {"SJ": ["BS", "IS", "CIS", "CF"]}}
    공시 목록 {"source": "DART", "kind": "list", "id": "00126380", "start": "2026-01-01", "end": "2026-11-13"}
값은 OpenDART가 준 문자열 그대로(원 단위) 보관한다. 각 연도 값은 그 연도 보고서의 '당기' 값이다(뒤 연도 보고서의 재작성 값으로 바꾸지 않음).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import re
import time
import unicodedata

import httpx

from . import catalog as cat
from . import ksic
from .config import VERSION
from .errors import SourceError
from .sources import dart

SOURCE_LABEL = "금융감독원 전자공시시스템 OpenDART"
CORP_TTL = 7 * 24 * 3600          # 회사 목록은 일주일에 한 번 새로 받는다
COMPANY_TTL = 24 * 3600
FIRST_YEAR = 2015                 # 전체 재무제표 API가 주는 첫 사업연도
SJ_ORDER = ["BS", "IS", "CIS", "CF", "SCE"]
SJ_LABEL = {"BS": "재무상태표", "IS": "손익계산서", "CIS": "포괄손익계산서", "CF": "현금흐름표", "SCE": "자본변동표"}
SJ_DEFAULT = ["BS", "IS", "CIS", "CF"]
REPRT_SUFFIX = {"11011": "", "11013": "-Q1", "11012": "-H1", "11014": "-Q3"}
REPRT_LABEL = {"11011": "연(사업보고서)", "11013": "1분기보고서", "11012": "반기보고서", "11014": "3분기보고서"}
MAX_YEARS = 12
MAX_LIST_PAGES = 20

# 상장사: 종목코드가 있고 법인구분이 유가(Y)·코스닥(K)·코넥스(N). 회사 목록(corpCode)은 상장폐지 회사에도 옛 종목코드를
# 남겨 두므로(기업개황 법인구분 E), 법인구분을 모르는 동안만 종목코드로 판단한다.
LISTED = "(stock_code<>'' AND (corp_cls IS NULL OR corp_cls IN ('Y','K','N')))"
LISTED_KNOWN = "(stock_code<>'' AND corp_cls IN ('Y','K','N'))"

_NAME_STRIP = re.compile(r"\(주\)|㈜|주식회사|\(유\)|유한회사|\(합\)|\s+")
# 계정 이름 맞추기: 주석 번호 '(주27)'·'(주석 5,6)', 앞 번호 'Ⅰ.'·'1.'·'(1)', 공백을 지운다
_ACC_NOTE = re.compile(r"\((?:주석?|note)\s*[\d,.\s-]*\)", re.I)
_ACC_LEAD = re.compile(r"^(?:[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+|[IVX]+|\d+|[가나다라마바사아자차카타파하])[.)]\s*|^\(\d+\)\s*")


_ACC_SIGN = re.compile(r"\((?:손실|결손금|\*\d*)\)")   # '(수익)'·'(비용)'·'(이익)'은 뜻이 반대일 수 있어 남긴다(codex 7차)


def norm_acc(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = _ACC_NOTE.sub("", s)
    s = _ACC_SIGN.sub("", s)          # '당기순이익(손실)' = '당기순이익', '총포괄손익(*3)' = '총포괄손익'

    s = _ACC_LEAD.sub("", s.strip())
    return re.sub(r"\s+", "", s)


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    return _NAME_STRIP.sub("", s)


# ------------------------------------------------------------------ 회사 목록
def corps_info(con) -> dict:
    n = con.execute(f"SELECT COUNT(*), SUM({LISTED}) FROM dart_corps").fetchone()
    at = con.execute("SELECT v FROM build_info WHERE k='dart_corps_at'").fetchone()
    return {"count": n[0] or 0, "listed": n[1] or 0, "at": at[0] if at else None}


def corps_stale(con) -> bool:
    r = con.execute("SELECT v FROM build_info WHERE k='dart_corps_ts'").fetchone()
    return not r or time.time() - float(r[0]) > CORP_TTL


async def refresh_corps(client: httpx.AsyncClient, con) -> int:
    rows = await dart.corp_codes(client)
    known = {r[0]: (r[1], r[2]) for r in con.execute("SELECT corp_code, induty_code, corp_cls FROM dart_corps")}
    con.execute("DELETE FROM dart_corps")
    con.executemany(
        "INSERT OR REPLACE INTO dart_corps VALUES (?,?,?,?,?,?,?,?)",
        [(r["corp_code"], r.get("corp_name", ""), r.get("corp_eng_name", ""), r.get("stock_code", "").strip(),
          r.get("modify_date", ""), norm_name(r.get("corp_name", "")),
          *(known.get(r["corp_code"]) or (None, None))) for r in rows])
    con.execute("INSERT OR REPLACE INTO build_info VALUES ('dart_corps_at', ?)", (cat.now_kst(),))
    con.execute("INSERT OR REPLACE INTO build_info VALUES ('dart_corps_ts', ?)", (str(time.time()),))
    con.commit()
    return len(rows)


def search_corps(con, q: str, limit: int = 60, listed_only: bool = False) -> dict:
    raw = unicodedata.normalize("NFKC", q or "").strip()
    nq = norm_name(raw)
    if not nq:
        return {"count": 0, "results": []}
    def like(t: str) -> str:          # 검색어 속 %·_ 는 글자 그대로
        return "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    params: list = []
    conds = ["name_norm LIKE ? ESCAPE '\\'"]
    params.append(like(nq))
    if re.fullmatch(r"[0-9A-Za-z]{6}", raw) and re.search(r"\d", raw):     # 종목코드(영문이 섞인 새 코드 포함, 예: 0068Y0)
        conds.append("UPPER(stock_code) = ?"); params.append(raw.upper())
    if re.fullmatch(r"\d{8}", raw):
        conds.append("corp_code = ?"); params.append(raw)
    if re.fullmatch(r"[a-z0-9 .,&()-]+", raw.lower()) and len(raw) >= 2:
        conds.append("LOWER(corp_eng_name) LIKE ? ESCAPE '\\'"); params.append(like(raw.lower()))
    where = f"({' OR '.join(conds)})" + (f" AND {LISTED}" if listed_only else "")
    rows = con.execute(f"SELECT * FROM dart_corps WHERE {where}", params).fetchall()

    def rank(r):
        n = r["name_norm"]
        exact = n == nq or r["stock_code"].upper() == raw.upper() or r["corp_code"] == raw
        listed = r["stock_code"] != "" and r["corp_cls"] != "E"
        return (not exact, not listed, not n.startswith(nq), len(n), r["corp_name"])

    rows = sorted(rows, key=rank)
    return {"count": len(rows), "results": [corp_dict(r) for r in rows[:limit]]}


def corp_dict(r) -> dict:
    return {"corp_code": r["corp_code"], "name": r["corp_name"], "eng_name": r["corp_eng_name"],
            "stock_code": r["stock_code"], "listed": bool(r["stock_code"]) and r["corp_cls"] not in ("E",),
            "modify_date": r["modify_date"],
            "induty_code": r["induty_code"], "corp_cls": r["corp_cls"],
            "market": dart.CORP_CLS.get(r["corp_cls"] or "", "")}


def get_corp(con, corp_code: str):
    return con.execute("SELECT * FROM dart_corps WHERE corp_code=?", (corp_code,)).fetchone()


# ------------------------------------------------------------------ 기업개황
async def company(client: httpx.AsyncClient, con, corp_code: str) -> dict:
    if not re.fullmatch(r"\d{8}", corp_code or ""):
        raise SourceError("DART", "bad_request", "회사 고유번호는 숫자 8자리입니다.")
    r = con.execute("SELECT fetched_at, body FROM meta_cache WHERE source='DART' AND ds_id=?", (corp_code,)).fetchone()
    if r and time.time() - float(r[0]) < COMPANY_TTL:
        return json.loads(r[1])
    try:
        c = await dart.company(client, corp_code)
    except SourceError as e:
        if e.kind == "no_data":
            raise SourceError("DART", "not_found", f"OpenDART에 회사 고유번호 {corp_code} 가 없습니다. 회사를 다시 검색해 보세요.", e.code) from None
        raise
    est = c.get("est_dt") or ""
    body = {
        "corp_code": corp_code, "name": c.get("corp_name"), "stock_name": c.get("stock_name"),
        "eng_name": c.get("corp_name_eng"), "stock_code": (c.get("stock_code") or "").strip(),
        "corp_cls": c.get("corp_cls"), "market": dart.CORP_CLS.get(c.get("corp_cls") or "", ""),
        "ceo": c.get("ceo_nm"), "induty_code": c.get("induty_code"),
        "est_dt": f"{est[:4]}-{est[4:6]}-{est[6:8]}" if len(est) == 8 else est,
        "acc_mt": c.get("acc_mt"), "hm_url": c.get("hm_url"),
        "jurir_no": (c.get("jurir_no") or "").strip(), "bizr_no": (c.get("bizr_no") or "").strip(),   # 기업집단포털과 잇는 열쇠
        "url": None,          # DART 회사 화면은 주소로 바로 열 수 없다(검색 양식 POST). 결과마다 보고서 원문 주소를 둔다
        "source_label": SOURCE_LABEL, "fetched_at": cat.now_kst(),
    }
    con.execute("INSERT OR REPLACE INTO meta_cache VALUES ('DART', ?, ?, ?)",
                (corp_code, str(time.time()), json.dumps(body, ensure_ascii=False)))
    con.execute("UPDATE dart_corps SET induty_code=?, corp_cls=? WHERE corp_code=?",
                (body["induty_code"], body["corp_cls"], corp_code))
    save_jurir(con, corp_code, body.get("jurir_no"))
    con.commit()
    return body


# ------------------------------------------------------------------ 재무제표
def _years(spec: dict) -> list[int]:
    try:
        a, b = int(str(spec.get("start")).strip()[:4]), int(str(spec.get("end")).strip()[:4])
    except (TypeError, ValueError):
        raise SourceError("DART", "bad_period", "사업연도는 2019처럼 네 자리 연도로 넣어 주세요.") from None
    if a > b:
        a, b = b, a
    this_year = _dt.date.today().year
    if b < FIRST_YEAR or a > this_year:
        raise SourceError("DART", "bad_period", f"전체 재무제표는 {FIRST_YEAR}년부터 {this_year}년 사이에서 고를 수 있습니다.")
    a, b = max(a, FIRST_YEAR), min(b, this_year)
    if b - a + 1 > MAX_YEARS:
        raise SourceError("DART", "too_large", f"한 번에 {MAX_YEARS}개 연도까지 받을 수 있습니다.")
    return list(range(a, b + 1))


def key_rows(rows: list[dict]) -> list[dict]:
    """주요계정 API 행 정리: 금액의 천 단위 쉼표만 빼고(값은 그대로), 같은 표·이름·금액이 두 번 오면 하나만 둔다
    (손익계산서 '당기순이익(손실)'이 포괄손익 부분에서 같은 금액으로 한 번 더 온다)."""
    out, seen = [], set()
    for r in rows:
        r = dict(r)
        for k in ("thstrm_amount", "thstrm_add_amount", "frmtrm_amount", "frmtrm_add_amount", "bfefrmtrm_amount"):
            if isinstance(r.get(k), str):
                r[k] = r[k].replace(",", "")          # 천 단위 쉼표만 뺀다(그 밖의 글자는 그대로)
        sig = (r.get("sj_div"), r.get("account_nm"), r.get("thstrm_amount"), r.get("thstrm_add_amount"))
        if sig in seen:
            continue
        seen.add(sig)
        r.setdefault("account_id", "")
        out.append(r)
    return out


def _detail(r: dict) -> str:
    d = (r.get("account_detail") or "").strip()
    return "" if d == "-" else d


def _value(r: dict, reprt: str, flow: str) -> str | None:
    if r.get("sj_div") in ("IS", "CIS") and reprt != "11011" and flow == "cum":
        v = r.get("thstrm_add_amount")
        if v is not None and str(v).strip():
            return str(v)
    v = r.get("thstrm_amount")
    return None if v is None or not str(v).strip() else str(v)      # 원천 문자열 그대로(빈칸만 '없음')


async def fetch_fs(client: httpx.AsyncClient, con, spec: dict) -> dict:
    corp_code = str(spec.get("id") or "")
    fs_div = spec.get("fs_div") or "CFS"
    reprt = spec.get("reprt") or "11011"
    flow = spec.get("flow") or "cum"
    scope = spec.get("scope") or "all"
    if fs_div not in dart.FS_DIV or reprt not in dart.REPRT or flow not in ("cum", "q") or scope not in ("all", "key"):
        raise SourceError("DART", "bad_request", "재무제표 종류·보고서 종류·범위 값이 맞지 않습니다.")
    sel = (spec.get("sel") or {}).get("SJ") or SJ_DEFAULT
    if sel == "ALL":
        sel = SJ_ORDER
    sel = [s for s in SJ_ORDER if s in sel]
    if scope == "key":
        sel = ["BS", "IS"]            # 주요계정 API는 재무상태표·손익계산서 계정만 준다
    if not sel:
        raise SourceError("DART", "bad_request", "재무제표를 하나 이상 고르세요.")
    years = _years(spec)
    co = await company(client, con, corp_code)
    log: list[str] = []
    warnings: list[str] = []

    async def one(y):
        try:
            if scope == "all":
                return y, await dart.fin_all(client, corp_code, y, reprt, fs_div, log)
            # 주요계정: 다중회사 주요계정 API에 이 회사 하나만. 연결·별도가 함께 오므로 고른 것만 남긴다
            got_rows = [r for r in await dart.multi(client, [corp_code], y, reprt, log) if r.get("fs_div") == fs_div]
            return y, ({"list": key_rows(got_rows)} if got_rows else None)
        except SourceError as e:
            if e.kind == "no_data":
                return y, None
            raise

    got = await asyncio.gather(*(one(y) for y in years))
    missing = [y for y, d in got if d is None]
    have = [(y, d) for y, d in got if d is not None]
    if not have:
        hint = " 연결재무제표가 없는 회사(종속회사 없음)는 '별도'로 바꿔 보세요." if fs_div == "CFS" else ""
        if scope == "key":
            hint += " 주요계정은 상장사·주요 비상장사(사업보고서 제출 대상)만 있습니다."
        raise SourceError("DART", "no_data",
                          f"{years[0]}~{years[-1]}년 {dart.REPRT[reprt]}의 {dart.FS_DIV[fs_div]}가 OpenDART에 없습니다.{hint}", "013")
    if missing:
        warnings.append(f"OpenDART에 자료가 없는 사업연도: {', '.join(map(str, missing))} "
                        "(그해 보고서가 아직 제출·반영되지 않았거나 XBRL 재무제표가 없음)")
    suffix = REPRT_SUFFIX[reprt]
    series: list[dict] = []
    order: dict[int, tuple] = {}
    rows = []
    currencies = set()
    reports = []
    # 연도 잇기(codex 7~9차 검증 반영). 한 줄 안의 '모든' 연도 쌍이 이름이나 유일 계정ID로 직접 맞게, 이름 연결을 ID 연결보다 먼저 두 단계로 묶는다.
    # (조회 기간을 늘리면 새 연도 때문에 ID 연결이 풀릴 수는 있다. 그때는 늘 더 잘게 나뉘는 쪽이다.)
    #  1단계: 같은 표·같은 정규화 이름·같은 세부·같은 순번끼리 묶는다(이름이 같으면 어느 연도끼리든 맞음).
    #  2단계: 이름이 다른 묶음끼리는 두 묶음의 모든 행이 '그 표에 한 번만 나오는 같은 계정ID'이고 연도가 겹치지 않을 때만 합친다.
    groups: dict[tuple, dict] = {}
    for rank_y, (y, data) in enumerate(sorted(have, key=lambda x: -x[0])):
        t = f"{y}{suffix}"
        lst = [r for r in (data.get("list") or []) if r.get("sj_div") in sel]
        rcept = {r.get("rcept_no") for r in data.get("list") or [] if r.get("rcept_no")}
        reports.append((t, sorted(rcept)))
        id_count: dict[tuple, int] = {}
        for r in lst:
            k0 = (r.get("sj_div"), (r.get("account_id") or "").strip())      # 세부(자본변동표 열)와 관계없이 표 전체에서 센다
            id_count[k0] = id_count.get(k0, 0) + 1
        dup: dict[tuple, int] = {}
        for seq, r in enumerate(lst):
            sj = r.get("sj_div")
            acc_id = (r.get("account_id") or "").strip()
            name = (r.get("account_nm") or "").strip()
            detail = _detail(r)
            nbase = (sj, norm_acc(name) or acc_id, re.sub(r"\s+", "", detail))
            dup[nbase] = dup.get(nbase, 0) + 1
            nkey = nbase + (dup[nbase],)          # 같은 보고서 안에 같은 이름이 또 나오면 순번으로 구분
            ikey = (sj, acc_id)
            id_ok = bool(acc_id) and not acc_id.startswith("-") and id_count.get(ikey) == 1
            try:
                o = int(r.get("ord") or 0)
            except ValueError:
                o = 0
            item = {"t": t, "r": r, "sj": sj, "acc_id": acc_id, "name": name, "detail": detail, "occ": dup[nbase],
                    "ikey": ikey if id_ok else None, "rank": (rank_y, SJ_ORDER.index(sj), o, seq)}
            g = groups.setdefault(nkey, {"items": {}, "first": item})
            g["items"][t] = item
    clusters: list[dict] = []
    by_uid: dict[tuple, list] = {}
    for g in sorted(groups.values(), key=lambda g: g["first"]["rank"]):     # 최근 연도·원천 순서대로
        ids = {it["ikey"] for it in g["items"].values()}
        uid = next(iter(ids)) if len(ids) == 1 and None not in ids else None
        g_years = set(g["items"])
        target = next((c for c in by_uid.get(uid, []) if not (c["years"] & g_years)), None) if uid else None
        if target is None:
            target = {"groups": [], "years": set()}
            clusters.append(target)
            if uid:
                by_uid.setdefault(uid, []).append(target)
        target["groups"].append(g)
        target["years"] |= g_years
    for c in clusters:
        main = c["groups"][0]["first"]
        si = len(series)
        others = []
        for g in c["groups"]:
            for it in sorted(g["items"].values(), key=lambda it: it["rank"]):
                if norm_acc(it["name"]) != norm_acc(main["name"]) and it["name"] not in others:
                    others.append(it["name"])       # 띄어쓰기·주석 번호만 다른 이름은 적지 않는다
                cur = (it["r"].get("currency") or "KRW").strip()
                currencies.add(cur)
                v = _value(it["r"], reprt, flow)
                if v is not None:
                    rows.append([si, it["t"], v])
        unit_cur = (main["r"].get("currency") or "KRW").strip()
        sj, detail = main["sj"], main["detail"]
        series.append({
            "codes": {"SJ": sj, "ACCOUNT": main["acc_id"], "DETAIL": detail},
            "names": {"재무제표": SJ_LABEL.get(sj, main["r"].get("sj_nm")),
                      "계정": main["name"] + (" (예전 이름: " + ", ".join(others) + ")" if others else ""), "세부": detail},
            "unit": "원" if unit_cur == "KRW" else unit_cur,
            "label": f"[{SJ_LABEL.get(sj, sj)}] {main['name']}" + (f" ({detail})" if detail else "")
                     + (f" #{main['occ']}" if main["occ"] > 1 else ""),
        })
        order[si] = main["rank"]
    if len(currencies) > 1:
        warnings.append(f"통화가 섞여 있습니다({', '.join(sorted(currencies))}). 단위 열을 확인하세요.")
    # 시계열 순서 바꾸기(최근 보고서 순서 → 예전에만 있는 계정)
    if not rows:
        raise SourceError("DART", "no_data", "고른 재무제표에 금액이 있는 계정이 없습니다. 다른 재무제표나 보고서를 골라 보세요.", "013")
    used = {si for si, _, _ in rows}          # 금액이 하나도 없는 계정(원천이 빈칸으로 준 것)은 뺀다
    new_order = sorted(used, key=lambda i: (SJ_ORDER.index(series[i]["codes"]["SJ"]), order[i][0], order[i][2], order[i][3]))
    remap = {old: new for new, old in enumerate(new_order)}
    series = [series[i] for i in new_order]
    rows = [[remap[si], t, v] for si, t, v in rows]
    rows.sort(key=lambda r: (r[0], r[1]))
    times = sorted({t for _, t, _ in rows})
    sj_names = ", ".join(SJ_LABEL[s] for s in sel)
    flow_text = ""
    if reprt != "11011":
        flow_text = " / 손익: " + ("누적(사업연도 시작부터)" if flow == "cum" else "해당 분기 3개월")
    disp = co.get("stock_name") or co.get("name") or corp_code
    notes = [
        "각 연도 값은 그 연도 보고서의 '당기' 금액이다. 뒤 연도 보고서에서 재작성(소급 수정)된 금액으로 바꾸지 않았다.",
    ]
    if scope == "key":
        notes.append("주요계정은 OpenDART '다중회사 주요계정' API(이 회사 하나만 조회)의 재무상태표·손익계산서 표준 계정이다. "
                     "원천 금액의 천 단위 쉼표만 뺐고, 같은 이름·같은 금액으로 두 번 온 '당기순이익'은 하나만 두었다.")
    notes += [
        "OpenDART 전체 재무제표는 회사가 제출한 XBRL 재무제표 기준이다. 계정 이름·순서는 회사와 연도에 따라 다를 수 있다.",
        "연도를 잇는 기준: 한 줄에 든 모든 연도끼리 ① 같은 표에서 계정 이름이 같거나(주석 번호·앞 번호·띄어쓰기·'(손실)'·'(결손금)'·'(*1)' 표시는 무시) "
        "② 그 표에 한 번만 나오는 같은 계정ID일 때만 한 줄로 잇는다(다른 해를 거쳐 잇지 않음). 이름이 바뀌어 이은 줄은 계정 칸에 예전 이름을 적었다. "
        "계정ID 칸은 가장 최근 연도의 것이다. 같은 표에 같은 이름이 두 번 나오면 나온 순서대로 '#2'를 붙였다.",
    ]
    notes.append("현금 유출 금액의 부호(-)를 회사·연도마다 다르게 적는 일이 있다(예: 취득액을 어떤 해는 음수, 어떤 해는 양수). "
                 "원천 값을 그대로 두었으니 연도 사이 부호가 바뀌면 원문 보고서로 확인한다.")
    if reprt != "11011":
        notes.append("현금흐름표 금액은 원천이 주는 대로 사업연도 시작부터의 누적이다(12월 결산이면 1월부터).")
    latest = sorted(reports)[-1][1] if reports else []
    url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={latest[0]}" if latest else "https://dart.fss.or.kr"
    extra = [(f"{t} 접수번호", ", ".join(f"{n} (https://dart.fss.or.kr/dsaf001/main.do?rcpNo={n})" for n in ns) or "없음")
             for t, ns in sorted(reports)]
    return {
        "spec": {**spec, "start": str(years[0]), "end": str(years[-1])},
        "source": "DART", "source_label": SOURCE_LABEL, "kind": "fs", "layout": "accounts",
        "id": corp_code, "ids": {"회사 고유번호": corp_code, "종목코드": co.get("stock_code") or "",
                                  "보고서": dart.REPRT[reprt], "재무제표 구분": dart.FS_DIV[fs_div]},
        "name": f"{disp} {dart.FS_DIV[fs_div]}" + (" 주요계정" if scope == "key" else ""), "org_name": co.get("name"), "url": url,
        "path": f"{co.get('market') or ''} / 업종코드 {co.get('induty_code') or '-'}",
        "cycle": "A", "cycle_label": REPRT_LABEL[reprt], "start": str(years[0]), "end": str(years[-1]),
        "dim_names": ["재무제표", "계정", "세부"], "series": series, "rows": rows, "times": times,
        "row_count": len(rows), "fetched_at": cat.now_kst(), "requests": log, "warnings": warnings,
        "selection_text": ("주요계정(" if scope == "key" else "전체 계정(") + f"{sj_names}) / {dart.FS_DIV[fs_div]} / {dart.REPRT[reprt]}{flow_text}",
        "notes": notes, "extra_prov": extra, "program": f"KR Public Data Hub {VERSION}",
    }


# ------------------------------------------------------------------ 공시 목록
def _date(s: str, what: str) -> _dt.date:
    t = re.sub(r"[^0-9]", "", str(s or ""))
    try:
        return _dt.date(int(t[:4]), int(t[4:6]), int(t[6:8]))
    except (ValueError, IndexError):
        raise SourceError("DART", "bad_period", f"{what} 날짜는 2026-01-31처럼 넣어 주세요.") from None


async def fetch_list(client: httpx.AsyncClient, con, spec: dict) -> dict:
    corp_code = str(spec.get("id") or "")
    a, b = _date(spec.get("start"), "시작"), _date(spec.get("end"), "끝")
    if a > b:
        a, b = b, a
    co = await company(client, con, corp_code)
    log: list[str] = []
    notes: list = []
    rows = await dart.disclosures(client, corp_code, a.strftime("%Y%m%d"), b.strftime("%Y%m%d"),
                                  max_pages=MAX_LIST_PAGES, log=log, notes=notes)
    warnings = []
    for total, got in notes:
        warnings.append(f"공시 {total:,}건 중 최근 {got:,}건만 받았습니다. 기간을 나눠 받으세요." if got < total
                        else f"OpenDART가 알린 공시 수({total:,})와 받은 수({got:,})가 다릅니다.")
    head = ["접수일자", "보고서명", "제출인", "접수번호", "비고", "법인구분", "원문 보기"]
    body = []
    for r in rows:
        d = r.get("rcept_dt") or ""
        body.append([f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d, r.get("report_nm") or "",      # 보고서명은 원천 그대로(끝 공백 포함)
                     r.get("flr_nm"), r.get("rcept_no"), r.get("rm"), dart.CORP_CLS.get(r.get("corp_cls") or "", r.get("corp_cls")),
                     f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={r.get('rcept_no')}"])
    disp = co.get("stock_name") or co.get("name") or corp_code
    return {
        "spec": {**spec, "start": a.isoformat(), "end": b.isoformat()},
        "source": "DART", "source_label": SOURCE_LABEL, "kind": "list", "layout": "table",
        "id": corp_code, "ids": {"회사 고유번호": corp_code, "종목코드": co.get("stock_code") or ""},
        "name": f"{disp} 공시 목록", "org_name": co.get("name"), "url": "https://dart.fss.or.kr/dsab007/main.do", "path": co.get("market"),
        "cycle": "D", "cycle_label": "공시일", "start": a.isoformat(), "end": b.isoformat(),
        "dim_names": [], "series": [], "rows": [], "times": [],
        "table": {"head": head, "rows": body},
        "row_count": len(body), "fetched_at": cat.now_kst(), "requests": log, "warnings": warnings,
        "selection_text": f"공시 기간 {a.isoformat()} ~ {b.isoformat()}", "notes": [],
        "program": f"KR Public Data Hub {VERSION}",
    }


# ------------------------------------------------------------------ 업종 비교
IND_ACCOUNTS = [("IS", "매출액"), ("IS", "영업이익"), ("IS", "당기순이익"), ("BS", "자산총계"), ("BS", "부채총계"), ("BS", "자본총계")]
IND_MAX_CORPS = 1000


def industry_info(con) -> dict:
    # 업종코드를 받은 뒤에는 상장폐지(E)를 빼고 센다. 받기 전에는 종목코드 있는 회사 전체가 대상
    n = con.execute("SELECT COUNT(*), SUM(induty_code IS NOT NULL AND induty_code<>'') FROM dart_corps "
                    f"WHERE {LISTED}").fetchone()
    at = con.execute("SELECT v FROM build_info WHERE k='dart_industry_at'").fetchone()
    return {"listed": n[0] or 0, "with_code": n[1] or 0, "at": at[0] if at else None}


def save_jurir(con, corp_code: str, jurir_no: str | None) -> None:
    j = re.sub(r"\D", "", jurir_no or "")
    if len(j) == 13:
        con.execute("INSERT OR REPLACE INTO dart_jurir VALUES (?, ?)", (corp_code, j))


async def refresh_industry(client: httpx.AsyncClient, con, progress=None) -> dict:
    """상장사마다 기업개황을 받아 업종코드를 저장한다(약 4천 번 호출, 분당 300회 제한으로 15분 안팎)."""
    codes = [r[0] for r in con.execute("SELECT corp_code FROM dart_corps WHERE stock_code<>'' ORDER BY corp_code")]
    done, fails = 0, []

    async def one(cc):
        nonlocal done
        try:
            c = await dart.company(client, cc)
            con.execute("UPDATE dart_corps SET induty_code=?, corp_cls=? WHERE corp_code=?",
                        (c.get("induty_code") or None, c.get("corp_cls") or None, cc))
            save_jurir(con, cc, c.get("jurir_no"))
        except SourceError as e:
            if e.kind in ("auth", "rate_limit"):
                raise
            fails.append((cc, e.kind))
        done += 1
        if progress:
            progress(done, len(codes))
        if done % 200 == 0:
            con.commit()

    for i in range(0, len(codes), 40):           # 조금씩 나눠 보내고, 인증·한도 오류면 바로 멈춘다
        await asyncio.gather(*(one(cc) for cc in codes[i:i + 40]))
    con.execute("INSERT OR REPLACE INTO build_info VALUES ('dart_industry_at', ?)", (cat.now_kst(),))
    con.commit()
    return {**industry_info(con), "failed": len(fails)}


def industry_levels(con, code: str | None) -> list[dict]:
    code = (code or "").strip()
    out = []
    for n in range(len(code), 1, -1):
        p = code[:n]
        cnt = con.execute(f"SELECT COUNT(*) FROM dart_corps WHERE {LISTED_KNOWN} AND induty_code LIKE ?", (p + "%",)).fetchone()[0]
        out.append({"code": p, "name": ksic.name(p), "level": ksic.LEVEL.get(n, ""), "count": cnt})
    return out


def _amount(v) -> str | None:
    if v is None:
        return None
    t = str(v).replace(",", "").strip()
    return t or None


async def fetch_industry(client: httpx.AsyncClient, con, spec: dict) -> dict:
    prefix = str(spec.get("id") or "").strip()
    reprt = spec.get("reprt") or "11011"
    fs_div = spec.get("fs_div") or "CFS"
    if not re.fullmatch(r"\d{2,5}", prefix) or reprt not in dart.REPRT or fs_div not in dart.FS_DIV:
        raise SourceError("DART", "bad_request", "업종코드(숫자 2~5자리)·보고서·재무제표 구분 값이 맞지 않습니다.")
    year = _years({"start": spec.get("start"), "end": spec.get("start")})[0]
    corps = con.execute(f"SELECT * FROM dart_corps WHERE {LISTED_KNOWN} AND induty_code LIKE ? ORDER BY corp_name",
                        (prefix + "%",)).fetchall()
    if not corps:
        info = industry_info(con)
        hint = "" if info["with_code"] else " 먼저 '상장사 업종코드 받기'를 해 주세요."
        raise SourceError("DART", "no_data", f"업종코드 {prefix}로 시작하는 상장사가 없습니다.{hint}")
    if len(corps) > IND_MAX_CORPS:
        raise SourceError("DART", "too_large", f"상장사가 {len(corps):,}개라 한 번에 받을 수 없습니다(최대 {IND_MAX_CORPS:,}). 더 자세한 업종을 고르세요.")
    log: list[str] = []
    codes = [c["corp_code"] for c in corps]
    got: list[dict] = []
    for i in range(0, len(codes), dart.MULTI_MAX):        # 100개씩 차례로
        try:
            got += await dart.multi(client, codes[i:i + dart.MULTI_MAX], year, reprt, log)
        except SourceError as e:
            if e.kind != "no_data":
                raise
    by_corp: dict[str, list] = {}
    for r in got:
        by_corp.setdefault(r.get("corp_code"), []).append(r)
    other = "OFS" if fs_div == "CFS" else "CFS"
    targets = {(sj, norm_acc(nm)) for sj, nm in IND_ACCOUNTS}
    table_rows, missing, fallback = [], [], 0
    for c in corps:
        rows = by_corp.get(c["corp_code"]) or []
        use = fs_div if any(r.get("fs_div") == fs_div for r in rows) else (other if any(r.get("fs_div") == other for r in rows) else None)
        if use is None:                 # 자료 없는 상장사도 표에 빈칸으로 남긴다(업종 상장사 집합을 그대로 보이게)
            missing.append(c["corp_name"])
            table_rows.append({"c": c, "use": None, "period": "", "rcept": "", "vals": [None] * len(IND_ACCOUNTS)})
            continue
        fallback += use != fs_div
        vals: dict[tuple, str | None] = {}
        period, rcept = "", ""
        for r in key_rows([r for r in rows if r.get("fs_div") == use]):
            k = (r.get("sj_div"), norm_acc(r.get("account_nm")))
            if k in targets and k not in vals:
                vals[k] = _value(r, reprt, "cum")
                if r.get("sj_div") == "IS" and not period:
                    period = r.get("thstrm_dt") or ""
            rcept = rcept or r.get("rcept_no") or ""
        table_rows.append({"c": c, "use": use, "period": period, "rcept": rcept,
                           "vals": [vals.get((sj, norm_acc(nm))) for sj, nm in IND_ACCOUNTS]})
    if len(missing) == len(corps):
        raise SourceError("DART", "no_data", f"{year}년 {dart.REPRT[reprt]}의 주요계정이 이 업종 상장사 모두에 없습니다.", "013")
    from decimal import Decimal, InvalidOperation

    def dec(v):
        try:
            return Decimal(v) if v is not None else None
        except InvalidOperation:
            return None
    total = sum((dec(t["vals"][0]) for t in table_rows if dec(t["vals"][0]) is not None), Decimal(0))
    table_rows.sort(key=lambda t: (t["use"] is None, dec(t["vals"][0]) is None, -(dec(t["vals"][0]) or 0), t["c"]["corp_name"]))
    head = ["순위", "회사명", "종목코드", "회사 고유번호", "시장", "업종코드", "업종명", "재무제표", "손익 기간", "접수번호",
            "매출액", "영업이익", "당기순이익", "자산총계", "부채총계", "자본총계", "매출액 비중(%)"]
    body = []
    rank = 0
    for t in table_rows:
        c, rev = t["c"], dec(t["vals"][0])
        share = f"{(rev / total * 100):.2f}" if rev is not None and total else None
        rank += t["use"] is not None
        basis = {"CFS": "연결", "OFS": "별도", None: "자료 없음"}[t["use"]]
        body.append([rank if t["use"] else None, c["corp_name"], c["stock_code"], c["corp_code"],
                     dart.CORP_CLS.get(c["corp_cls"] or "", ""), c["induty_code"], ksic.name(c["induty_code"]) or "", basis,
                     t["period"], t["rcept"], *t["vals"], share])
    warnings = []
    if missing:
        warnings.append(f"{year}년 {dart.REPRT[reprt]} 주요계정이 없는 회사 {len(missing)}곳은 표 아래쪽에 '자료 없음'으로 두었습니다: "
                        + ", ".join(missing[:10]) + (" 외" if len(missing) > 10 else ""))
    if fallback:
        warnings.append(f"{dart.FS_DIV[fs_div]}가 없는 {fallback}곳은 {dart.FS_DIV[other]} 금액을 넣었습니다('재무제표' 칸 참고).")
    no_rev = sum(dec(t["vals"][0]) is None for t in table_rows)
    if no_rev:
        warnings.append(f"매출액 계정이 없는 회사 {no_rev}곳(은행·보험 같은 금융업 등)은 매출액·비중 칸을 비웠습니다. "
                        "금융업은 '재무제표' 탭의 주요계정에서 이자수익·순수수료손익 등을 보세요.")
    periods_ = {t["period"] for t in table_rows if t["period"]}
    if len(periods_) > 1:
        warnings.append("결산 기간이 다른 회사가 섞여 있습니다('손익 기간' 칸 참고).")
    ind_name = ksic.name(prefix) or ""
    notes = [
        "금액은 OpenDART '다중회사 주요계정' API 값(원, 천 단위 쉼표만 뺌)이다.",
        "매출액 비중(%)은 이 표에 든 회사 매출액 합계 대비 비율로 이 프로그램이 계산한 값이다(원천 값 아님). 회사 전체 매출(해외·다른 사업 포함)이라 "
        "관련시장 점유율과 다르며, 비상장사·외국 회사는 빠져 있다.",
        "업종코드는 OpenDART 기업개황의 회사 대표 업종이고, 업종명은 KOSIS 전국사업체조사의 제11차 한국표준산업분류 이름이다(OpenDART 업종코드도 제11차).",
        "상장사는 기업개황 법인구분이 유가증권시장·코스닥·코넥스인 회사다(상장폐지 회사는 뺌). 법인구분·업종코드는 '상장사 업종코드 받기' 시점 기준이다.",
    ]
    info = industry_info(con)
    return {
        "spec": {**spec, "start": str(year), "end": str(year)},
        "source": "DART", "source_label": SOURCE_LABEL, "kind": "industry", "layout": "table",
        "id": prefix, "ids": {"업종코드(KSIC)": prefix, "업종명": ind_name, "사업연도": str(year), "보고서": dart.REPRT[reprt]},
        "name": f"{ind_name or '업종'}({prefix}) 상장사 주요계정", "org_name": "금융감독원", "url": "https://opendart.fss.or.kr",
        "path": f"업종코드 {prefix} {ind_name}", "cycle": "A", "cycle_label": REPRT_LABEL[reprt], "start": str(year), "end": str(year),
        "dim_names": [], "series": [], "rows": [], "times": [],
        "table": {"head": head, "rows": body, "numeric_cols": list(range(10, 17))},
        "row_count": len(body), "fetched_at": cat.now_kst(), "requests": log, "warnings": warnings,
        "selection_text": f"업종코드 {prefix}로 시작하는 상장사 {len(corps)}곳 / {year} {dart.REPRT[reprt]} / {dart.FS_DIV[fs_div]} 우선",
        "notes": notes, "extra_prov": [("상장사 업종코드 받은 시각", info["at"] or "모름")],
        "program": f"KR Public Data Hub {VERSION}",
    }


async def fetch(client: httpx.AsyncClient, con, spec: dict) -> dict:
    kind = spec.get("kind") or "fs"
    if kind == "industry":
        return await fetch_industry(client, con, spec)
    if kind == "fs":
        return await fetch_fs(client, con, spec)
    if kind == "list":
        return await fetch_list(client, con, spec)
    raise SourceError("DART", "bad_request", f"알 수 없는 DART 조회 종류입니다: {kind}")
