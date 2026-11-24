"""경로·버전·API 키 읽기.

API 키는 각자 기관에서 받아 넣는다(저장소에는 키가 없다). 키를 찾는 순서
1. 환경변수 (ECOS_API_KEY, KOSIS_API_KEY, OPENDART_API_KEY, DATA_GO_KR_SERVICE_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY)
2. 저장소 맨 위의 .env 파일 (.env.example 을 복사해 값을 채운다. .gitignore 로 올라가지 않음)
키 값은 화면·파일·로그 어디에도 적지 않는다(오류 메시지 속 키도 scrub()으로 가린다).
"""
from __future__ import annotations

import os
from pathlib import Path

VERSION = "0.4.0"

PROGRAM_DIR = Path(__file__).resolve().parent.parent          # 저장소 맨 위
DATA_DIR = PROGRAM_DIR / "data"
STATIC_DIR = PROGRAM_DIR / "static"
CATALOG_DB = Path(os.environ.get("DATAHUB_CATALOG_DB") or DATA_DIR / "catalog.sqlite")   # 시험용 사본을 쓸 때만 환경변수로 바꿈
SYNONYM_FILE = DATA_DIR / "synonyms.csv"
# 시험용 사본 색인(DATAHUB_CATALOG_DB)으로 켜면 다운로드 기록도 그 옆에 남긴다(실제 기록 파일에 시험 흔적이 섞이지 않게)
DOWNLOAD_LOG = (CATALOG_DB.parent if os.environ.get("DATAHUB_CATALOG_DB") else DATA_DIR) / "download_log.csv"
LOCAL_ENV = PROGRAM_DIR / ".env"

KEY_NAMES = {"ECOS": "ECOS_API_KEY", "KOSIS": "KOSIS_API_KEY", "DART": "OPENDART_API_KEY",
             "AI": "ANTHROPIC_API_KEY",            # AI 추천(Claude, 선택)
             "OPENAI": "OPENAI_API_KEY",           # AI 추천에서 GPT를 고를 때(선택)
             "PUBLIC": "DATA_GO_KR_SERVICE_KEY"}    # 공공데이터포털 오픈API(기업집단포털 등)


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if v:
                out[k.strip()] = v
    except OSError:
        pass
    return out


_KEY_CACHE: dict[str, str] = {}


def get_api_key(source: str) -> str | None:
    """키를 한 번 찾으면 프로그램이 켜져 있는 동안 기억한다(바꾼 키는 다시 켜면 반영)."""
    if source in _KEY_CACHE:
        return _KEY_CACHE[source]
    key = _find_api_key(source)
    if key:
        _KEY_CACHE[source] = key
    return key


def _find_api_key(source: str) -> str | None:
    name = KEY_NAMES[source]
    if os.environ.get(name):
        return os.environ[name]
    return _read_env_file(LOCAL_ENV).get(name)


def scrub(text: str) -> str:
    """지금까지 읽은 키(원문·URL 인코딩형)를 모두 '<KEY>'로 가린다. 원천 오류 메시지가 키를 되돌려 줘도 새지 않게."""
    import re
    from urllib.parse import quote, quote_plus

    def pattern(form: str) -> str:
        # '%2B' 같은 퍼센트 인코딩은 16진수 대소문자를 가리지 않고(%2b·%2B·혼합), 나머지 글자는 그대로 맞춘다
        out, i = [], 0
        while i < len(form):
            if form[i] == "%" and re.fullmatch(r"[0-9A-Fa-f]{2}", form[i + 1:i + 3]):
                out.append("%" + "".join(f"[{c.upper()}{c.lower()}]" if c.isalpha() else c for c in form[i + 1:i + 3]))
                i += 3
            else:
                out.append(re.escape(form[i]))
                i += 1
        return "".join(out)

    for k in list(_KEY_CACHE.values()):
        if k:
            encoded = {quote(k, safe=""), quote_plus(k), quote(k)} - {k}
            for form in sorted(encoded, key=len, reverse=True):
                text = re.sub(pattern(form), "<KEY>", text)   # 인코딩형: %HH 대소문자 무시
            text = text.replace(k, "<KEY>")                     # 원문: 정확히 같을 때만
    return text


def mask(text: str, *secrets: str | None) -> str:
    """오류 메시지·기록에 키가 섞여 나가지 않게 가린다."""
    for s in secrets:
        if s:
            text = text.replace(s, "<KEY>")
    return text
