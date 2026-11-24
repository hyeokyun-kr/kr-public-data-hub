"""로컬 데이터셋 카탈로그(SQLite)와 검색.

검색용 정보(통계표 이름·경로·설명·ECOS 항목명)만 로컬에 두고, 실제 수치는 조회할 때마다
원천 API에서 가져온다.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass, field

from .config import CATALOG_DB, DATA_DIR
from .periods import CYCLE_LABEL

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
  source TEXT NOT NULL,          -- KOSIS / ECOS
  ds_id TEXT NOT NULL,           -- ECOS: 통계표코드, KOSIS: 기관ID/통계표ID
  org_id TEXT, org_name TEXT,
  name TEXT NOT NULL,            -- 통계표명(ECOS는 앞 번호를 뗀 이름)
  path TEXT,                     -- 분류 경로
  ranges TEXT,                   -- {"M": ["1965-01","2026-10"], ...}, KOSIS 목록에서 온 것은 {"*": [시작,끝]} 또는 {}
  end_year INTEGER,              -- 마지막 수록 연도(모르면 최종 갱신 연도)
  unit TEXT,
  description TEXT,
  keywords TEXT,                 -- KOSIS 검색 결과의 CONTENTS(분류·품목 이름 묶음)
  url TEXT,
  vw_cd TEXT, stat_id TEXT,
  origin TEXT,                   -- ecos_list / kosis_tree / kosis_search / datago_list
  updated_at TEXT,
  extra TEXT,                    -- 기관별 덧붙임(JSON). 공공데이터포털: 유형·형식·수정일·업데이트 주기·포털 파일 여부
  PRIMARY KEY (source, ds_id)
);
CREATE TABLE IF NOT EXISTS ecos_items (
  stat_code TEXT, grp_code TEXT, grp_name TEXT, item_code TEXT, item_name TEXT,
  p_item_code TEXT, p_item_name TEXT, cycle TEXT, start TEXT, end TEXT, unit TEXT, data_cnt INTEGER, weight TEXT
);
CREATE INDEX IF NOT EXISTS ecos_items_stat ON ecos_items(stat_code);
CREATE TABLE IF NOT EXISTS search_cache (
  source TEXT, term TEXT, fetched_at TEXT, PRIMARY KEY(source, term)
);
CREATE TABLE IF NOT EXISTS meta_cache (
  source TEXT, ds_id TEXT, fetched_at TEXT, body TEXT, PRIMARY KEY(source, ds_id)
);
CREATE TABLE IF NOT EXISTS build_info (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS egroup_groups (     -- 공정위 기업집단포털: 지정 대규모기업집단(공개년월별)
  ym TEXT, code TEXT, name TEXT, owner TEXT, rep TEXT, n_comp INTEGER, limit_type TEXT, cls TEXT,
  asset_rank INTEGER, asset_total TEXT, PRIMARY KEY (ym, code)
);
CREATE TABLE IF NOT EXISTS egroup_affiliates (  -- 소속회사(공개년월별). 원천에 집단코드가 없어 같은 공개년월 집단 이름으로 붙임
  ym TEXT, group_code TEXT, group_name TEXT, company TEXT, jurirno TEXT, bizrno TEXT, ceo TEXT, founded TEXT, joined TEXT
);
CREATE INDEX IF NOT EXISTS egroup_aff_jurir ON egroup_affiliates(jurirno);
CREATE INDEX IF NOT EXISTS egroup_aff_grp ON egroup_affiliates(ym, group_code);
CREATE TABLE IF NOT EXISTS egroup_changes (     -- 계열 편입·제외·유예 변경내역(전체 기간, 원천 칸 그대로 JSON)
  group_name TEXT, company TEXT, jurirno TEXT, change TEXT, day TEXT, body TEXT
);
CREATE INDEX IF NOT EXISTS egroup_chg_grp ON egroup_changes(group_name);
CREATE TABLE IF NOT EXISTS dart_jurir (        -- DART 회사 고유번호 → 법인등록번호(기업개황에서). 기업집단 소속회사와 정확히 잇는 열쇠
  corp_code TEXT PRIMARY KEY, jurir_no TEXT
);
CREATE INDEX IF NOT EXISTS dart_jurir_no ON dart_jurir(jurir_no);
CREATE TABLE IF NOT EXISTS dart_corps (       -- OpenDART 회사 고유번호 목록(corpCode.xml). 회사명 검색은 여기서만
  corp_code TEXT PRIMARY KEY, corp_name TEXT, corp_eng_name TEXT, stock_code TEXT, modify_date TEXT,
  name_norm TEXT, induty_code TEXT, corp_cls TEXT
);
"""

# 마지막 수록 연도가 올해보다 이 해 수 넘게 앞서면 '중단'으로 본다(2년 주기·공표 지연 통계를 중단으로 보지 않게 3년)
DISCONTINUED_GAP = 3
_PHONE = re.compile(r"\s*\(\s*\d[\d\-~ ]{5,}\)\s*")
REGIONS = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남", "충청", "전북",
           "전남", "전라", "경북", "경남", "경상", "제주", "시도", "시군구", "지역", "시군", "구별", "읍면동"]
_STRIP = re.compile(r"[\s·ㆍ・,./\\()\[\]{}<>\-_=:;'\"‘’“”~!?|+*&^%$#@`]")
_NUM_PREFIX = re.compile(r"^\s*(\d+\.)+\s*")
_PAREN = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).lower()
    return _STRIP.sub("", s)


def core_name(name: str) -> str:
    """'4.2.1. 소비자물가지수(2020=100)' → '소비자물가지수'"""
    s = _NUM_PREFIX.sub("", unicodedata.normalize("NFKC", name))
    s = _PAREN.sub("", s)
    return norm(s)


def clean_org(name: str | None) -> str | None:
    """'국가데이터처(02-2012-9114)' → '국가데이터처'"""
    return _PHONE.sub("", name).strip() if name else name


def strip_number(name: str) -> str:
    return _NUM_PREFIX.sub("", name).strip()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(CATALOG_DB, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(datasets)")}
    if "extra" not in cols:            # v0.2 이전 색인 파일에 칸 더하기
        con.execute("ALTER TABLE datasets ADD COLUMN extra TEXT")
        con.commit()
    return con


def now_kst() -> str:
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S")


def upsert_dataset(con: sqlite3.Connection, d: dict, keep_richer: bool = True) -> None:
    """같은 통계표가 이미 있으면, 비어 있지 않은 새 값만 덮어쓴다(검색 결과의 설명·기간이 목록보다 풍부)."""
    cols = ["source", "ds_id", "org_id", "org_name", "name", "path", "ranges", "end_year", "unit", "description",
            "keywords", "url", "vw_cd", "stat_id", "origin", "updated_at", "extra"]
    d = {**d, "updated_at": d.get("updated_at") or now_kst()}
    if isinstance(d.get("ranges"), dict):
        d["ranges"] = json.dumps(d["ranges"], ensure_ascii=False)
    if isinstance(d.get("extra"), dict):
        d["extra"] = json.dumps(d["extra"], ensure_ascii=False)
    old = con.execute("SELECT * FROM datasets WHERE source=? AND ds_id=?", (d["source"], d["ds_id"])).fetchone()
    if old is not None and keep_richer:
        merged = dict(old)
        for c in cols:
            v = d.get(c)
            if v not in (None, "", "{}"):
                merged[c] = v
        # 상세 정보(PRD)로 확인한 주기별 기간({"A": [...]})을, 주기 없는 검색 결과({"*": [...]})로 덮어쓰지 않는다
        try:
            old_r = json.loads(old["ranges"] or "{}")
            new_r = json.loads(d.get("ranges") or "{}")
        except (ValueError, TypeError):
            old_r, new_r = {}, {}
        if any(k != "*" for k in old_r) and not any(k != "*" for k in new_r):
            merged["ranges"] = old["ranges"]
            merged["end_year"] = old["end_year"]
        d = merged
    con.execute(f"INSERT OR REPLACE INTO datasets ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [d.get(c) for c in cols])


# ---------------------------------------------------------------- 검색

@dataclass
class Entry:
    source: str
    ds_id: str
    row: dict
    n_name: str
    n_core: str
    n_path: str
    n_kw: str
    cycles: set = field(default_factory=set)
    hay: str = ""            # 이름·경로·설명·키워드·작성기관·ECOS 항목명을 합친 것(후보 거르기용)
    n_keyw: str = ""         # 키워드만(설명보다 높게 친다)
    n_org: str = ""          # 작성·제공 기관


@dataclass
class Hit:
    entry: Entry
    score: float
    reason: str
    term: str
    items: list = field(default_factory=list)   # ECOS 항목 일치 [(item_code, item_name, grp_code)]


class Catalog:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.entries: dict[tuple[str, str], Entry] = {}
        self.ecos_items: dict[str, list[tuple]] = {}
        self.load()

    def load(self) -> None:
        con = connect()
        entries: dict[tuple[str, str], Entry] = {}
        for r in con.execute("SELECT * FROM datasets"):
            entries[(r["source"], r["ds_id"])] = self._entry(dict(r))
        items: dict[str, list[tuple]] = {}
        for r in con.execute("SELECT stat_code, item_code, item_name, grp_code, cycle FROM ecos_items"):
            items.setdefault(r[0], []).append((r[1], r[2], r[3], norm(r[2]), r[4]))
        con.close()
        org_names: dict[str, str] = {}
        for (src, ds), e in entries.items():
            if src == "KOSIS" and e.row.get("org_name"):
                org_names.setdefault(e.row.get("org_id"), e.row["org_name"])
        for (src, ds), e in entries.items():
            if src == "KOSIS" and not e.row.get("org_name"):
                e.row["org_name"] = org_names.get(e.row.get("org_id"))
                if e.row["org_name"]:          # 기관 이름을 나중에 알게 된 표도 기관 이름으로 찾히게
                    e.n_org = norm(e.row["org_name"])
                    e.hay += "|" + e.n_org
            if src == "ECOS":
                e.hay += "|" + "|".join(x[3] for x in items.get(ds, ()))
        with self.lock:
            self.entries = entries
            self.ecos_items = items
            self._reindex()

    def _reindex(self) -> None:
        self._list = list(self.entries.values())
        self._hays = [e.hay for e in self._list]
        self._pos = {(e.source, e.ds_id): i for i, e in enumerate(self._list)}

    def _entry(self, row: dict) -> Entry:
        try:
            ranges = json.loads(row.get("ranges") or "{}")
        except ValueError:
            ranges = {}
        row["ranges"] = ranges
        try:
            row["extra"] = json.loads(row.get("extra") or "{}")
        except (ValueError, TypeError):
            row["extra"] = {}
        if row.get("source") != "DATAGO":     # 공공데이터포털 기관명은 원천 그대로(KOSIS 기관명의 전화번호 꼬리만 떼는 처리)
            row["org_name"] = clean_org(row.get("org_name"))
        e = Entry(row["source"], row["ds_id"], row, norm(row["name"]), core_name(row["name"]),
                  norm(row.get("path")), norm((row.get("keywords") or "") + " " + (row.get("description") or "")[:400]),
                  {c for c in ranges if c != "*"})
        e.n_keyw = norm(row.get("keywords"))
        e.n_org = norm(row.get("org_name"))
        e.hay = "|".join((e.n_name, e.n_path, e.n_kw, e.n_org))
        if e.source == "DATAGO":          # 10만 건이라 검색·결과 표시에 쓰는 칸만 남긴다(설명은 n_kw 에, 상세는 datago_meta.sqlite)
            e.row = {k: row.get(k) for k in ("source", "ds_id", "org_id", "org_name", "name", "path", "end_year", "url", "extra")}
            e.row["ranges"] = {}
        return e

    def add(self, row: dict) -> None:
        e = self._entry(dict(row))
        if e.source == "ECOS":
            e.hay += "|" + "|".join(x[3] for x in self.ecos_items.get(e.ds_id, ()))
        with self.lock:
            key = (e.source, e.ds_id)
            self.entries[key] = e
            i = self._pos.get(key)
            if i is None:
                self._pos[key] = len(self._list)
                self._list.append(e)
                self._hays.append(e.hay)
            else:
                self._list[i] = e
                self._hays[i] = e.hay

    def update_ranges(self, source: str, ds_id: str, ranges: dict) -> None:
        """상세 정보에서 알게 된 실제 수록 주기·기간을 색인에 저장한다(주기 필터가 정확해짐)."""
        e = self.entries.get((source, ds_id))
        if e is None or not ranges or e.row.get("ranges") == ranges:
            return
        ends = [v[1] for v in ranges.values() if v and v[1]]
        end_year = max(int(str(x)[:4]) for x in ends if str(x)[:4].isdigit()) if ends else e.row.get("end_year")
        con = connect()
        con.execute("UPDATE datasets SET ranges=?, end_year=? WHERE source=? AND ds_id=?",
                    (json.dumps(ranges, ensure_ascii=False), end_year, source, ds_id))
        con.commit()
        row = dict(con.execute("SELECT * FROM datasets WHERE source=? AND ds_id=?", (source, ds_id)).fetchone())
        con.close()
        row["org_name"] = row.get("org_name") or e.row.get("org_name")
        self.add(row)

    def stats(self) -> dict:
        out: dict[str, int] = {}
        for (src, _), _e in list(self.entries.items()):
            out[src] = out.get(src, 0) + 1
        out["ECOS_items"] = sum(len(v) for v in self.ecos_items.values())
        return out

    # 점수: 이름 완전 일치 100 > 핵심이름 일치 95 > 이름 앞부분 62~75 > 이름에 포함 50~60 > 낱말 모두 이름에 55
    #      > ECOS 항목명 일치 70 / 표 이름+항목명에 낱말 모두 50 / 항목명에 포함 45 > 경로 포함 30
    #      > 키워드 포함 25 > 이름·경로·설명에 낱말 모두 20 > 작성·제공 기관 일치 18 > 설명·분류명 포함 15.
    #      앞부분·포함은 검색어가 이름에서 차지하는 비율만큼 더 준다('GDP' → 'GDP 대비 경상의료비 비율'은 낮게).
    #      동의어로 찾은 것은 ×0.9, 관련어는 ×0.7.
    def _score(self, e: Entry, t: str, toks: list[str]) -> tuple[float, str, list]:
        best, reason, items = 0.0, "", []
        if not t:
            return 0.0, "", []
        base = e.n_core or e.n_name
        cover = min(1.0, len(t) / max(1, len(base)))
        if e.n_name == t:
            best, reason = 100, "이름 일치"
        elif e.n_core == t:
            best, reason = 95, "이름 일치"
        elif e.n_name.startswith(t) or e.n_core.startswith(t):
            best, reason = 62 + 13 * cover, "이름 앞부분 일치"
        elif t in e.n_name:
            best, reason = 50 + 10 * cover, "이름에 포함"
        elif len(toks) > 1 and all(k in e.n_name for k in toks):
            best, reason = 55, "이름에 낱말 모두 포함"
        if e.source == "ECOS" and best < 70:
            name_hits = [k for k in toks if k in e.n_name] if len(toks) > 1 else []
            for code, name, grp, n_item, cyc in self.ecos_items.get(e.ds_id, ()):
                if n_item == t or (len(toks) > 1 and all(k in n_item for k in toks) and len(n_item) <= len(t) + 6):
                    s, why = 70, f"항목 '{name}' 일치"
                elif n_item.startswith(t):
                    s, why = 60, f"항목 '{name}' 앞부분 일치"
                elif name_hits and all(k in n_item or k in e.n_name for k in toks) and any(k in n_item for k in toks):
                    s, why = 50, f"표 이름과 항목 '{name}'에 낱말 모두"
                elif t in n_item or (len(toks) > 1 and all(k in n_item for k in toks)):
                    s, why = 45, f"항목 '{name}'에 포함"
                else:
                    continue
                if (code, name, grp) not in items:
                    items.append((code, name, grp))
                if s > best:
                    best, reason = s, why
            items = items[:5]
        if best < 30:
            hay = e.n_name + e.n_path
            if t in e.n_path or (len(toks) > 1 and all(k in hay for k in toks)):
                best, reason = 30, "분류 경로에 포함"
        if best < 20 and len(toks) > 1 and all(k in e.n_name + e.n_path + e.n_kw for k in toks):
            best, reason = 20, "이름·경로·설명에 낱말 모두"
        if best < 25 and e.n_keyw and (t in e.n_keyw or (len(toks) > 1 and all(k in e.n_keyw for k in toks))):
            best, reason = 25, "키워드에 포함"
        if best < 18 and e.n_org and t in e.n_org:
            best, reason = 18, "작성·제공 기관 일치"
        if best < 15 and e.n_kw:
            if t in e.n_kw or (len(toks) > 1 and all(k in e.n_kw for k in toks)):
                best, reason = 15, "설명·분류명에 포함"
        return best, reason, items

    def search(self, terms: list[tuple[str, str, float]], source: str | None = None, cycle: str | None = None,
               recent_only: bool = False, limit: int = 60, pins: list[tuple[str, str]] | None = None) -> list[Hit]:
        """terms: [(검색어, 이유표시, 가중치)] — 첫 번째가 사용자가 친 말(1.0), 그다음 동의어(0.9)·관련어(0.7).
        pins: 동의어 사전에서 이 검색어의 대표 통계표로 지정한 (기관, ID) — 맨 위로 올린다."""
        pins = list(pins or [])
        this_year = _dt.date.today().year
        hits: dict[tuple[str, str], Hit] = {}
        asks_region = any(r in (terms[0][0] if terms else "") for r in REGIONS)
        prepared = []
        for term, why, weight in terms:
            toks = [norm(x) for x in term.split() if norm(x)]
            prepared.append((norm(term), toks, weight, why, term))
        # 후보 거르기: 검색어(또는 낱말 모두)가 합친 문자열에 들어 있는 표만 점수를 매긴다
        with self.lock:
            lst, hays = self._list, self._hays
            cand: set[int] = {self._pos[p] for p in pins if p in self._pos}
            for t, toks, *_ in prepared:
                if not t:
                    continue
                first = max(toks, key=len) if toks else t
                idx = [i for i, h in enumerate(hays) if first in h]
                if len(toks) > 1:
                    idx = [i for i in idx if all(k in hays[i] for k in toks)]
                cand.update(idx)
            entries = [lst[i] for i in sorted(cand)]
        for e in entries:
            if source and e.source != source:
                continue
            if cycle and e.cycles and cycle not in e.cycles:
                continue          # 주기를 아는 표는 정확히 거른다(KOSIS 표는 한 번 열어 보면 주기를 알게 됨)
            if cycle and e.source == "DATAGO":
                continue          # 공공데이터포털 목록은 자료 주기 정보가 없어 주기 필터에서는 뺀다
            end_year = e.row.get("end_year") or 0
            if recent_only and end_year and end_year < this_year - DISCONTINUED_GAP:
                continue
            best: Hit | None = None
            best_rel: Hit | None = None
            all_items: list = []          # 입력어·동의어로 찾은 ECOS 항목을 모두 모은다(관련어로 맞은 항목은 미리 고르지 않음)
            for t, toks, w, why, raw in prepared:
                s, reason, items = self._score(e, t, toks)
                if w >= 0.9:
                    for it in items:
                        if it not in all_items:
                            all_items.append(it)
                if s <= 0:
                    continue
                s *= w
                if w < 1.0:
                    reason = f"{why} → {reason}"
                if w >= 0.9:
                    if best is None or s > best.score:
                        best = Hit(e, s, reason, raw, items)
                elif best_rel is None or s > best_rel.score:
                    best_rel = Hit(e, s, reason, raw, [])
            # 입력어·동의어로 찾았으면 그것으로 설명하고, 관련어로만 찾힌 표는 관련어로 설명한다
            if best is None and best_rel is not None:
                best_rel.score = min(best_rel.score, 48)   # 관련어로만 찾힌 표는 입력어·동의어로 찾힌 표보다 아래
                best = best_rel
            if best is None:
                if (e.source, e.ds_id) not in pins:
                    continue
                best = Hit(e, 50, "", prepared[0][4] if prepared else "", [])
            best.items = all_items[:5]
            if best.items and "항목" not in best.reason:
                best.reason += f", 항목 '{best.items[0][1]}'"
                best.score += 6      # 표 이름과 항목이 함께 맞으면 더 관련 있음
            if (e.source, e.ds_id) in pins:
                best.score += 40 - pins.index((e.source, e.ds_id))   # 지정 순서대로
                best.reason = "대표 통계(동의어 사전 지정), " + best.reason
            # 보정: 끝난 통계는 내리고, 국제통계는 조금 내리고, 이름이 짧을수록 조금 올린다
            if end_year and end_year < this_year - DISCONTINUED_GAP:
                best.score -= 12
            if cycle and not e.cycles:
                best.score -= 10          # 주기 미확인 표는 주기 필터에서 빼지 않고 아래로
            if (e.row.get("vw_cd") or "").startswith("MT_RTITLE"):
                best.score -= 12          # 국제기구·외국 통계
            path = e.row.get("path") or ""
            if not asks_region and path.startswith("지역통계"):
                best.score -= 15 if "지자체" in path else 8    # 지역을 묻지 않았으면 지자체 기본통계 등은 내린다
            best.score -= min(len(e.row["name"]) / 15.0, 4)
            if e.source == "DATAGO":
                best.score -= 5           # 같은 점수면 국가통계(KOSIS·ECOS)를 먼저
            hits[(e.source, e.ds_id)] = best
        ranked = sorted(hits.values(), key=lambda h: (-h.score, h.entry.row["name"]))
        return ranked[:limit]


def hit_to_dict(h: Hit) -> dict:
    r = h.entry.row
    ranges = r.get("ranges") or {}
    cyc = [c for c in ["D", "SM", "M", "Q", "S", "A", "F", "IR"] if c in ranges]
    span = None
    if ranges:
        starts = [v[0] for v in ranges.values() if v and v[0]]
        ends = [v[1] for v in ranges.values() if v and v[1]]
        if starts and ends:
            span = [min(starts), max(ends)]
    this_year = _dt.date.today().year
    return {
        "source": r["source"], "id": r["ds_id"], "org_id": r.get("org_id"), "org_name": r.get("org_name"),
        "name": r["name"], "path": r.get("path"), "cycles": cyc, "cycle_labels": [CYCLE_LABEL[c] for c in cyc],
        "span": span, "end_year": r.get("end_year"), "unit": r.get("unit"),
        "discontinued": r["source"] != "DATAGO" and bool(r.get("end_year") and r["end_year"] < this_year - DISCONTINUED_GAP),
        "international": (r.get("vw_cd") or "").startswith("MT_RTITLE"),
        "regional": (r.get("path") or "").startswith("지역통계"),
        "cycles_known": bool(cyc),
        "score": round(h.score, 1), "reason": h.reason, "term": h.term,
        "items": [{"code": c, "name": n, "grp": g} for c, n, g in h.items],
        "url": r.get("url"),
        "extra": r.get("extra") or {},
    }
