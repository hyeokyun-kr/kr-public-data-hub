"""주기·시점 형식 맞추기.

화면에서는 한 가지 형식(표시형)만 쓰고, 원천 API로 보낼 때와 받은 뒤에만 기관 형식으로 바꾼다.

주기 코드(이 프로그램 기준)   표시형 예
  A  연                       2025
  S  반기                     2025-H1
  Q  분기                     2025-Q1
  M  월                       2025-01
  SM 반월                     2025-01-S1
  D  일                       2025-01-01
  IR 부정기                   원천 값 그대로
"""
from __future__ import annotations

import datetime as _dt
import re

CYCLE_LABEL = {"A": "연", "S": "반기", "Q": "분기", "M": "월", "SM": "반월", "D": "일", "IR": "부정기", "F": "다년"}
CYCLE_ORDER = ["D", "SM", "M", "Q", "S", "A", "F", "IR"]

# KOSIS 메타(한글) ↔ 이 프로그램 주기 코드 ↔ KOSIS 자료 API prdSe
KOSIS_META_TO_CYCLE = {"년": "A", "연": "A", "반기": "S", "분기": "Q", "월": "M", "일": "D", "부정기": "IR", "2년": "F", "3년": "F", "4년": "F", "5년": "F", "10년": "F", "다년": "F"}
CYCLE_TO_KOSIS_PRDSE = {"A": "Y", "S": "H", "Q": "Q", "M": "M", "D": "D", "IR": "IR", "F": "F"}
KOSIS_PRDSE_TO_CYCLE = {v: k for k, v in CYCLE_TO_KOSIS_PRDSE.items()}


class PeriodError(ValueError):
    pass


def _canon(cycle: str, s: str) -> tuple:
    """아무 형식의 시점 문자열을 (연, 하위) 튜플로 읽는다."""
    raw = str(s).strip()
    t = raw.upper().replace(" ", "")
    if cycle == "IR" or cycle == "F":
        m = re.match(r"^(\d{4})", t)
        if not m:
            raise PeriodError(f"시점 형식을 읽을 수 없음: {raw}")
        return (int(m.group(1)),) if len(t) == 4 else (t,)
    if cycle == "A":
        m = re.match(r"^(\d{4})$", t)
        if m:
            return (int(m.group(1)),)
    elif cycle == "S":
        for pat in (r"^(\d{4})[-.]?[HS]([12])$", r"^(\d{4})([12])/2$", r"^(\d{4})0([12])$"):
            m = re.match(pat, t)
            if m:
                return (int(m.group(1)), int(m.group(2)))
    elif cycle == "Q":
        for pat in (r"^(\d{4})[-.]?Q([1-4])$", r"^(\d{4})([1-4])/4$", r"^(\d{4})0([1-4])$"):
            m = re.match(pat, t)
            if m:
                return (int(m.group(1)), int(m.group(2)))
    elif cycle == "M":
        m = re.match(r"^(\d{4})[-./]?(\d{2})$", t)
        if m and 1 <= int(m.group(2)) <= 12:
            return (int(m.group(1)), int(m.group(2)))
    elif cycle == "SM":
        m = re.match(r"^(\d{4})[-./]?(\d{2})[-.]?S([12])$", t)
        if m and 1 <= int(m.group(2)) <= 12:
            return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    elif cycle == "D":
        m = re.match(r"^(\d{4})[-./]?(\d{2})[-./]?(\d{2})$", t)
        if m:
            try:
                _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))  # 없는 날짜(2025-02-30) 거르기
            except ValueError:
                raise PeriodError(f"없는 날짜: {raw}") from None
            return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    raise PeriodError(f"{CYCLE_LABEL.get(cycle, cycle)} 주기의 시점 형식이 아님: {raw}")


def to_display(cycle: str, s: str) -> str:
    c = _canon(cycle, s)
    if cycle in ("IR", "F"):
        return str(c[0])
    if cycle == "A":
        return f"{c[0]:04d}"
    if cycle == "S":
        return f"{c[0]:04d}-H{c[1]}"
    if cycle == "Q":
        return f"{c[0]:04d}-Q{c[1]}"
    if cycle == "M":
        return f"{c[0]:04d}-{c[1]:02d}"
    if cycle == "SM":
        return f"{c[0]:04d}-{c[1]:02d}-S{c[2]}"
    if cycle == "D":
        return f"{c[0]:04d}-{c[1]:02d}-{c[2]:02d}"
    raise PeriodError(cycle)


def to_ecos(cycle: str, s: str) -> str:
    c = _canon(cycle, s)
    return {
        "A": lambda: f"{c[0]:04d}",
        "S": lambda: f"{c[0]:04d}S{c[1]}",
        "Q": lambda: f"{c[0]:04d}Q{c[1]}",
        "M": lambda: f"{c[0]:04d}{c[1]:02d}",
        "SM": lambda: f"{c[0]:04d}{c[1]:02d}S{c[2]}",
        "D": lambda: f"{c[0]:04d}{c[1]:02d}{c[2]:02d}",
    }[cycle]()


def to_kosis(cycle: str, s: str) -> str:
    c = _canon(cycle, s)
    if cycle in ("IR", "F"):
        return str(c[0])
    return {
        "A": lambda: f"{c[0]:04d}",
        "S": lambda: f"{c[0]:04d}{c[1]:02d}",
        "Q": lambda: f"{c[0]:04d}{c[1]:02d}",
        "M": lambda: f"{c[0]:04d}{c[1]:02d}",
        "D": lambda: f"{c[0]:04d}{c[1]:02d}{c[2]:02d}",
    }[cycle]()


def sort_key(cycle: str, display: str) -> tuple:
    try:
        return _canon(cycle, display)
    except PeriodError:
        return (display,)


def shift(cycle: str, display: str, n: int) -> str:
    """시점을 n 기간만큼 앞(음수)·뒤(양수)로 옮긴다."""
    c = _canon(cycle, display)
    if cycle == "A":
        return to_display(cycle, f"{c[0] + n}")
    if cycle in ("S", "Q", "M"):
        per = {"S": 2, "Q": 4, "M": 12}[cycle]
        idx = c[0] * per + (c[1] - 1) + n
        y, r = divmod(idx, per)
        return to_display(cycle, {"S": f"{y}-H{r + 1}", "Q": f"{y}-Q{r + 1}", "M": f"{y}-{r + 1:02d}"}[cycle])
    if cycle == "SM":
        idx = (c[0] * 12 + c[1] - 1) * 2 + (c[2] - 1) + n
        m_idx, half = divmod(idx, 2)
        y, m = divmod(m_idx, 12)
        return f"{y:04d}-{m + 1:02d}-S{half + 1}"
    if cycle == "D":
        d = _dt.date(*c) + _dt.timedelta(days=n)
        return d.isoformat()
    return display


def count(cycle: str, start: str, end: str) -> int:
    """start~end 사이 기간 수(대략, 일 주기는 달력 일수)."""
    a, b = _canon(cycle, start), _canon(cycle, end)
    if cycle == "A":
        return b[0] - a[0] + 1
    if cycle in ("S", "Q", "M"):
        per = {"S": 2, "Q": 4, "M": 12}[cycle]
        return (b[0] * per + b[1]) - (a[0] * per + a[1]) + 1
    if cycle == "SM":
        return ((b[0] * 12 + b[1]) * 2 + b[2]) - ((a[0] * 12 + a[1]) * 2 + a[2]) + 1
    if cycle == "D":
        return (_dt.date(*b) - _dt.date(*a)).days + 1
    return 1


DEFAULT_SPAN = {"A": 10, "S": 20, "Q": 40, "M": 120, "SM": 48, "D": 365}


def default_range(cycle: str, start: str, end: str) -> tuple[str, str]:
    """수록 기간 안에서 최근 기본 구간(연 10년, 월 10년, 일 1년 등)."""
    if cycle not in DEFAULT_SPAN:
        return start, end
    s = shift(cycle, end, -(DEFAULT_SPAN[cycle] - 1))
    if sort_key(cycle, s) < sort_key(cycle, start):
        s = start
    return s, end
