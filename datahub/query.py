"""통계표 상세(차원·주기)와 자료 조회. 두 기관의 응답을 같은 모양으로 맞춘다.

조회 조건(spec)
    {"source": "KOSIS", "id": "101/DT_1J22003", "cycle": "M", "start": "2016-01", "end": "2026-10",
     "sel": {"ITEM": ["T"], "C1": ["T10", "T11"]}}          # 차원별 코드 목록, "ALL" = 전체
결과
    series: 시계열마다 이름·단위·코드, rows: (시계열 번호, 시점, 원천 값 문자열)
값은 원천 API가 준 문자열을 그대로 보관한다(다운로드 수치 = 원천 수치).
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import time
import unicodedata

import httpx

from . import catalog as cat
from . import periods
from .config import VERSION
from .errors import SourceError
from .sources import ecos, kosis

META_TTL = 24 * 3600
RESULT_TTL = 15 * 60
MAX_ECOS_CALLS = 60          # ECOS 3분 300회 제한 안에서 여유 있게
_results: dict[str, tuple[float, dict]] = {}


def spec_hash(spec: dict) -> str:
    keep = {k: spec.get(k) for k in ("source", "id", "cycle", "start", "end", "sel")}
    for k in ("kind", "fs_div", "reprt", "flow", "scope", "ym"):          # DART 조회 조건(있을 때만 → 기존 해시는 그대로)
        if k in spec:
            keep[k] = spec[k]
    return hashlib.sha1(json.dumps(keep, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _nfkc(s):
    return unicodedata.normalize("NFKC", s) if isinstance(s, str) else s


# ------------------------------------------------------------------ 상세
def _cached_meta(con, source: str, ds_id: str):
    r = con.execute("SELECT fetched_at, body FROM meta_cache WHERE source=? AND ds_id=?", (source, ds_id)).fetchone()
    if r and time.time() - float(r[0]) < META_TTL:
        return json.loads(r[1])
    return None


def _store_meta(con, source: str, ds_id: str, body: dict) -> None:
    con.execute("INSERT OR REPLACE INTO meta_cache VALUES (?,?,?,?)",
                (source, ds_id, str(time.time()), json.dumps(body, ensure_ascii=False)))
    con.commit()


def _tree_order(items: list[dict]) -> list[dict]:
    """부모-자식 순서로 정렬하고 depth 를 붙인다(원래 순서 유지)."""
    by_parent: dict = {}
    codes = {it["code"] for it in items}
    for it in items:
        p = it.get("parent") if it.get("parent") in codes else None
        by_parent.setdefault(p, []).append(it)
    out = []

    def walk(p, depth):
        for it in by_parent.get(p, []):
            if any(o is it for o in out):
                continue
            it["depth"] = depth
            out.append(it)
            walk(it["code"], depth + 1)

    walk(None, 0)
    for it in items:   # 순환 등으로 빠진 것
        if not any(o is it for o in out):
            it["depth"] = 0
            out.append(it)
    return out


async def detail(client: httpx.AsyncClient, con, catalog: cat.Catalog, source: str, ds_id: str) -> dict:
    if source == "DATAGO":            # 공공데이터포털: 저장된 목록 정보로만(원천 호출 없음)
        from . import datago_query
        return datago_query.detail(con, ds_id)
    cached = _cached_meta(con, source, ds_id)
    if cached:
        return cached
    entry = catalog.entries.get((source, ds_id))
    row = entry.row if entry else {}
    if source == "ECOS":
        body = await _ecos_detail(client, con, ds_id, row)
    elif source == "KOSIS":
        body = await _kosis_detail(client, ds_id, row)
    else:
        raise SourceError(source, "bad_request", "지원하지 않는 기관입니다.")
    if not body.get("warnings"):          # 항목 목록이 덜 온 상세는 캐시하지 않는다(다음에 다시 받음)
        _store_meta(con, source, ds_id, body)
    return body


async def _ecos_detail(client, con, stat_code: str, row: dict) -> dict:
    rows = [dict(r) for r in con.execute("SELECT * FROM ecos_items WHERE stat_code=?", (stat_code,))]
    item_notes: list = []
    if rows:
        items_raw = [{"GRP_CODE": r["grp_code"], "GRP_NAME": r["grp_name"], "ITEM_CODE": r["item_code"],
                      "ITEM_NAME": r["item_name"], "P_ITEM_CODE": r["p_item_code"], "CYCLE": r["cycle"],
                      "START_TIME": r["start"], "END_TIME": r["end"], "UNIT_NAME": r["unit"],
                      "DATA_CNT": r["data_cnt"]} for r in rows]
    else:
        notes: list = []
        try:
            items_raw = await ecos.item_list(client, stat_code, notes=notes)
        except SourceError as e:
            if e.kind == "no_data":     # ECOS는 없는 표도 INFO-200(결과 없음)으로 답한다
                raise SourceError("ECOS", "not_found", f"ECOS에 통계표 코드 {stat_code} 가 없습니다.", e.code) from None
            raise
        if not items_raw:
            raise SourceError("ECOS", "not_found", f"ECOS에 통계표 코드 {stat_code} 가 없습니다.")
        item_notes = notes
    groups: dict[str, dict] = {}
    for it in items_raw:
        g = groups.setdefault(it["GRP_CODE"], {"key": it["GRP_CODE"], "name": it.get("GRP_NAME") or it["GRP_CODE"],
                                               "items": {}})
        item = g["items"].setdefault(it["ITEM_CODE"], {"code": it["ITEM_CODE"], "name": it["ITEM_NAME"],
                                                       "parent": it.get("P_ITEM_CODE"), "cycles": {}, "unit": None})
        c = it.get("CYCLE")
        if c in periods.CYCLE_LABEL:
            try:
                item["cycles"][c] = [periods.to_display(c, it["START_TIME"]), periods.to_display(c, it["END_TIME"])]
            except periods.PeriodError:
                pass
        item["unit"] = item["unit"] or it.get("UNIT_NAME")
    dims = []
    for key in sorted(groups):
        g = groups[key]
        dims.append({"key": key, "name": g["name"], "items": _tree_order(list(g["items"].values()))})
    ranges: dict[str, list[str]] = {}
    for d in dims[:1]:
        for it in d["items"]:
            for c, (s, e) in it["cycles"].items():
                cur = ranges.setdefault(c, [s, e])
                if periods.sort_key(c, s) < periods.sort_key(c, cur[0]):
                    cur[0] = s
                if periods.sort_key(c, e) > periods.sort_key(c, cur[1]):
                    cur[1] = e
    cycles = [{"cycle": c, "label": periods.CYCLE_LABEL[c], "start": ranges[c][0], "end": ranges[c][1]}
              for c in periods.CYCLE_ORDER if c in ranges]
    definition = await ecos.word(client, cat.core_name(row.get("name") or "") or stat_code) if row.get("name") else None
    return {
        "source": "ECOS", "id": stat_code, "org_name": row.get("org_name") or "한국은행",
        "name": row.get("name") or stat_code, "path": row.get("path"), "unit": row.get("unit"),
        "url": "https://ecos.bok.or.kr/#/SearchStat", "dims": dims, "cycles": cycles,
        "info": {"정의(ECOS 통계용어사전)": definition} if definition else {},
        "warnings": [f"ECOS가 알린 항목 수({t:,})와 받은 수({g:,})가 다릅니다. 항목 목록이 정확하지 않을 수 있어 "
                     "이 상세 정보는 저장하지 않고 다음에 다시 받습니다." for t, g in item_notes],
        "notes": [], "source_label": "한국은행 경제통계시스템(ECOS)",
        "ids": {"통계표코드": stat_code},
    }


async def _kosis_detail(client, ds_id: str, row: dict) -> dict:
    org_id, tbl_id = ds_id.split("/", 1)
    try:
        tbl, prd, itm, cmmt, src = await asyncio.gather(
            kosis.meta(client, org_id, tbl_id, "TBL"), kosis.meta(client, org_id, tbl_id, "PRD"),
            kosis.meta(client, org_id, tbl_id, "ITM"), kosis.meta(client, org_id, tbl_id, "CMMT"),
            kosis.meta(client, org_id, tbl_id, "SOURCE"))
    except SourceError as e:
        if e.kind == "bad_request" and "존재하지" in e.message:
            raise SourceError("KOSIS", "not_found", f"KOSIS에 통계표 {ds_id} 가 없습니다.", e.code) from None
        raise
    org = await kosis.meta(client, org_id, tbl_id, "ORG") if not row.get("org_name") else []
    if not itm:
        raise SourceError("KOSIS", "not_found", f"KOSIS에 통계표 {ds_id} 가 없거나 항목 정보가 없습니다(기관ID/통계표ID 확인).")
    dims_by: dict[str, dict] = {}
    for it in itm:
        obj = it.get("OBJ_ID")
        sn = 0 if obj == "ITEM" else int(it.get("OBJ_ID_SN") or 99)
        key = "ITEM" if obj == "ITEM" else f"C{sn}"
        d = dims_by.setdefault(key, {"key": key, "obj_id": obj, "sn": sn,
                                     "name": it.get("OBJ_NM") or ("항목" if obj == "ITEM" else obj), "items": []})
        d["items"].append({"code": it["ITM_ID"], "name": it.get("ITM_NM"), "parent": it.get("UP_ITM_ID"),
                           "unit": _nfkc(it.get("UNIT_NM"))})
    dims = []
    for d in sorted(dims_by.values(), key=lambda x: x["sn"]):
        d["items"] = _tree_order(d["items"])
        dims.append({k: d[k] for k in ("key", "obj_id", "name", "items")})
    cycles = []
    for p in prd:
        c = periods.KOSIS_META_TO_CYCLE.get((p.get("PRD_SE") or "").strip())
        if not c:
            continue
        try:
            s = periods.to_display(c, p["STRT_PRD_DE"])
            e = periods.to_display(c, p["END_PRD_DE"])
        except (periods.PeriodError, KeyError):
            s, e = p.get("STRT_PRD_DE"), p.get("END_PRD_DE")
        cycles.append({"cycle": c, "label": periods.CYCLE_LABEL[c], "start": s, "end": e})
    cycles.sort(key=lambda x: periods.CYCLE_ORDER.index(x["cycle"]))
    info = {}
    if src:
        s0 = src[0]
        if s0.get("JOSA_NM"):
            info["조사명"] = s0["JOSA_NM"]
        if s0.get("DEPT_NM"):
            info["작성 부서"] = s0["DEPT_NM"]
    if row.get("description"):
        info["설명(KOSIS)"] = row["description"]
    notes = [c.get("CMMT_DC").strip() for c in cmmt if (c.get("CMMT_DC") or "").strip()]
    name = (tbl[0].get("TBL_NM") if tbl else None) or row.get("name") or tbl_id
    org_name = row.get("org_name") or (org[0].get("ORG_NM") if org else None)
    units = {it["unit"] for d in dims if d["key"] == "ITEM" for it in d["items"] if it.get("unit")}
    return {
        "source": "KOSIS", "id": ds_id, "org_name": org_name, "name": name, "path": row.get("path"),
        "unit": next(iter(units)) if len(units) == 1 else ("항목별 상이" if units else None),
        "url": f"https://kosis.kr/statHtml/statHtml.do?orgId={org_id}&tblId={tbl_id}",
        "dims": dims, "cycles": cycles, "info": info, "notes": notes,
        "source_label": "KOSIS 국가통계포털", "ids": {"기관ID": org_id, "통계표ID": tbl_id},
    }


# ------------------------------------------------------------------ 조회
def _label(series: list[dict], dim_names: list[str]) -> None:
    """선택이 하나뿐인 차원은 이름에서 빼서 짧게 만든다(모두 같으면 전체 표시)."""
    varying = [n for n in dim_names if len({s["names"].get(n) for s in series}) > 1]
    for s in series:
        use = varying or dim_names
        s["label"] = " | ".join(str(s["names"].get(n)) for n in use if s["names"].get(n))


async def fetch(client: httpx.AsyncClient, con, catalog: cat.Catalog, spec: dict, use_cache: bool = True) -> dict:
    h = spec_hash(spec)
    hit = _results.get(h)
    if use_cache and hit and time.time() - hit[0] < RESULT_TTL:
        return hit[1]
    if spec.get("source") == "EGROUP":           # 기업집단포털(색인에서 표로)
        from . import egroup_query
        result = egroup_query.fetch(con, spec)
        result["hash"] = h
        _remember(h, result)
        return result
    if spec.get("source") == "DART":
        from . import dart_query
        result = await dart_query.fetch(client, con, spec)
        result["hash"] = h
        _remember(h, result)
        return result
    det = await detail(client, con, catalog, spec["source"], spec["id"])
    cycle = spec["cycle"]
    if cycle not in {c["cycle"] for c in det["cycles"]}:
        raise SourceError(spec["source"], "bad_request", f"이 통계표에는 {periods.CYCLE_LABEL.get(cycle, cycle)} 주기 자료가 없습니다.")
    try:
        start = periods.to_display(cycle, spec["start"])
        end = periods.to_display(cycle, spec["end"])
    except (periods.PeriodError, ValueError, KeyError, TypeError) as e:
        raise SourceError(spec["source"], "bad_period", f"기간을 읽을 수 없습니다: {e}") from None
    if periods.sort_key(cycle, start) > periods.sort_key(cycle, end):
        start, end = end, start
    log: list[str] = []
    warnings: list[str] = []
    if spec["source"] == "ECOS":
        series, rows = await _fetch_ecos(client, det, spec, cycle, start, end, log, warnings)
    else:
        series, rows = await _fetch_kosis(client, det, spec, cycle, start, end, log, warnings)
    dim_names = [d["name"] for d in det["dims"]]
    _label(series, dim_names)
    rows.sort(key=lambda r: (r[0], periods.sort_key(cycle, r[1])))
    times = sorted({r[1] for r in rows}, key=lambda t: periods.sort_key(cycle, t))
    result = {
        "spec": {**spec, "start": start, "end": end}, "hash": h,
        "source": det["source"], "source_label": det["source_label"], "id": det["id"], "ids": det["ids"],
        "name": det["name"], "org_name": det["org_name"], "url": det["url"], "path": det.get("path"),
        "cycle": cycle, "cycle_label": periods.CYCLE_LABEL[cycle], "start": start, "end": end,
        "dim_names": dim_names, "series": series, "rows": rows, "times": times,
        "row_count": len(rows), "fetched_at": cat.now_kst(), "requests": log, "warnings": warnings,
        "selection_text": _selection_text(det, spec), "notes": det.get("notes", []),
        "program": f"KR Public Data Hub {VERSION}",
    }
    _remember(h, result)
    return result


def _remember(h: str, result: dict) -> None:
    now = time.time()
    for k in [k for k, (t0, _) in _results.items() if now - t0 > RESULT_TTL]:   # 오래된 결과 버리기
        _results.pop(k, None)
    if len(_results) >= 40:
        _results.pop(min(_results, key=lambda k: _results[k][0]), None)
    _results[h] = (now, result)


def _selection_text(det: dict, spec: dict) -> str:
    parts = []
    for d in det["dims"]:
        sel = (spec.get("sel") or {}).get(d["key"])
        if sel == "ALL" or sel is None:
            parts.append(f"{d['name']}: 전체({len(d['items'])}개)")
        else:
            names = {it["code"]: it["name"] for it in d["items"]}
            shown = [f"{names.get(c, c)}[{c}]" for c in sel[:12]]
            more = f" 외 {len(sel) - 12}개" if len(sel) > 12 else ""
            parts.append(f"{d['name']}: " + ", ".join(shown) + more)
    return " / ".join(parts)


async def _fetch_ecos(client, det, spec, cycle, start, end, log, warnings):
    sel = spec.get("sel") or {}
    lists = []
    for d in det["dims"]:
        v = sel.get(d["key"], "ALL")
        lists.append(["?"] if v == "ALL" or not v else list(v))
    n_combos = 1
    for x in lists:
        n_combos *= len(x)
    if n_combos > MAX_ECOS_CALLS:
        raise SourceError("ECOS", "too_large",
                          f"선택 조합이 {n_combos:,}개라 한 번에 받을 수 없습니다(최대 {MAX_ECOS_CALLS}). "
                          "한 차원은 '전체'로 두거나 선택을 줄여 주세요.")
    combos = list(itertools.product(*lists)) if lists else [()]
    s_e, e_e = periods.to_ecos(cycle, start), periods.to_ecos(cycle, end)
    notes: list = []
    results = await asyncio.gather(*(ecos.search(client, det["id"], cycle, s_e, e_e, list(c), log=log, notes=notes)
                                     for c in combos))
    if notes:
        told, got = sum(t for t, _ in notes), sum(g for _, g in notes)
        if any(g < t for t, g in notes):
            warnings.append(f"ECOS가 알린 건수({told:,})와 받은 건수({got:,})가 달라 추가 범위를 한 번 더 확인했고, 더 받을 자료는 "
                            "없었습니다. ECOS는 '전체' 조회 때 건수를 크게 알리는 일이 있습니다. 중요하면 원본 화면과 대조하세요.")
        else:
            warnings.append(f"ECOS가 알린 건수({told:,})보다 많이({got:,}) 받았습니다. 받은 자료는 모두 담았습니다. 중요하면 원본 화면과 대조하세요.")
    names_by_grp = [d["name"] for d in det["dims"]]
    series_idx: dict[tuple, int] = {}
    series: list[dict] = []
    rows, seen = [], {}
    conflicts = 0
    for res in results:
        for r in res:
            codes = tuple(r.get(f"ITEM_CODE{i}") for i in range(1, len(det["dims"]) + 1))
            key = codes + (r.get("UNIT_NAME"),)
            if key not in series_idx:
                series_idx[key] = len(series)
                series.append({
                    "codes": {det["dims"][i]["key"]: codes[i] for i in range(len(codes))},
                    "names": {names_by_grp[i]: r.get(f"ITEM_NAME{i + 1}") for i in range(len(codes))},
                    "unit": r.get("UNIT_NAME"), "weight": r.get("WGT"),
                })
            try:
                t = periods.to_display(cycle, r["TIME"])
            except periods.PeriodError:
                t = r["TIME"]
            k = (series_idx[key], t)
            v = r.get("DATA_VALUE")
            if k in seen:
                conflicts += seen[k] != v
                continue
            seen[k] = v
            rows.append([series_idx[key], t, v])
    if conflicts:
        warnings.append(f"원천 응답에 같은 시계열·시점의 값이 서로 다르게 {conflicts}번 더 들어 있어 처음 값을 썼습니다. 원본 화면에서 확인하세요.")
    if not rows:
        raise SourceError("ECOS", "no_data", "조건에 맞는 자료가 없습니다(기간·주기·항목을 확인하세요).", "INFO-200")
    return series, rows


async def _fetch_kosis(client, det, spec, cycle, start, end, log, warnings):
    sel = spec.get("sel") or {}
    item_dim = next((d for d in det["dims"] if d["key"] == "ITEM"), None)
    class_dims = [d for d in det["dims"] if d["key"] != "ITEM"]

    def pick(d):
        v = sel.get(d["key"], "ALL")
        return "ALL" if v == "ALL" or not v else list(v)

    itm = pick(item_dim) if item_dim else "ALL"
    objs = [pick(d) for d in class_dims]
    n_cells_per_period = (len(item_dim["items"]) if itm == "ALL" else len(itm)) if item_dim else 1
    for d, o in zip(class_dims, objs):
        n_cells_per_period *= len(d["items"]) if o == "ALL" else len(o)
    n_periods = periods.count(cycle, start, end) if cycle not in ("IR", "F") else 1
    chunks = [(start, end)]
    if n_cells_per_period * n_periods > kosis.CELL_LIMIT:
        per = max(1, kosis.CELL_LIMIT // max(1, n_cells_per_period))
        if n_cells_per_period > kosis.CELL_LIMIT or cycle in ("IR", "F"):
            raise SourceError("KOSIS", "too_large",
                              f"한 시점에 {n_cells_per_period:,}칸이라 KOSIS 한도(4만 칸)를 넘습니다. 분류 선택을 줄여 주세요.")
        chunks = []
        cur = start
        while periods.sort_key(cycle, cur) <= periods.sort_key(cycle, end):
            last = periods.shift(cycle, cur, per - 1)
            if periods.sort_key(cycle, last) > periods.sort_key(cycle, end):
                last = end
            chunks.append((cur, last))
            cur = periods.shift(cycle, last, 1)
        warnings.append(f"KOSIS 한도(4만 칸) 때문에 기간을 {len(chunks)}번에 나눠 받았습니다.")
    prd_se = periods.CYCLE_TO_KOSIS_PRDSE[cycle]
    results = []
    for a, b in chunks:   # 차례로(한도 오류를 피하려고)
        results.append(await kosis.data(client, *det["id"].split("/", 1), itm, objs, prd_se,
                                        periods.to_kosis(cycle, a), periods.to_kosis(cycle, b), log=log))
    dim_keys = [d["key"] for d in class_dims]
    series_idx: dict[tuple, int] = {}
    series: list[dict] = []
    rows, seen = [], {}
    conflicts = 0
    item_name = item_dim["name"] if item_dim else "항목"
    for res in results:
        for r in res:
            ccodes = tuple(r.get(f"C{i}") for i in range(1, len(class_dims) + 1))
            key = (r.get("ITM_ID"),) + ccodes
            if key not in series_idx:
                series_idx[key] = len(series)
                names = {item_name: r.get("ITM_NM")}
                for i, d in enumerate(class_dims, start=1):
                    names[d["name"]] = r.get(f"C{i}_NM")
                codes = {"ITEM": r.get("ITM_ID")}
                codes.update({dim_keys[i]: ccodes[i] for i in range(len(ccodes))})
                series.append({"codes": codes, "names": names, "unit": _nfkc(r.get("UNIT_NM")),
                               "last_changed": r.get("LST_CHN_DE")})
            try:
                t = periods.to_display(cycle, r["PRD_DE"])
            except periods.PeriodError:
                t = r["PRD_DE"]
            k = (series_idx[key], t)
            v = r.get("DT")
            if k in seen:
                conflicts += seen[k] != v
                continue
            seen[k] = v
            rows.append([series_idx[key], t, v])
    if conflicts:
        warnings.append(f"원천 응답에 같은 시계열·시점의 값이 서로 다르게 {conflicts}번 더 들어 있어 처음 값을 썼습니다. 원본 화면에서 확인하세요.")
    if not rows:
        raise SourceError("KOSIS", "no_data", "조건에 맞는 자료가 없습니다(기간·주기·항목을 확인하세요).", "30")
    return series, rows
