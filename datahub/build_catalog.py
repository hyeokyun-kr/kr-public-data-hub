"""검색용 카탈로그를 만든다(처음 한 번, 이후 한 달에 한 번쯤 다시).

    python3 -m datahub.build_catalog            # ECOS + KOSIS 모두
    python3 -m datahub.build_catalog --only ecos
    python3 -m datahub.build_catalog --only kosis
    python3 -m datahub.build_catalog --only ecos-missing   # 항목을 못 받은 ECOS 표만 다시
    python3 -m datahub.build_catalog --only kosis --max-calls 16000   # KOSIS 목록을 끝까지(약 1시간 40분)
    python3 -m datahub.build_catalog --only dart    # OpenDART 회사 목록(약 12만 개, 호출 1번). 서버도 일주일마다 알아서 받음
    python3 -m datahub.build_catalog --only dart-industry   # 상장사 업종코드(기업개황 약 4천 번, 15분 안팎). 업종 비교에 필요
    python3 -m datahub.build_catalog --only ksic    # 업종코드 → 업종명 표(KOSIS 호출 1번)
    python3 -m datahub.build_catalog --only datago  # 공공데이터포털 목록(목록개방현황 CSV 약 150MB, 한 달에 한 번쯤)
    python3 -m datahub.build_catalog --only egroup  # 공정위 기업집단포털(지정 집단·소속회사·변경내역, 호출 약 130번)
2026-11-13 첫 구축: KOSIS 8,000번 상한에서 통계표 88,051개(+통합검색 보강 → 89,572개), 깊은 폴더 6,423개는 남김.
ECOS는 3분에 300회를 넘게 부르면 30분 동안 막히므로 호출 속도를 제한한다(sources/__init__.py).

- ECOS: 통계표 목록 전체 + 조회 가능한 통계표(SRCH_YN=Y)마다 항목 목록
- KOSIS: '국내통계 주제별' 목록 트리 전체(통계표 이름·경로·최종 갱신일)
         + 동의어 사전 대표어로 통합검색(설명·수록기간·분류명 보강)
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import time

import httpx

from . import catalog as cat
from . import periods
from .errors import SourceError
from .sources import ecos, kosis
from .synonyms import all_groups


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ------------------------------------------------------------------ ECOS
def ecos_path(code: str, tables: dict) -> str:
    names = []
    cur = tables.get(code, {}).get("P_STAT_CODE")
    while cur and cur in tables and cur != "*":
        names.append(cat.strip_number(tables[cur]["STAT_NAME"]))
        cur = tables[cur].get("P_STAT_CODE")
    return " > ".join(reversed(names))


def ecos_ranges(items: list[dict]) -> tuple[dict, int | None, str | None]:
    ranges: dict[str, list[str]] = {}
    units = collections.Counter()
    end_year = None
    for it in items:
        c = it.get("CYCLE")
        if c not in periods.CYCLE_LABEL:
            continue
        try:
            s = periods.to_display(c, it["START_TIME"])
            e = periods.to_display(c, it["END_TIME"])
        except (periods.PeriodError, KeyError, TypeError):
            continue
        cur = ranges.get(c)
        if cur is None:
            ranges[c] = [s, e]
        else:
            if periods.sort_key(c, s) < periods.sort_key(c, cur[0]):
                cur[0] = s
            if periods.sort_key(c, e) > periods.sort_key(c, cur[1]):
                cur[1] = e
        y = int(e[:4])
        end_year = max(end_year or 0, y)
        if it.get("UNIT_NAME"):
            units[it["UNIT_NAME"]] += 1
    unit = None
    if len(units) == 1:
        unit = next(iter(units))
    elif units:
        unit = "항목별 상이(" + ", ".join(u for u, _ in units.most_common(3)) + ")"
    return ranges, end_year, unit


async def build_ecos(client: httpx.AsyncClient, con, missing_only: bool = False) -> None:
    log("ECOS 통계표 목록 받는 중")
    tnotes: list = []
    rows = await ecos.table_list(client, notes=tnotes)
    if tnotes:
        log(f"  주의: ECOS가 알린 통계표 수와 받은 수가 다름 {tnotes}")
    tables = {r["STAT_CODE"]: r for r in rows}
    targets = [r for r in rows if r.get("SRCH_YN") == "Y"]
    if missing_only:
        have = {x[0] for x in con.execute("SELECT DISTINCT stat_code FROM ecos_items")}
        targets = [r for r in targets if r["STAT_CODE"] not in have]
    log(f"ECOS 통계표 {len(rows)}개 중 조회 가능 {len(targets)}개 → 항목 목록 받는 중")
    done = 0
    fails = []

    async def one(r):
        nonlocal done
        code = r["STAT_CODE"]
        inotes: list = []
        try:
            items = await ecos.item_list(client, code, notes=inotes)
            if inotes:     # 알린 수와 받은 수가 다르면 저장하지 않는다 → 상세에서 새로 받아 경고, ecos-missing이 다시 받음
                fails.append((code, f"항목 수 차이(알림, 받음)={inotes}, 저장 안 함"))
                items = []
        except SourceError as e:
            fails.append((code, e.message))
            items = []
        done += 1
        if done % 50 == 0:
            log(f"  ECOS 항목 {done}/{len(targets)}")
        return r, items

    results = await asyncio.gather(*(one(r) for r in targets))
    if not missing_only:
        con.execute("DELETE FROM ecos_items")
        con.execute("DELETE FROM datasets WHERE source='ECOS'")
        con.execute("DELETE FROM meta_cache WHERE source='ECOS'")   # 예전 상세가 새 항목 목록을 가리지 않게
    for r, items in results:
        code = r["STAT_CODE"]
        # 다시 받은 표는 저장 여부와 관계없이 예전 상세 캐시를 버린다(덜 온 표는 상세에서 새로 받아 경고)
        con.execute("DELETE FROM meta_cache WHERE source='ECOS' AND ds_id=?", (code,))
        if missing_only and not items:
            continue
        con.execute("DELETE FROM ecos_items WHERE stat_code=?", (code,))
        ranges, end_year, unit = ecos_ranges(items)
        cat.upsert_dataset(con, {
            "source": "ECOS", "ds_id": code, "org_id": "BOK", "org_name": cat.clean_org(r.get("ORG_NAME")) or "한국은행",
            "name": cat.strip_number(r["STAT_NAME"]), "path": ecos_path(code, tables),
            "ranges": ranges, "end_year": end_year, "unit": unit,
            "url": "https://ecos.bok.or.kr/#/SearchStat", "origin": "ecos_list",
        }, keep_richer=False)
        con.executemany(
            "INSERT INTO ecos_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(code, it.get("GRP_CODE"), it.get("GRP_NAME"), it.get("ITEM_CODE"), it.get("ITEM_NAME"),
              it.get("P_ITEM_CODE"), it.get("P_ITEM_NAME"), it.get("CYCLE"), it.get("START_TIME"), it.get("END_TIME"),
              it.get("UNIT_NAME"), it.get("DATA_CNT"), it.get("WEIGHT")) for it in items])
    con.execute("INSERT OR REPLACE INTO build_info VALUES ('ecos_built_at', ?)", (cat.now_kst(),))
    con.commit()
    n_items = con.execute("SELECT COUNT(*) FROM ecos_items").fetchone()[0]
    log(f"ECOS 끝: 통계표 {len(results)}개, 항목×주기 {n_items}줄, 실패 {len(fails)}개 {fails[:3]}")


# ------------------------------------------------------------------ KOSIS
def kosis_row_from_list(r: dict, path: str) -> dict:
    send = r.get("SEND_DE") or ""
    return {
        "source": "KOSIS", "ds_id": f"{r['ORG_ID']}/{r['TBL_ID']}", "org_id": r["ORG_ID"],
        "name": r["TBL_NM"], "path": path, "ranges": {},
        "end_year": int(send[:4]) if send[:4].isdigit() else None,
        "url": f"https://kosis.kr/statHtml/statHtml.do?orgId={r['ORG_ID']}&tblId={r['TBL_ID']}",
        "vw_cd": r.get("VW_CD"), "stat_id": r.get("STAT_ID"), "origin": "kosis_tree",
    }


def kosis_row_from_search(r: dict) -> dict:
    s, e = (r.get("STRT_PRD_DE") or "").strip(), (r.get("END_PRD_DE") or "").strip()
    end_year = int(e[:4]) if e[:4].isdigit() else None
    return {
        "source": "KOSIS", "ds_id": f"{r['ORG_ID']}/{r['TBL_ID']}", "org_id": r["ORG_ID"], "org_name": r.get("ORG_NM"),
        "name": r["TBL_NM"], "path": (r.get("MT_ATITLE") or "").strip(),
        "ranges": {"*": [s, e]} if s and e else {}, "end_year": end_year,
        "description": (r.get("ITEM03") or "").strip() or None, "keywords": (r.get("CONTENTS") or "").strip() or None,
        "url": r.get("LINK_URL") or f"https://kosis.kr/statHtml/statHtml.do?orgId={r['ORG_ID']}&tblId={r['TBL_ID']}",
        "vw_cd": r.get("VW_CD"), "stat_id": r.get("STAT_ID"), "origin": "kosis_search",
    }


async def build_kosis_tree(client: httpx.AsyncClient, con, vw_cd: str = "MT_ZTITLE", max_calls: int = 8000) -> None:
    log(f"KOSIS 목록 트리({vw_cd}) 훑는 중")
    queue: list[tuple[str, str]] = [("", "")]
    calls = 0
    tables: dict[str, dict] = {}
    fails = []
    next_log = 200
    while queue and calls < max_calls:
        batch, queue = queue[:16], queue[16:]

        async def one(pid_path):
            pid, path = pid_path
            try:
                return pid_path, await kosis.list_children(client, vw_cd, pid)
            except SourceError as e:
                fails.append((pid, e.kind, e.message))
                return pid_path, []

        res = await asyncio.gather(*(one(x) for x in batch))
        calls += len(batch)
        for (pid, path), rows in res:
            for r in rows:
                if r.get("TBL_ID"):
                    tables[f"{r['ORG_ID']}/{r['TBL_ID']}"] = kosis_row_from_list(r, path)
                elif r.get("LIST_ID"):
                    name = (r.get("LIST_NM") or "").strip()
                    queue.append((r["LIST_ID"], f"{path} > {name}" if path else name))
        if calls >= next_log:
            next_log += 200
            log(f"  KOSIS 목록 호출 {calls}번, 통계표 {len(tables)}개, 남은 폴더 {len(queue)}개, 실패 {len(fails)}개")
    for row in tables.values():
        cat.upsert_dataset(con, row, keep_richer=True)
    con.execute("INSERT OR REPLACE INTO build_info VALUES ('kosis_tree_built_at', ?)", (cat.now_kst(),))
    con.commit()
    log(f"KOSIS 목록 끝: 호출 {calls}번, 통계표 {len(tables)}개, 남은 폴더 {len(queue)}개, 실패 {len(fails)}개 {fails[:3]}")


async def build_kosis_seed(client: httpx.AsyncClient, con) -> None:
    terms = []
    for g in all_groups():
        terms.append(g["head"])
        terms.extend(g["same"][:2])
    terms = list(dict.fromkeys(terms))
    log(f"KOSIS 통합검색으로 보강: 검색어 {len(terms)}개")
    n = 0
    for i in range(0, len(terms), 6):
        chunk = terms[i:i + 6]
        res = await asyncio.gather(*(kosis.search(client, t, 50) for t in chunk), return_exceptions=True)
        for t, rows in zip(chunk, res):
            if isinstance(rows, Exception):
                log(f"  검색 실패 '{t}': {getattr(rows, 'message', rows)}")
                continue
            for r in rows:
                cat.upsert_dataset(con, kosis_row_from_search(r), keep_richer=True)
                n += 1
            con.execute("INSERT OR REPLACE INTO search_cache VALUES ('KOSIS', ?, ?)", (cat.norm(t), cat.now_kst()))
        con.commit()
    log(f"KOSIS 보강 끝: 검색 결과 {n}건 반영")


async def main(only: str | None, max_calls: int = 8000) -> None:
    con = cat.connect()
    async with httpx.AsyncClient() as client:
        if only == "dart":
            from . import dart_query
            n = await dart_query.refresh_corps(client, con)
            log(f"OpenDART 회사 목록 {n:,}개 저장")
        if only == "dart-industry":
            from . import dart_query
            res = await dart_query.refresh_industry(client, con, progress=lambda d, t: d % 200 == 0 and log(f"  기업개황 {d}/{t}"))
            log(f"상장사 업종코드: {res}")
        if only == "egroup":
            from . import egroup_query
            res = await egroup_query.refresh(client, con, progress=lambda i, n, ym, g, a: log(f"  {ym}: 집단 {g}, 소속회사 {a} ({i}/{n})"))
            log(f"기업집단포털: {res}")
        if only == "datago":
            from . import datago_query
            log(f"공공데이터포털 목록: {await datago_query.refresh(client, con)}")
        if only == "ksic":
            from . import ksic
            log(f"KSIC 업종명 {await ksic.build(client):,}개 저장 → {ksic.KSIC_FILE.name}")
        if only in (None, "ecos"):
            await build_ecos(client, con)
        if only == "ecos-missing":
            await build_ecos(client, con, missing_only=True)
        if only in (None, "kosis"):
            await build_kosis_tree(client, con, max_calls=max_calls)
            await build_kosis_seed(client, con)
    stats = dict(con.execute("SELECT source, COUNT(*) FROM datasets GROUP BY source").fetchall())
    log(f"카탈로그: {stats}")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["ecos", "ecos-missing", "kosis", "dart", "dart-industry", "ksic", "datago", "egroup"])
    ap.add_argument("--max-calls", type=int, default=8000,
                    help="KOSIS 목록 호출 상한(분당 170회라 8000번이면 약 50분). 전부 받으려면 16000 정도")
    a = ap.parse_args()
    asyncio.run(main(a.only, a.max_calls))
