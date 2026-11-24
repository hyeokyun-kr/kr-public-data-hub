"""공정위 의결서 검색('의결서' 탭, 선택 기능). 색인 파일 하나(decisions.sqlite)만 읽어 이 컴퓨터 안에서 찾는다.

색인은 공정위 사건처리시스템(case.ftc.go.kr)에서 받은 의결서 PDF로 따로 만든다(이 저장소에는 넣지 않음.
2026-11-17 기준 의결서 20,323건, 쪽 208,548쪽). 표 구성:
  decisions  의결서 파일 1개 = 1행(의결번호·의결일·사건번호·사건명·대표위반유형·대표조치유형·상태 등)
  page       쪽마다 본문(zlib 압축)
  pg         FTS5 전문 검색 색인(본문은 담지 않음), 단어 기준(unicode61). 단어 앞부분 일치로 찾는다
  info       만든 날·건수·출처
열 구성은 tests/test_decisions.py 의 SCHEMA 를 본다.

색인 파일을 찾는 순서
  1. 환경변수 DATAHUB_DECISION_DB
  2. data/decisions.sqlite
색인이 없으면 탭에 '쓸 수 없음'만 뜨고 나머지 기능은 그대로 쓴다.
원문 PDF는 받아 둔 폴더(환경변수 DATAHUB_DECISION_PDF_DIR)가 있으면 그것을 열고, 없으면 그때 공정위 사이트에서
한 건만 받아 넘긴다(검색어는 공정위로 가지 않음).
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import unicodedata
import zlib
from array import array
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx

from .config import DATA_DIR
from .errors import SourceError
from .sources import rate_wait

SOURCE = "FTC"
SOURCE_LABEL = "공정거래위원회 사건처리시스템(case.ftc.go.kr) 의결서"
FTC_LIST = "https://case.ftc.go.kr/ocp/co/ltfr.do"
FTC_PDF = "https://case.ftc.go.kr/ocp/co/getFileList.do"
FTC_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": FTC_LIST}   # 수집 때와 같은 머리글(2만 건 받을 때 문제없었음)
PAGE_SIZE = 30          # 검색 결과 한 번에 보여 줄 의결서 수
SNIPS_PER_DOC = 2       # 결과 목록에서 의결서마다 보여 줄 앞뒤 문장 수
DOC_SNIP_MAX = 150      # 의결서 상세에서 앞뒤 문장을 보여 줄 쪽 수 상한
MAX_TERMS = 6
MAX_EXCLUDE = 3
COLS = ("id", "의결번호", "의결일", "연도", "사건번호", "사건명", "대표위반유형", "대표조치유형", "fileId", "fileSn",
        "경로", "쪽수", "글자수", "상태")
_WORD = re.compile(r"[^\W_]+")      # FTS5 unicode61 이 단어로 보는 글자 덩어리(글자·숫자)


def db_path() -> Path:
    env = os.environ.get("DATAHUB_DECISION_DB")
    return Path(env) if env else DATA_DIR / "decisions.sqlite"


def pdf_dir() -> Path | None:
    env = os.environ.get("DATAHUB_DECISION_PDF_DIR")
    return Path(env) if env and Path(env).is_dir() else None


def site_url(case_no: str) -> str:
    """공정위 사건처리시스템 의결서 목록을 사건번호로 거른 화면(브라우저로 열면 그 사건 줄과 PDF 단추가 나옴)."""
    return FTC_LIST + "?" + urlencode({"searchKrwd": "", "pageIndex": 1, "caseNo": case_no})


def _key(s: str) -> str:
    """사건명·번호 비교용: 띄어쓰기·문장부호를 모두 빼고 소문자로(붙여 쓴 이름도 찾게)."""
    return "".join(_WORD.findall(unicodedata.normalize("NFC", s or ""))).lower()


# ------------------------------------------------------------------ 검색어 풀기
@dataclass
class Term:
    text: str                 # 화면에 보일 원래 말
    toks: list[str]

    @property
    def fts(self) -> str:     # 단어 앞부분 일치. 여러 단어면 그 순서로 붙어 있는 구절
        return '"' + " ".join(self.toks) + '"*'

    @property
    def key(self) -> str:
        return "".join(self.toks)

    @property
    def pattern(self) -> str:  # 본문에서 강조할 자리(구절 안 띄어쓰기·줄바꿈·문장부호는 무엇이든)
        return r"[\W_]+".join(re.escape(t) for t in self.toks)


def parse(q: str) -> tuple[list[Term], list[Term], list[str]]:
    """'공동행위 감경' → 둘 다 든 의결서, '"부당한 공동행위"' → 붙어 있는 구절, '-입찰' → 그 말이 든 의결서 빼기."""
    q = unicodedata.normalize("NFC", q or "")
    pos: list[Term] = []
    neg: list[Term] = []
    ignored: list[str] = []
    for neg1, quoted, neg2, word in re.findall(r'(-?)"([^"]*)"?|(-?)(\S+)', q):
        text = quoted if (quoted or neg1) else word
        minus = bool(neg1 or neg2)
        toks = [t.lower() for t in _WORD.findall(text)]
        if not toks:
            continue
        if sum(len(t) for t in toks) < 2:        # 한 글자는 거의 모든 쪽에 걸려 뜻이 없다
            ignored.append(text)
            continue
        term = Term(text.strip(), toks)
        (neg if minus else pos).append(term)
    if len(pos) > MAX_TERMS:
        ignored += [t.text for t in pos[MAX_TERMS:]]
        pos = pos[:MAX_TERMS]
    if len(neg) > MAX_EXCLUDE:
        ignored += ["-" + t.text for t in neg[MAX_EXCLUDE:]]
        neg = neg[:MAX_EXCLUDE]
    return pos, neg, ignored


def _regex(terms: list[Term]) -> re.Pattern | None:
    if not terms:
        return None
    pats = sorted({t.pattern for t in terms}, key=len, reverse=True)
    return re.compile("|".join(f"(?:{p})" for p in pats), re.IGNORECASE)


def mark(text: str, rx: re.Pattern | None) -> list[list]:
    """글을 [조각, 강조 여부] 목록으로. 화면은 조각을 이스케이프한 뒤 강조만 감싼다(본문 HTML이 그대로 들어가지 않게)."""
    if not rx or not text:
        return [[text, 0]] if text else []
    out, i = [], 0
    for m in rx.finditer(text):
        if m.start() == m.end():
            continue
        if m.start() > i:
            out.append([text[i:m.start()], 0])
        out.append([m.group(0), 1])
        i = m.end()
    if i < len(text):
        out.append([text[i:], 0])
    return out


def snippet(text: str, rx: re.Pattern | None, before: int = 70, after: int = 130) -> dict:
    """처음 나온 자리 앞뒤 문장(한 줄로)."""
    flat = re.sub(r"\s+", " ", text).strip()
    m = rx.search(flat) if rx else None
    first = m.start() if m else 0
    a, b = max(0, first - before), min(len(flat), first + after)
    if a > 0:                                   # 단어 중간에서 자르지 않게
        sp = flat.find(" ", a)
        if 0 <= sp < first:
            a = sp + 1
    if b < len(flat):
        sp = flat.rfind(" ", first + (m.end() - m.start() if m else 0), b)
        if sp > first + 30:
            b = sp
    return {"pre": a > 0, "post": b < len(flat), "segs": mark(flat[a:b], rx), "found": bool(m)}


# ------------------------------------------------------------------ 색인
_local = threading.local()


def _stats(per_term: list[dict], doc: int) -> tuple[set, set]:
    """그 의결서에서 찾는 말이 모두 나온 쪽(rowid)과 하나라도 나온 쪽."""
    rows = [set(h.get(doc, ())) for h in per_term]
    if not rows:
        return set(), set()
    return set.intersection(*rows), set().union(*rows)


class Index:
    """의결서 목록(2만 건)과 쪽 번호표(20만 쪽)는 메모리에, 본문·전문 색인은 파일에서 그때그때 읽는다."""

    def __init__(self, path: Path):
        self.path = path
        self.mtime = path.stat().st_mtime
        con = self._con()
        self.info = dict(con.execute("SELECT k, v FROM info").fetchall())
        self.docs: dict[int, dict] = {}
        for r in con.execute(f"SELECT {', '.join(COLS)} FROM decisions"):
            d = dict(zip(COLS, r))
            d["types"] = [x for x in (d["대표위반유형"] or "").split(" / ") if x]
            d["acts"] = [x for x in (d["대표조치유형"] or "").split(" / ") if x]
            d["key"] = _key(f"{d['사건명']} {d['사건번호']} {d['의결번호']}")
            self.docs[d["id"]] = d
        pm = con.execute("SELECT rowid, doc, page FROM page INDEXED BY page_doc").fetchall()
        top = max((r for r, _, _ in pm), default=0)
        self.row_doc = array("i", bytes(4 * (top + 1)))
        self.row_page = array("i", bytes(4 * (top + 1)))
        self.doc_rows: dict[int, list[tuple[int, int]]] = {}       # 의결서 → [(쪽 번호, rowid)] 쪽 순서
        for rowid, doc, page in pm:
            self.row_doc[rowid] = doc
            self.row_page[rowid] = page
            self.doc_rows.setdefault(doc, []).append((page, rowid))
        for v in self.doc_rows.values():
            v.sort()
        self.types = Counter(t for d in self.docs.values() for t in d["types"])
        self.acts = Counter(a for d in self.docs.values() for a in d["acts"])
        years = [d["연도"] for d in self.docs.values() if d["연도"]]
        self.years = (min(years), max(years)) if years else (None, None)
        self.order_date = sorted(self.docs, key=lambda i: (self.docs[i]["의결일"] or "", i), reverse=True)

    def _con(self) -> sqlite3.Connection:
        """스레드마다 읽기 전용 연결 하나(검색은 서버의 작업 스레드에서 돈다)."""
        c = getattr(_local, "con", None)
        tag = (str(self.path), self.mtime)       # 색인 파일을 새로 만들어 바꾸면 연결도 새로
        if c is None or getattr(_local, "tag", None) != tag:
            if c is not None:
                c.close()
            c = sqlite3.connect(f"file:{quote(str(self.path))}?mode=ro", uri=True, check_same_thread=False)
            _local.con, _local.tag = c, tag
        return c

    def stale(self) -> bool:
        try:
            return self.path.stat().st_mtime != self.mtime
        except OSError:
            return True

    def status(self) -> dict:
        i = self.info
        return {"available": True, "count": len(self.docs), "pages": sum(len(v) for v in self.doc_rows.values()),
                "built": i.get("만든때"), "source": i.get("출처"), "years": list(self.years),
                "problems": sum(1 for d in self.docs.values() if d["상태"] != "정상"),
                "types": self.types.most_common(), "acts": self.acts.most_common()}

    # -------------------------------------------------------------- 찾기
    def _rows(self, term: Term, lo: int | None = None, hi: int | None = None) -> list[int]:
        con = self._con()
        try:
            if lo is None:
                return [r for (r,) in con.execute("SELECT rowid FROM pg WHERE pg MATCH ?", (term.fts,))]
            return [r for (r,) in con.execute("SELECT rowid FROM pg WHERE pg MATCH ? AND rowid BETWEEN ? AND ?",
                                              (term.fts, lo, hi))]
        except sqlite3.OperationalError as e:
            raise SourceError(SOURCE, "bad_request", f"검색어를 풀지 못했습니다: {term.text} ({e})") from None

    def _term_docs(self, term: Term) -> dict[int, list[int]]:
        """그 말이 본문(쪽) 또는 사건명·사건번호·의결번호에 있는 의결서 → 본문에 나온 rowid 목록."""
        out: dict[int, list[int]] = {}
        rd = self.row_doc
        for r in self._rows(term):
            out.setdefault(rd[r], []).append(r)
        k = term.key
        for i, d in self.docs.items():
            if k in d["key"] and i not in out:
                out[i] = []
        return out

    def search(self, q: str = "", types: list[str] | None = None, acts: list[str] | None = None, y1: int | None = None,
               y2: int | None = None, sort: str = "date", offset: int = 0, limit: int = PAGE_SIZE,
               snippets: bool = True) -> dict:
        """snippets=False 이면 앞뒤 문장 없이 목록만 돌려준다(화면은 목록을 먼저 띄우고 snippets()로 채움.
        본문은 의결서마다 다른 자리에 있어, 느린 디스크에서는 30건 앞뒤 문장 읽기가 몇 초 걸림)."""
        pos, neg, ignored = parse(q)
        types = [t for t in (types or []) if t]
        acts = [a for a in (acts or []) if a]
        if not pos and not (types or acts or y1 or y2):
            if ignored or neg:
                raise SourceError(SOURCE, "bad_request", "찾을 말이 없습니다. 두 글자 이상 넣거나 빼기(-)만 쓰지 말고 찾을 말도 넣으세요.")
            raise SourceError(SOURCE, "bad_request", "검색어를 넣거나 위반유형·조치유형·연도를 고르세요.")
        per_term = [self._term_docs(t) for t in pos]
        if per_term:
            cand = set(per_term[0])
            for h in per_term[1:]:
                cand &= set(h)
        else:
            cand = set(self.docs)
        for t in neg:
            cand -= set(self._term_docs(t))

        def year_ok(d):
            return (not y1 or (d["연도"] or 0) >= y1) and (not y2 or (d["연도"] or 9999) <= y2)

        def type_ok(d):
            return not types or any(t in d["types"] for t in types)

        def act_ok(d):
            return not acts or any(a in d["acts"] for a in acts)

        base = [self.docs[i] for i in cand if year_ok(self.docs[i])]
        f_types = Counter(t for d in base if act_ok(d) for t in d["types"])
        f_acts = Counter(a for d in base if type_ok(d) for a in d["acts"])
        res = [d for d in base if type_ok(d) and act_ok(d)]
        f_years = Counter(d["연도"] for d in res)

        def stats(i):
            return _stats(per_term, i)

        if sort == "hits" and per_term:
            score = {}
            for d in res:
                together, anyrow = stats(d["id"])
                title = sum(1 for t in pos if t.key in d["key"])
                score[d["id"]] = (len(together), title, len(anyrow), d["의결일"] or "", d["id"])
            res.sort(key=lambda d: score[d["id"]], reverse=True)
        else:
            sort = "date"
            res.sort(key=lambda d: (d["의결일"] or "", d["id"]), reverse=True)
        offset = max(0, offset)
        shown = res[offset:offset + limit]
        out = []
        for d in shown:
            item = self.brief(d)
            if per_term:
                together, anyrow = stats(d["id"])
                item["hit_pages"] = len({self.row_page[r] for r in anyrow})
                item["together_pages"] = len({self.row_page[r] for r in together})
                item["title_hit"] = any(t.key in d["key"] for t in pos)
            out.append(item)
        if snippets and per_term:
            snips = self._snippets(pos, per_term, [d["id"] for d in shown])
            for item in out:
                item["snippets"] = snips.get(item["id"], [])
        return {"query": q, "terms": [t.text for t in pos], "exclude": [t.text for t in neg], "ignored": ignored,
                "count": len(res), "offset": offset, "shown": len(out), "sort": sort, "results": out,
                "facets": {"types": f_types.most_common(), "acts": f_acts.most_common(), "years": sorted(f_years.items())}}

    def _snippets(self, pos: list[Term], per_term: list[dict], ids: list[int]) -> dict[int, list[dict]]:
        """의결서마다 앞뒤 문장 SNIPS_PER_DOC개: 찾는 말이 모두 나온 쪽 먼저, 모자라면 하나라도 나온 쪽."""
        picks: dict[int, list[int]] = {}
        for i in ids:
            together, anyrow = _stats(per_term, i)
            pick = sorted(together, key=lambda r: self.row_page[r])[:SNIPS_PER_DOC]
            if len(pick) < SNIPS_PER_DOC:
                pick += sorted(anyrow - together, key=lambda r: self.row_page[r])[:SNIPS_PER_DOC - len(pick)]
            picks[i] = pick
        body = dict(self._bodies(sorted(r for v in picks.values() for r in v)))   # 한 번에, 파일 순서대로
        rx = _regex(pos)
        return {i: [{"page": self.row_page[r], **snippet(body[r], rx)} for r in v if r in body] for i, v in picks.items()}

    def snippets(self, q: str, ids: list[int]) -> dict:
        """목록을 먼저 띄운 뒤 화면이 부르는 앞뒤 문장 채우기(검색과 같은 규칙)."""
        pos, _, _ = parse(q)
        ids = [i for i in ids if i in self.docs][:PAGE_SIZE * 2]
        if not pos or not ids:
            return {"snippets": {}}
        per_term = [self._term_docs(t) for t in pos]
        return {"snippets": {str(k): v for k, v in self._snippets(pos, per_term, ids).items()}}

    def best_pages(self, q: str, types: list[str] | None = None, acts: list[str] | None = None, y1: int | None = None,
                   y2: int | None = None, max_docs: int = 12, per_doc: int = 3) -> list[dict]:
        """AI 검색의 근거 쪽 고르기: 찾는 말이 많이 함께 나온 의결서(많이 나온 순) 상위 max_docs건에서,
        의결서마다 찾는 말이 가장 많이 함께 나온 쪽 per_doc쪽. 본문은 읽지 않는다(rowid만)."""
        pos, _, _ = parse(q)
        if not pos:
            return []
        res = self.search(q, types, acts, y1, y2, sort="hits", limit=max_docs, snippets=False)
        per_term = [self._term_docs(t) for t in pos]
        out = []
        for item in res["results"]:
            cnt = Counter()
            for h in per_term:
                for r in h.get(item["id"], ()):
                    cnt[r] += 1
            for r, c in sorted(cnt.items(), key=lambda kv: (-kv[1], self.row_page[kv[0]]))[:per_doc]:
                out.append({"doc": item["id"], "rowid": r, "page": self.row_page[r], "matched": c, "of": len(pos),
                            "date": item["date"]})
        return out

    def texts(self, rowids: list[int]) -> dict[int, str]:
        return dict(self._bodies(sorted(set(rowids))))

    def _bodies(self, rowids: list[int]) -> list[tuple[int, str]]:
        if not rowids:
            return []
        con = self._con()
        got = {}
        for i in range(0, len(rowids), 500):
            part = rowids[i:i + 500]
            for r, z in con.execute(f"SELECT rowid, z FROM page WHERE rowid IN ({','.join('?' * len(part))})", part):
                got[r] = zlib.decompress(z).decode("utf-8")
        return [(r, got[r]) for r in rowids if r in got]

    def brief(self, d: dict) -> dict:
        return {"id": d["id"], "no": d["의결번호"], "date": d["의결일"], "year": d["연도"], "case_no": d["사건번호"],
                "name": d["사건명"], "types": d["types"], "acts": d["acts"], "pages": d["쪽수"], "status": d["상태"]}

    def get(self, doc_id: int) -> dict:
        d = self.docs.get(doc_id)
        if d is None:
            raise SourceError(SOURCE, "not_found", f"의결서 번호 {doc_id}가 색인에 없습니다(색인이 바뀌었으면 다시 검색하세요).")
        return d

    def doc(self, doc_id: int, q: str = "") -> dict:
        """의결서 한 건: 목록 정보 + 검색어가 나온 쪽(앞뒤 문장) + 쪽 번호 목록."""
        d = self.get(doc_id)
        rows = self.doc_rows.get(doc_id, [])
        pos, _, _ = parse(q)
        hits = []
        if rows and pos:
            lo, hi = min(r for _, r in rows), max(r for _, r in rows)
            sets = [{r for r in self._rows(t, lo, hi) if self.row_doc[r] == doc_id} for t in pos]
            together = set.intersection(*sets)
            anyrow = set().union(*sets)
            order = sorted(anyrow, key=lambda r: self.row_page[r])
            rx = _regex(pos)
            body = dict(self._bodies(order[:DOC_SNIP_MAX]))
            for r in order:
                h = {"page": self.row_page[r], "together": r in together,
                     "terms": [i for i, s in enumerate(sets) if r in s]}
                if r in body:
                    h.update(snippet(body[r], rx))
                hits.append(h)
        url = site_url(d["사건번호"].split(", ")[0]) if d["사건번호"] else FTC_LIST
        return {**self.brief(d), "chars": d["글자수"], "page_list": [p for p, _ in rows], "terms": [t.text for t in pos],
                "hits": hits, "title_hit": any(t.key in d["key"] for t in pos), "site_url": url,
                "pdf_local": bool(self.local_pdf(d)), "source_label": SOURCE_LABEL}

    def page(self, doc_id: int, page: int, q: str = "") -> dict:
        d = self.get(doc_id)
        rows = self.doc_rows.get(doc_id, [])
        hit = [r for p, r in rows if p == page]
        if not hit:
            raise SourceError(SOURCE, "not_found", f"{d['의결번호']}에는 {page}쪽 본문이 없습니다(빈 쪽이거나 쪽 수를 넘음).")
        pos, _, _ = parse(q)
        text = self._bodies(hit)[0][1]
        return {"id": doc_id, "page": page, "page_list": [p for p, _ in rows], "segs": mark(text, _regex(pos)),
                "terms": [t.text for t in pos]}

    def local_pdf(self, d: dict) -> Path | None:
        base = pdf_dir()
        if not base or not d.get("경로"):
            return None
        p = base / d["경로"]
        return p if p.is_file() else None


# ------------------------------------------------------------------ 불러오기(서버가 켜질 때 뒤에서)
_lock = threading.Lock()
_index: Index | None = None
_error: str | None = None
_loading = False


def load(force: bool = False) -> Index | None:
    global _index, _error, _loading
    with _lock:
        if _index is not None and not force and not _index.stale():
            return _index
        p = db_path()
        if not p.exists():
            _index, _error = None, "missing"
            return None
        _loading = True                 # 느린 디스크에서는 처음 읽기에 수십 초 걸릴 수 있다(화면은 '불러오는 중')
        try:
            _index, _error = Index(p), None
        except (sqlite3.Error, OSError, zlib.error) as e:
            _index, _error = None, f"색인 파일을 읽지 못했습니다: {e}"
        finally:
            _loading = False
        return _index


def current() -> Index | None:
    return _index


def status() -> dict:
    if _index is not None:
        return _index.status()
    if _loading or _error is None:
        return {"available": False, "loading": True}
    return {"available": False, "error": _error, "expected": "data/decisions.sqlite"}


def need() -> Index:
    idx = load()
    if idx is None:
        if _error == "missing":
            raise SourceError(SOURCE, "not_found", "의결서 색인 파일(decisions.sqlite)이 없습니다. data/ 에 넣거나 DATAHUB_DECISION_DB 로 위치를 알려 주고 다시 켜세요.")
        raise SourceError(SOURCE, "server", _error or "의결서 색인을 읽지 못했습니다.")
    return idx


# ------------------------------------------------------------------ 원문 PDF(공정위 사이트에서 한 건만)
_ftc_client: httpx.AsyncClient | None = None


async def _client() -> httpx.AsyncClient:
    global _ftc_client
    if _ftc_client is None or _ftc_client.is_closed:
        _ftc_client = httpx.AsyncClient(headers=FTC_HEADERS, timeout=httpx.Timeout(90.0, connect=15.0))
    return _ftc_client


async def close() -> None:
    global _ftc_client
    if _ftc_client is not None:
        await _ftc_client.aclose()
        _ftc_client = None


async def fetch_pdf(file_id: str, file_sn: str) -> bytes:
    """공정위 사이트는 목록 화면을 한 번 연 세션(쿠키)이 있어야 PDF를 준다. 세션은 10분쯤 지나면 끊겨
    PDF 대신 307이나 첫 화면(HTML)을 주므로, 그러면 세션을 새로 열어 한 번 더 받는다."""
    client = await _client()
    for attempt in range(2):
        try:
            if attempt or not client.cookies:
                await rate_wait(SOURCE)
                await client.get(FTC_LIST, params={"searchKrwd": "", "pageIndex": 1})
            await rate_wait(SOURCE)
            r = await client.post(FTC_PDF, data={"docId": file_id, "docSn": file_sn}, follow_redirects=False)
        except (httpx.TimeoutException, httpx.TransportError):
            raise SourceError(SOURCE, "network", "공정위 사이트에 연결하지 못했습니다. '공정위 사이트에서 찾기'로 여세요.") from None
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            return r.content
        client.cookies.clear()
    raise SourceError(SOURCE, "server", f"공정위 사이트가 PDF 대신 다른 응답을 주었습니다(HTTP {r.status_code}). '공정위 사이트에서 찾기'로 여세요.",
                      str(r.status_code))


def pdf_name(d: dict) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', " ", d["사건명"] or "").strip()[:40].strip()
    return f"{d['의결일']}_{d['의결번호']}_{name}.pdf"
