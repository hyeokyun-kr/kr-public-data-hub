"""KR Public Data Hub 웹 서버(FastAPI). 이 컴퓨터(127.0.0.1)에서만 열린다.

    python3 -m datahub.server          # http://127.0.0.1:8765
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import re
import time
from urllib.parse import quote

import httpx
from fastapi import Body, FastAPI, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import catalog as cat
from . import ai_recommend, dart_query, decision_ai, decisions, egroup_query, exporter, periods, query, synonyms
from . import pivot as pv
from .build_catalog import kosis_row_from_search
from .config import STATIC_DIR, VERSION, get_api_key
from .errors import SourceError
from .sources import HEADERS, TIMEOUT, datago, kosis, rate_wait

STATUS_BY_KIND = {"auth": 401, "no_data": 404, "not_found": 404, "bad_period": 400, "bad_request": 400, "too_large": 413, "rate_limit": 429,
                  "server": 502, "network": 504}
LIVE_TTL = 24 * 3600
PREVIEW_ROW_LIMIT = 50000
SEARCH_SHOW = 60

state: dict = {}


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # 큰 색인을 읽는 데 느린 디스크에서는 처음에 10~20초 걸린다. 서버는 바로 응답하게 하고 색인은 뒤에서 읽는다.
    state["con"] = cat.connect()
    state["catalog"] = None
    state["client"] = httpx.AsyncClient()
    state["dart_task"] = None
    state["dart_error"] = None
    # 회사 목록 날짜 확인은 큰 색인을 읽기 시작하기 전에 한다. 디스크가 바쁠 때 큰 색인 읽기와 겹치면
    # 이 작은 읽기가 몇 분씩 밀려 서버가 아예 응답을 못 한다(2026-11-17 켜는 데 7분 걸림).
    dart_due = bool(get_api_key("DART")) and dart_query.corps_stale(state["con"])

    async def load():
        state["catalog"] = await asyncio.to_thread(cat.Catalog)

    state["loading"] = asyncio.create_task(load())
    # 의결서 색인도 바로 함께 읽는다(통계 색인 다음으로 미뤘더니 디스크가 느릴 때 의결서 탭이 오래 '쓸 수 없음'·필터 빈칸, 11/17 되돌림)
    state["dec_loading"] = asyncio.create_task(asyncio.to_thread(decisions.load))    # 없으면 탭만 안내
    if dart_due:
        start_dart_refresh()      # 회사 목록이 없거나 일주일 넘었으면 뒤에서 새로 받는다(키 말고 보내는 것 없음)
    yield
    await state["client"].aclose()
    await decisions.close()
    state["con"].close()


app = FastAPI(title="KR Public Data Hub", version=VERSION, lifespan=lifespan)


@app.middleware("http")
async def no_cache_static(request, call_next):
    """화면 파일을 고치면 브라우저가 바로 새 것을 쓰게 한다."""
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


async def catalog() -> cat.Catalog:
    """색인이 다 읽힐 때까지 기다린다(처음 켠 직후의 요청)."""
    if state.get("catalog") is None:
        await state["loading"]
    return state["catalog"]


def start_dart_refresh() -> None:
    t = state.get("dart_task")
    if t is not None and not t.done():
        return

    async def run():
        try:
            await dart_query.refresh_corps(state["client"], state["con"])
            state["dart_error"] = None
        except SourceError as e:
            state["dart_error"] = e.to_dict()

    state["dart_task"] = asyncio.create_task(run())


def error_response(e: SourceError) -> JSONResponse:
    return JSONResponse({"error": e.to_dict()}, status_code=STATUS_BY_KIND.get(e.kind, 500))


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/status")
async def status():
    con = state["con"]
    info = dict(con.execute("SELECT k, v FROM build_info").fetchall())
    return {
        "version": VERSION,
        "keys": {s: bool(get_api_key(s)) for s in ("KOSIS", "ECOS", "DART", "AI", "OPENAI", "PUBLIC")},
        "egroup": {k: v for k, v in egroup_query.info(con).items() if k != "yms"},
        "dart": {**dart_query.corps_info(con), "refreshing": bool(state.get("dart_task") and not state["dart_task"].done()),
                 "error": state.get("dart_error"), "industry": dart_query.industry_info(con), "industry_job": _ind_job()},
        "ready": state.get("catalog") is not None,
        "decision": decisions.status(),
        "catalog": state["catalog"].stats() if state.get("catalog") is not None else {},
        "built": info,
    }


async def _live_kosis(terms: list[str]) -> dict:
    """KOSIS 통합검색을 바로 불러 카탈로그를 보강한다(같은 말은 하루에 한 번만)."""
    con = state["con"]
    todo = []
    for t in terms:
        r = con.execute("SELECT fetched_at FROM search_cache WHERE source='KOSIS' AND term=?", (cat.norm(t),)).fetchone()
        if r:
            try:
                age = time.time() - time.mktime(time.strptime(r[0], "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                age = LIVE_TTL + 1
            if age < LIVE_TTL:
                continue
        todo.append(t)
    if not todo:
        return {"status": "cached"}
    client = state["client"]
    try:
        res = await asyncio.wait_for(asyncio.gather(*(kosis.search(client, t, 50) for t in todo),
                                                    return_exceptions=True), timeout=12)
    except asyncio.TimeoutError:
        return {"status": "timeout", "message": "KOSIS 검색 응답이 늦어 로컬 색인 결과만 보여 줍니다."}
    errors = []
    for t, rows in zip(todo, res):
        if isinstance(rows, SourceError):
            errors.append(rows.to_dict())
            continue
        if isinstance(rows, Exception):
            errors.append({"kind": "server", "label": "오류", "message": str(rows)})
            continue
        for r in rows:
            row = kosis_row_from_search(r)
            cat.upsert_dataset(con, row, keep_richer=True)
            merged = con.execute("SELECT * FROM datasets WHERE source=? AND ds_id=?", (row["source"], row["ds_id"])).fetchone()
            (await catalog()).add(dict(merged))
        con.execute("INSERT OR REPLACE INTO search_cache VALUES ('KOSIS', ?, ?)", (cat.norm(t), cat.now_kst()))
    con.commit()
    if errors:
        return {"status": "error", "errors": errors}
    return {"status": "ok", "terms": todo}


@app.get("/api/search")
async def search(q: str = Query(..., min_length=1, max_length=80), source: str | None = None,
                 cycle: str | None = None, recent: int = 0, syn: int = 1, live: int = 1, offset: int = 0):
    q = " ".join(q.split())
    terms = synonyms.expand(q) if syn else [(q, "입력어", 1.0)]
    live_info = {"status": "skipped"}
    if live and source in (None, "", "KOSIS") and get_api_key("KOSIS"):
        live_terms = [t for t, why, w in terms if w >= 0.9][:3]
        live_info = await _live_kosis(live_terms)
    pins = synonyms.pins_for(q) if syn else []
    hits = (await catalog()).search(terms, source=source or None, cycle=cycle or None, recent_only=bool(recent), pins=pins,
                                   limit=100000)
    rel = synonyms.related_info(q) if syn else {"synonyms": [], "related": []}
    offset = max(0, offset)
    shown = hits[offset:offset + SEARCH_SHOW]      # '더 보기'는 offset 으로 다음 60개
    return {"query": q, "expansion": rel, "live": live_info, "count": len(hits), "shown": len(shown), "offset": offset,
            "results": [cat.hit_to_dict(h) for h in shown]}


@app.get("/api/detail")
async def detail(source: str, id: str):
    try:
        d = await query.detail(state["client"], state["con"], await catalog(), source, id)
    except SourceError as e:
        return error_response(e)
    d = dict(d)
    try:
        (await catalog()).update_ranges(source, id, {c["cycle"]: [c["start"], c["end"]] for c in d["cycles"]})
    except Exception:
        pass          # 색인 갱신이 안 돼도 상세 화면은 보여 준다
    cycles = []
    for c in d["cycles"]:
        c = dict(c)
        try:
            c["default_start"], c["default_end"] = periods.default_range(c["cycle"], c["start"], c["end"])
        except (periods.PeriodError, KeyError):
            c["default_start"], c["default_end"] = c.get("start"), c.get("end")
        cycles.append(c)
    d["cycles"] = cycles
    return d


def _preview_payload(res: dict) -> dict:
    rows = res["rows"]
    trimmed = len(rows) > PREVIEW_ROW_LIMIT
    keys = ("spec", "hash", "source", "source_label", "id", "ids", "name", "org_name", "url", "cycle", "cycle_label",
            "start", "end", "dim_names", "series", "times", "row_count", "fetched_at", "requests", "warnings",
            "selection_text", "notes")
    out = {k: res[k] for k in keys}
    for k in ("kind", "layout", "table", "extra_prov"):      # DART 재무제표·공시 목록
        if k in res:
            out[k] = res[k]
    out["rows"] = rows[-PREVIEW_ROW_LIMIT:] if trimmed else rows
    out["trimmed"] = trimmed
    return out


@app.post("/api/preview")
async def preview(spec: dict = Body(...)):
    try:
        res = await query.fetch(state["client"], state["con"], await catalog(), spec)
    except SourceError as e:
        return error_response(e)
    return _preview_payload(res)


def _file(content: bytes, name: str, media: str) -> Response:
    return Response(content, media_type=media, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        "Access-Control-Expose-Headers": "Content-Disposition",
    })


@app.post("/api/download")
async def download(payload: dict = Body(...)):
    spec, fmt, shape = payload["spec"], payload.get("format", "xlsx"), payload.get("shape", "long")
    try:
        res = await query.fetch(state["client"], state["con"], await catalog(), spec)
        res = with_pivot(res, payload.get("pivot"))
    except SourceError as e:
        return error_response(e)
    exporter.log_download([res], fmt)
    if fmt == "csv":
        suffix = "_가로형" if shape == "wide" else ""
        return _file(exporter.csv_bytes(res, shape), exporter.base_name(res) + suffix + ".csv", "text/csv; charset=utf-8")
    return _file(exporter.xlsx_bytes(res), exporter.base_name(res) + ".xlsx",
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def with_pivot(res: dict, pivot: dict | None) -> dict:
    """사용자가 고른 행·열 배치를 결과에 붙인다(캐시된 결과는 건드리지 않게 얕은 복사)."""
    if not pivot or res.get("layout") == "table":
        return res
    try:
        p = pv.validate(res, pivot)
    except ValueError as e:
        raise SourceError(res.get("source") or "-", "bad_request", str(e)) from None
    return {**res, "pivot": p}


@app.post("/api/download/zip")
async def download_zip(payload: dict = Body(...)):
    specs = payload.get("specs") or []
    if not specs:
        return error_response(SourceError("-", "bad_request", "다운로드 목록이 비어 있습니다."))
    results, errors = [], []
    for s in specs:   # 차례로(기관 호출 한도를 지키려고)
        try:
            results.append(with_pivot(await query.fetch(state["client"], state["con"], await catalog(), s), s.get("pivot")))
        except SourceError as e:
            errors.append({"name": s.get("name") or s.get("id"), **e.to_dict()})
    if not results:
        return JSONResponse({"error": {"kind": "no_data", "label": "받은 자료 없음", "message": "모든 항목 조회에 실패했습니다.",
                                       "details": errors}}, status_code=502)
    exporter.log_download(results, "zip")
    stamp = cat.now_kst()[:16].replace("-", "").replace(":", "").replace(" ", "_")
    resp = _file(exporter.zip_bytes(results), f"KR_DataHub_{len(results)}건_{stamp}.zip", "application/zip")
    if errors:
        resp.headers["X-DataHub-Failed"] = quote(", ".join(f"{e['name']}({e['label']})" for e in errors))
        resp.headers["Access-Control-Expose-Headers"] += ", X-DataHub-Failed"
    return resp


@app.get("/api/dart/search")
async def dart_search(q: str = Query(..., min_length=1, max_length=80), listed: int = 0):
    info = dart_query.corps_info(state["con"])
    if not info["count"]:
        if not get_api_key("DART"):
            return error_response(SourceError("DART", "auth", "OpenDART 인증키가 없어 회사 목록을 받을 수 없습니다."))
        start_dart_refresh()
        t = state["dart_task"]
        try:
            await asyncio.wait_for(asyncio.shield(t), timeout=60)
        except asyncio.TimeoutError:
            return error_response(SourceError("DART", "server", "회사 목록(약 12만 개)을 받는 중입니다. 잠시 뒤 다시 찾아 보세요."))
        if state.get("dart_error"):
            e = state["dart_error"]
            return error_response(SourceError("DART", e["kind"], "회사 목록을 받지 못했습니다: " + e["message"], e.get("code")))
    res = dart_query.search_corps(state["con"], q, listed_only=bool(listed))
    res["query"] = q
    res["groups"] = egroup_query.search_groups(state["con"], q) if len(q.strip()) >= 2 else []   # 이름이 맞는 기업집단
    res["corps"] = dart_query.corps_info(state["con"])
    return res


@app.get("/api/dart/company")
async def dart_company(corp: str):
    try:
        co = await dart_query.company(state["client"], state["con"], corp)
    except SourceError as e:
        return error_response(e)
    return {**co, "egroup": egroup_query.for_company(state["con"], co.get("jurir_no"))}   # 법인등록번호로 소속 기업집단


@app.get("/api/egroup/group")
async def egroup_group(code: str, ym: str | None = None):
    try:
        return egroup_query.group(state["con"], code, ym)
    except SourceError as e:
        return error_response(e)


@app.get("/api/egroup/search")
async def egroup_search(q: str = Query(..., min_length=1, max_length=40)):
    return {"query": q, "groups": egroup_query.search_groups(state["con"], q), "info": egroup_query.info(state["con"])}


@app.get("/api/dart/industry")
async def dart_industry(corp: str):
    """회사의 업종코드 단계별(세세분류→중분류) 이름과 상장사 수."""
    try:
        co = await dart_query.company(state["client"], state["con"], corp)
    except SourceError as e:
        return error_response(e)
    return {"code": co.get("induty_code"), "levels": dart_query.industry_levels(state["con"], co.get("induty_code")),
            "info": dart_query.industry_info(state["con"]), "job": _ind_job()}


def _ind_job() -> dict:
    t = state.get("ind_task")
    return {"running": bool(t and not t.done()), "done": state.get("ind_done", 0), "total": state.get("ind_total", 0),
            "error": state.get("ind_error")}


@app.post("/api/dart/industry/refresh")
async def dart_industry_refresh():
    """상장사 업종코드 받기(기업개황 약 4천 번, 15분 안팎). 뒤에서 돌고 진행 상황은 /api/dart/industry 의 job."""
    if not get_api_key("DART"):
        return error_response(SourceError("DART", "auth", "OpenDART 인증키가 없습니다."))
    t = state.get("ind_task")
    if t is None or t.done():
        state["ind_error"] = None
        state["ind_done"], state["ind_total"] = 0, 0

        def prog(d, total):
            state["ind_done"], state["ind_total"] = d, total

        async def run():
            try:
                await dart_query.refresh_industry(state["client"], state["con"], progress=prog)
            except SourceError as e:
                state["ind_error"] = e.to_dict()

        state["ind_task"] = asyncio.create_task(run())
    return _ind_job()


@app.get("/api/datago/file")
async def datago_file(id: str):
    """공공데이터포털 파일데이터의 원본 파일을 포털이 화면에서 쓰는 순서대로 받아 그대로 넘긴다(내용 변경 없음)."""
    client = state["client"]
    try:
        info = await datago.file_info(client, id)
        await datago.check_limit(client, info)
        await rate_wait("DATAGO")
        resp = await client.send(client.build_request("GET", datago.download_url(info), headers=HEADERS, timeout=TIMEOUT),
                                 stream=True)
    except SourceError as e:
        return error_response(e)
    except (httpx.TimeoutException, httpx.TransportError):
        return error_response(SourceError("DATAGO", "network", "공공데이터포털에 연결하지 못했습니다."))
    ctype = resp.headers.get("content-type", "")
    if resp.status_code != 200 or "text/html" in ctype:     # 오류 화면(HTML)을 파일로 넘기지 않는다
        await resp.aclose()
        kind = {403: "rate_limit", 429: "rate_limit", 404: "not_found"}.get(resp.status_code, "server")
        return error_response(SourceError("DATAGO", kind, f"공공데이터포털이 파일 대신 다른 응답을 주었습니다(HTTP {resp.status_code}). 원본 페이지에서 받으세요.",
                                          str(resp.status_code)))
    name = info["file_name"]
    if not name:                       # 원래 파일 이름이 없으면 포털 응답 머리글의 확장자를 쓴다(예: filename="x.csv")
        m = re.search(r'\.([A-Za-z0-9]{1,8})"?\s*(?:;|$)', resp.headers.get("content-disposition", ""))
        ext = (m.group(1) if m else info["ext"] or "bin").lower()
        name = f"{info['data_name']}.{ext}"
    exporter.log_download([{"source": "DATAGO", "name": info["data_name"], "id": id, "cycle_label": "원본 파일", "start": "",
                            "end": "", "selection_text": name, "row_count": info.get("rows") or "", "fetched_at": cat.now_kst()}],
                          "원본파일")

    async def body():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
               "Access-Control-Expose-Headers": "Content-Disposition"}
    if resp.headers.get("content-length"):
        headers["Content-Length"] = resp.headers["content-length"]
    return StreamingResponse(body(), media_type="application/octet-stream", headers=headers)


@app.post("/api/ai/recommend")
async def ai_recommend_api(payload: dict = Body(...)):
    """자연어 질문 → 색인 속 자료 우선순위 추천(숫자는 말하지 않음). 질문은 Claude API로 나간다."""
    try:
        return await ai_recommend.recommend(str(payload.get("question") or ""), await catalog(),
                                            model=payload.get("model") or None)
    except SourceError as e:
        return error_response(e)


@app.get("/api/ai/models")
async def ai_models():
    return {"models": ai_recommend.models_info(), "default": ai_recommend.DEFAULT_MODEL}


@app.get("/api/decision/models")
async def decision_models():
    """의결서 AI 질문에서 고를 수 있는 모델(AI 추천보다 좁음: Haiku 뺌, 기본 GPT-6.1 Sol)."""
    return {"models": ai_recommend.models_info(decision_ai.DEC_MODELS, decision_ai.DEC_DEFAULT),
            "default": decision_ai.DEC_DEFAULT}


# ------------------------------------------------------------------ 공정위 의결서(색인 파일 하나, 검색은 이 안에서만)
async def _dec() -> decisions.Index:
    # 뒤에서 읽는 중이면 같은 잠금(decisions._lock)에서 기다렸다가 읽힌 색인을 쓴다.
    return await asyncio.to_thread(decisions.need)


def _multi(s: str | None) -> list[str]:
    return [x for x in (s or "").split("|") if x]


@app.get("/api/decision/search")
async def decision_search(q: str = Query("", max_length=200), type: str = "", act: str = "", y1: int = 0, y2: int = 0,
                          sort: str = "date", offset: int = 0, snip: int = 1):
    """의결서 찾기. 위반유형·조치유형은 '|'로 여러 개. snip=0 이면 앞뒤 문장 없이(화면은 /api/decision/snippets 로 채움)."""
    try:
        idx = await _dec()
        return await asyncio.to_thread(idx.search, q, _multi(type), _multi(act), y1 or None, y2 or None, sort,
                                       max(0, offset), decisions.PAGE_SIZE, bool(snip))
    except SourceError as e:
        return error_response(e)


@app.get("/api/decision/snippets")
async def decision_snippets(q: str = Query("", max_length=200), ids: str = ""):
    try:
        idx = await _dec()
        nums = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        return await asyncio.to_thread(idx.snippets, q, nums)
    except SourceError as e:
        return error_response(e)


@app.get("/api/decision/doc")
async def decision_doc(id: int, q: str = Query("", max_length=200)):
    try:
        idx = await _dec()
        return await asyncio.to_thread(idx.doc, id, q)
    except SourceError as e:
        return error_response(e)


@app.get("/api/decision/page")
async def decision_page(id: int, p: int, q: str = Query("", max_length=200)):
    try:
        idx = await _dec()
        return await asyncio.to_thread(idx.page, id, p, q)
    except SourceError as e:
        return error_response(e)


@app.post("/api/decision/ask")
async def decision_ask(payload: dict = Body(...)):
    """의결서 AI 질문: 질문·찾을 말·고른 쪽 본문이 고른 AI(Claude·OpenAI)로 나간다. 근거 번호 붙은 답."""
    def num(v):
        try:
            return int(v) or None
        except (TypeError, ValueError):
            return None
    try:
        await _dec()
        return await decision_ai.ask(str(payload.get("question") or ""), model=payload.get("model") or None,
                                     types=_multi(payload.get("type")), acts=_multi(payload.get("act")),
                                     y1=num(payload.get("y1")), y2=num(payload.get("y2")))
    except SourceError as e:
        return error_response(e)


@app.get("/api/decision/pdf")
async def decision_pdf(id: int):
    """원문 PDF. 받아 둔 파일이 있으면 그것을, 없으면 공정위 사이트에서 그 한 건만 받아 브라우저에서 연다."""
    try:
        idx = await _dec()
        d = idx.get(id)
        name = decisions.pdf_name(d)
        disp = f"inline; filename*=UTF-8''{quote(name)}"
        local = idx.local_pdf(d)
        if local:
            return FileResponse(local, media_type="application/pdf", headers={"Content-Disposition": disp})
        body = await decisions.fetch_pdf(d["fileId"], d["fileSn"])
    except SourceError as e:
        # 새 탭에서 여는 주소라 JSON 대신 읽을 수 있는 안내 화면을 준다
        link = ""
        idx = decisions.current()
        if idx is not None and id in idx.docs and idx.docs[id]["사건번호"]:
            u = decisions.site_url(idx.docs[id]["사건번호"].split(", ")[0])
            link = f'<p><a href="{html.escape(u)}">공정위 사이트에서 이 사건 찾기</a></p>'
        return HTMLResponse(f'<!doctype html><meta charset="utf-8"><title>원문 PDF</title><body style="font-family:sans-serif;padding:24px">'
                            f'<h3>원문 PDF를 열지 못했습니다</h3><p>{html.escape(e.message)}</p>{link}</body>',
                            status_code=STATUS_BY_KIND.get(e.kind, 500))
    return Response(body, media_type="application/pdf", headers={"Content-Disposition": disp})


@app.get("/api/synonyms")
async def synonym_list():
    return {"groups": synonyms.all_groups()}


def main():
    import argparse
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
