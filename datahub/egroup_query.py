"""공정위 기업집단포털: 색인 만들기, 회사 → 소속 기업집단, 집단 상세(소속회사·변경내역), 내려받기 표.

색인(catalog.sqlite의 egroup_* 표)은 '공개년월'(지정 기준 연월, 1997~)마다 지정 집단·자산순위·소속회사와
전체 기간의 편입·제외·유예 변경내역을 담는다. 화면·내려받기는 이 색인을 쓴다(원천은 연 1회 지정·수시 변경).

    python3 -m datahub.build_catalog --only egroup     # 다시 받기(호출 약 130번, 몇 분)
"""
from __future__ import annotations

import json
import re

import httpx

from . import catalog as cat
from .config import VERSION
from .dart_query import norm_name
from .errors import SourceError
from .sources import egroup

SOURCE_LABEL = "공정거래위원회 기업집단포털(공공데이터포털 OpenAPI)"
PORTAL = "https://www.egroup.go.kr"


def ym_label(ym: str | None) -> str:
    ym = ym or ""
    return f"{ym[:4]}-{ym[4:6]}" if re.fullmatch(r"\d{6}", ym) else ym


def _int(v) -> int | None:
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ 색인 만들기
async def refresh(client: httpx.AsyncClient, con, progress=None) -> dict:
    log: list[str] = []
    notes: list = []
    yms = [r.get("othbcYm") for r in await egroup.fetch_all(client, "publicYmList", log, jobSeCode="0001") if r.get("othbcYm")]
    if not yms:
        raise SourceError("EGROUP", "no_data", "기업집단포털이 공개년월 목록을 주지 않았습니다.")
    fails = []
    for i, ym in enumerate(yms, start=1):
        try:
            groups = await egroup.fetch_all(client, "appnGroupSttusList", log, notes, presentnYear=ym)
            assets = await egroup.fetch_all(client, "appnGroupAssetsList", log, notes, presentnYear=ym)
            affs = await egroup.fetch_all(client, "appnGroupAffiList", log, notes, presentnYear=ym)
        except SourceError as e:
            if e.kind in ("auth", "rate_limit"):
                raise
            fails.append((ym, e.message))
            continue
        by_code = {a.get("unityGrupCode"): a for a in assets}
        names: dict[str, list] = {}
        for g in groups:
            names.setdefault(g.get("unityGrupNm"), []).append(g.get("unityGrupCode"))
        con.execute("DELETE FROM egroup_groups WHERE ym=?", (ym,))
        con.execute("DELETE FROM egroup_affiliates WHERE ym=?", (ym,))
        con.executemany("INSERT OR REPLACE INTO egroup_groups VALUES (?,?,?,?,?,?,?,?,?,?)", [
            (ym, g.get("unityGrupCode"), g.get("unityGrupNm"), g.get("smerNm"), g.get("repreCmpny"), _int(g.get("sumCmpnyCo")),
             g.get("invstmntLmtt"), g.get("entrprsCl"), _int(by_code.get(g.get("unityGrupCode"), {}).get("rn")),
             by_code.get(g.get("unityGrupCode"), {}).get("assetsTotamt")) for g in groups])
        con.executemany("INSERT INTO egroup_affiliates VALUES (?,?,?,?,?,?,?,?,?)", [
            (ym, (names.get(a.get("unityGrupNm")) or [None])[0] if len(names.get(a.get("unityGrupNm")) or []) == 1 else None,
             a.get("unityGrupNm"), a.get("entrprsNm"), a.get("jurirno"), a.get("bizrno"), a.get("rprsntvNm"), a.get("fondDe"),
             a.get("grinil")) for a in affs])
        con.commit()
        if progress:
            progress(i, len(yms), ym, len(groups), len(affs))
    changes = await egroup.fetch_all(client, "tyAssetsRentDelngDtlsList", log, notes)
    con.execute("DELETE FROM egroup_changes")
    con.executemany("INSERT INTO egroup_changes VALUES (?,?,?,?,?,?)", [
        (c.get("unityGrupNm"), c.get("entrprsNm"), c.get("jurirno"), egroup.CHANGE.get(c.get("psitnCmpnyChangeSeCode") or "", c.get("psitnCmpnyChangeSeCode")),
         c.get("exclDe") or c.get("incrprDe") or c.get("postpneBeginDe") or c.get("postpneConfmDe") or "",
         json.dumps(c, ensure_ascii=False)) for c in changes])
    con.execute("INSERT OR REPLACE INTO build_info VALUES ('egroup_built_at', ?)", (cat.now_kst(),))
    con.execute("INSERT OR REPLACE INTO build_info VALUES ('egroup_notes', ?)",
                (json.dumps({"count_mismatch": notes, "failed_ym": fails}, ensure_ascii=False),))
    con.commit()
    return {**info(con), "calls": len(log), "count_mismatch": notes, "failed_ym": fails}


def info(con) -> dict:
    yms = [r[0] for r in con.execute("SELECT DISTINCT ym FROM egroup_groups ORDER BY ym DESC")]
    at = con.execute("SELECT v FROM build_info WHERE k='egroup_built_at'").fetchone()
    n = con.execute("SELECT COUNT(*) FROM egroup_groups WHERE ym=?", (yms[0],)).fetchone()[0] if yms else 0
    na = con.execute("SELECT COUNT(*) FROM egroup_affiliates WHERE ym=?", (yms[0],)).fetchone()[0] if yms else 0
    nc = con.execute("SELECT COUNT(*) FROM egroup_changes").fetchone()[0]
    return {"yms": yms, "latest": yms[0] if yms else None, "groups": n, "affiliates": na, "changes": nc, "at": at[0] if at else None}


# ------------------------------------------------------------------ 조회
def _group_row(r) -> dict:
    return {"ym": r["ym"], "ym_label": ym_label(r["ym"]), "code": r["code"], "name": r["name"], "owner": r["owner"],
            "rep": r["rep"], "n_comp": r["n_comp"], "limit_type": r["limit_type"], "cls": r["cls"],
            "asset_rank": r["asset_rank"], "asset_total": r["asset_total"]}


def for_company(con, jurir_no: str | None) -> dict | None:
    """법인등록번호로 이 회사가 들어 있던 지정 기업집단(가장 최근 공개년월 기준)과 이력."""
    j = re.sub(r"\D", "", jurir_no or "")
    if len(j) != 13:
        return None
    rows = con.execute("SELECT a.ym, a.group_code, a.group_name, a.joined FROM egroup_affiliates a WHERE a.jurirno=? "
                       "ORDER BY a.ym DESC", (j,)).fetchall()
    if not rows:
        return None
    latest_all = con.execute("SELECT MAX(ym) FROM egroup_groups").fetchone()[0]
    top = rows[0]
    g = con.execute("SELECT * FROM egroup_groups WHERE ym=? AND code=?", (top["ym"], top["group_code"])).fetchone()
    hist: list[dict] = []
    for r in rows:
        if not hist or hist[-1]["group_name"] != r["group_name"]:
            hist.append({"group_name": r["group_name"], "group_code": r["group_code"], "from": r["ym"], "to": r["ym"]})
        else:
            hist[-1]["from"] = r["ym"]
    return {"current": top["ym"] == latest_all, "ym": top["ym"], "ym_label": ym_label(top["ym"]),
            "group_code": top["group_code"], "group_name": top["group_name"], "joined": top["joined"],
            "group": _group_row(g) if g else None,
            "history": [{**h, "from_label": ym_label(h["from"]), "to_label": ym_label(h["to"])} for h in hist]}


def search_groups(con, q: str, limit: int = 20) -> list[dict]:
    nq = norm_name(q)
    latest = con.execute("SELECT MAX(ym) FROM egroup_groups").fetchone()[0]
    if not nq or not latest:
        return []
    rows = con.execute("SELECT * FROM egroup_groups WHERE ym=?", (latest,)).fetchall()
    hits = [r for r in rows if nq in norm_name(r["name"]) or nq in norm_name(r["rep"] or "")]
    hits.sort(key=lambda r: (norm_name(r["name"]) != nq, r["asset_rank"] or 999))
    return [_group_row(r) for r in hits[:limit]]


def _dart_match(con, affs) -> dict:
    """소속회사 → DART 고유번호. ① 법인등록번호가 같은 회사(DART 기업개황에서 받아 둔 것) ② 없으면 회사 이름(정규화)이
    DART 회사 목록에 '딱 하나'만 있을 때. 동명 회사는 붙이지 않는다. 결과에 어떻게 이었는지(how)를 남긴다."""
    out = {}
    for a in affs:
        j = re.sub(r"\D", "", a["jurirno"] or "")
        r = con.execute("SELECT c.corp_code, c.stock_code, c.corp_cls FROM dart_jurir j JOIN dart_corps c ON c.corp_code=j.corp_code "
                        "WHERE j.jurir_no=? LIMIT 2", (j,)).fetchall() if len(j) == 13 else []
        how = "법인등록번호"
        if len(r) != 1:
            r = con.execute("SELECT corp_code, stock_code, corp_cls FROM dart_corps WHERE name_norm=? LIMIT 2",
                            (norm_name(a["company"]),)).fetchall()
            how = "이름"
        if len(r) == 1:
            out[a["company"]] = {"corp_code": r[0][0], "stock_code": r[0][1], "listed": bool(r[0][1]) and r[0][2] != "E", "how": how}
    return out


def group(con, code: str, ym: str | None = None) -> dict:
    yms = [r[0] for r in con.execute("SELECT ym FROM egroup_groups WHERE code=? ORDER BY ym DESC", (code,))]
    if not yms:
        raise SourceError("EGROUP", "not_found", f"기업집단 코드 {code} 가 색인에 없습니다.")
    ym = ym if ym in yms else yms[0]
    g = con.execute("SELECT * FROM egroup_groups WHERE ym=? AND code=?", (ym, code)).fetchone()
    affs = con.execute("SELECT * FROM egroup_affiliates WHERE ym=? AND (group_code=? OR (group_code IS NULL AND group_name=?)) "
                       "ORDER BY company", (ym, code, g["name"])).fetchall()
    dm = _dart_match(con, affs)
    changes = con.execute("SELECT * FROM egroup_changes WHERE group_name=? ORDER BY day DESC", (g["name"],)).fetchall()
    return {"group": _group_row(g), "yms": [{"ym": y, "label": ym_label(y)} for y in yms],
            "affiliates": [{"company": a["company"], "jurirno": a["jurirno"], "bizrno": a["bizrno"], "ceo": a["ceo"],
                            "founded": a["founded"], "joined": a["joined"], "dart": dm.get(a["company"])} for a in affs],
            "changes": [change_row(c) for c in changes],
            "notes": ["소속회사의 DART 연결: 법인등록번호가 같은 DART 회사가 있으면 그것(상장사 등 기업개황을 받아 둔 회사), "
                      "없으면 회사 이름이 DART 회사 목록에 하나뿐일 때만 붙였다(동명 회사는 붙이지 않음).",
                      "변경내역은 집단 이름으로 묶었다(원천에 집단 코드가 없음). 집단 이름이 바뀐 경우 옛 이름의 변경내역은 빠질 수 있다."]}


def change_row(c) -> dict:
    b = json.loads(c["body"]) if isinstance(c["body"], str) else {}
    reason = ""
    if b.get("psitnCmpnyChangeSeCode") == "0001":
        reason = egroup.INCRPR.get(b.get("incrprPrvonshCode") or "", b.get("incrprPrvonshCode") or "")
    elif b.get("psitnCmpnyChangeSeCode") == "0002":
        reason = " · ".join(x for x in (egroup.EXCL.get(b.get("exclPrvonshCode") or "", b.get("exclPrvonshCode") or ""),
                                         egroup.EXCL_JOB.get(b.get("exclTrgetJobCode") or "", "")) if x and x != "해당없음")
    elif b.get("psitnCmpnyChangeSeCode") == "0003":
        reason = egroup.POSTPONE.get(b.get("postpnePrvonshTyCode") or "", b.get("postpnePrvonshTyCode") or "")
    return {"group_name": c["group_name"], "company": c["company"], "jurirno": c["jurirno"], "change": c["change"],
            "day": c["day"], "reason": reason, "bizrno": b.get("bizrno"), "postpone_end": b.get("postpneEndDe") or b.get("postpneEndPrearngeDe")}


# ------------------------------------------------------------------ 내려받기(표)
def _table_result(kind, ident, name, head, rows, ym, extra_ids, selection, notes, numeric_cols=None) -> dict:
    return {
        "spec": {}, "source": "EGROUP", "source_label": SOURCE_LABEL, "kind": kind, "layout": "table",
        "id": ident, "ids": extra_ids, "name": name, "org_name": "공정거래위원회", "url": PORTAL, "path": "기업집단포털",
        "cycle": "A", "cycle_label": "공개년월" if ym else "변경일", "start": ym_label(ym) if ym else "", "end": ym_label(ym) if ym else "",
        "dim_names": [], "series": [], "rows": [], "times": [],
        "table": {"head": head, "rows": rows, **({"numeric_cols": numeric_cols} if numeric_cols else {})},
        "row_count": len(rows), "fetched_at": cat.now_kst(), "requests": [], "warnings": [],
        "selection_text": selection, "notes": notes, "program": f"KR Public Data Hub {VERSION}",
    }


def fetch(con, spec: dict) -> dict:
    kind = spec.get("kind") or "affiliates"
    built = (con.execute("SELECT v FROM build_info WHERE k='egroup_built_at'").fetchone() or [""])[0]
    base_notes = [f"기업집단포털 OpenAPI를 이 프로그램 색인으로 받아 둔 것이다(색인 받은 시각 {built}). 최신 변동은 기업집단포털에서 확인한다."]
    if kind == "groups":
        ym = spec.get("ym") or info(con)["latest"]
        rows = con.execute("SELECT * FROM egroup_groups WHERE ym=? ORDER BY COALESCE(asset_rank, 999), name", (ym,)).fetchall()
        if not rows:
            raise SourceError("EGROUP", "no_data", f"공개년월 {ym_label(ym)}의 지정 기업집단이 색인에 없습니다.")
        head = ["자산 순위", "기업집단명", "기업집단코드", "동일인", "대표회사", "소속회사 수", "상호출자제한 여부", "기업집단 구분", "자산총액(10억원)"]
        body = [[r["asset_rank"], r["name"], r["code"], r["owner"], r["rep"], r["n_comp"], r["limit_type"], r["cls"], r["asset_total"]] for r in rows]
        res = _table_result("groups", ym, f"지정 대규모기업집단 {ym_label(ym)}", head, body, ym, {"공개년월": ym_label(ym)},
                            f"공개년월 {ym_label(ym)} 지정 기업집단 {len(body)}곳",
                            base_notes + ["자산총액은 원천 단위(10억원)·원천 표기(천 단위 쉼표) 그대로다."], numeric_cols=[5])
    elif kind == "affiliates":
        g = group(con, str(spec.get("id") or ""), spec.get("ym"))
        gg = g["group"]
        head = ["기업집단명", "기업집단코드", "공개년월", "회사명", "법인등록번호", "사업자등록번호", "대표자", "설립일", "계열편입일", "DART 고유번호", "종목코드"]
        body = [[gg["name"], gg["code"], gg["ym_label"], a["company"], a["jurirno"], a["bizrno"], a["ceo"], a["founded"], a["joined"],
                 (a["dart"] or {}).get("corp_code") or "", (a["dart"] or {}).get("stock_code") or ""] for a in g["affiliates"]]
        res = _table_result("affiliates", gg["code"], f"{gg['name']} 소속회사 {gg['ym_label']}", head, body, gg["ym"],
                            {"기업집단코드": gg["code"], "기업집단명": gg["name"], "공개년월": gg["ym_label"]},
                            f"{gg['name']}({gg['code']}) 공개년월 {gg['ym_label']} 소속회사 {len(body)}곳", base_notes + g["notes"])
    elif kind == "changes":
        g = group(con, str(spec.get("id") or ""))
        gg = g["group"]
        head = ["기업집단명", "회사명", "법인등록번호", "사업자등록번호", "변경구분", "일자", "사유", "유예종료(예정)일"]
        body = [[c["group_name"], c["company"], c["jurirno"], c["bizrno"], c["change"], c["day"], c["reason"], c["postpone_end"]]
                for c in g["changes"]]
        res = _table_result("changes", gg["code"], f"{gg['name']} 계열 편입·제외·유예 변경내역", head, body, None,
                            {"기업집단코드": gg["code"], "기업집단명": gg["name"]}, f"{gg['name']} 변경내역 {len(body)}건",
                            base_notes + g["notes"][1:])
        days = sorted(c["day"] for c in g["changes"] if re.fullmatch(r"\d{8}", c["day"] or ""))
        if days:                       # 파일 이름·출처의 기간 = 가장 이른·늦은 변경일
            res["start"], res["end"] = (f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in (days[0], days[-1]))
    else:
        raise SourceError("EGROUP", "bad_request", f"알 수 없는 기업집단 조회 종류입니다: {kind}")
    res["spec"] = dict(spec)
    res["requests"] = [egroup.masked(svc, f"{svc}Api", {}) for svc in
                       {"groups": ["appnGroupSttusList", "appnGroupAssetsList"], "affiliates": ["appnGroupAffiList"],
                        "changes": ["tyAssetsRentDelngDtlsList"]}[kind]]
    return res
