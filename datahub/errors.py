"""원천 API 오류를 사용자가 구분할 수 있는 종류로 나눈다.

kind
- auth        인증 오류(키 없음·틀림·만료)
- no_data     조회 결과 없음(정상 응답이지만 해당 조건에 자료가 없음)
- not_found   없는 통계표(통계표 코드·ID가 원천에 없음)
- bad_period  기간 형식 오류(없는 날짜, 주기와 맞지 않는 형식)
- bad_request 요청 오류(통계표·항목 코드나 기간 형식이 맞지 않음)
- too_large   조회 범위 초과(한 번에 받을 수 있는 양을 넘음)
- rate_limit  호출 한도 초과
- server      원천 기관 서버 오류
- network     연결 실패·시간 초과
"""
from __future__ import annotations

KIND_LABEL = {
    "auth": "인증 오류",
    "no_data": "조회 결과 없음",
    "not_found": "대상 없음",           # 없는 통계표·회사
    "bad_period": "기간 형식 오류",
    "bad_request": "요청 오류",
    "too_large": "조회 범위 초과",
    "rate_limit": "호출 한도 초과",
    "server": "원천 서버 오류",
    "network": "연결 실패",
}


class SourceError(Exception):
    def __init__(self, source: str, kind: str, message: str, code: str | None = None):
        from .config import scrub

        message = scrub(str(message))
        super().__init__(message)
        self.source = source
        self.kind = kind
        self.message = message
        self.code = scrub(str(code)) if code is not None else None   # 원천이 준 오류 코드 칸도 가린다

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "kind": self.kind,
            "label": KIND_LABEL.get(self.kind, self.kind),
            "message": self.message,
            "code": self.code,
        }


# ECOS 응답 코드 → 종류 (ECOS 개발 가이드의 결과 코드)
ECOS_CODES = {
    "INFO-100": "auth",
    "INFO-200": "no_data",
    "ERROR-100": "bad_request",
    "ERROR-101": "bad_request",
    "ERROR-200": "bad_request",
    "ERROR-300": "bad_request",
    "ERROR-301": "bad_request",
    "ERROR-400": "too_large",
    "ERROR-500": "server",
    "ERROR-600": "server",
    "ERROR-601": "server",
    "ERROR-602": "rate_limit",
}

# KOSIS 오류 코드 → 종류 (KOSIS 공유서비스 오류 메시지 표)
KOSIS_CODES = {
    "10": "auth",
    "11": "auth",
    "12": "auth",            # 인증키 기간만료(KOSIS FAQ)
    "20": "bad_request",
    "21": "bad_request",
    "30": "no_data",
    "31": "too_large",
    "40": "rate_limit",
    "41": "rate_limit",
    "42": "rate_limit",
    "50": "server",
}

# OpenDART 응답 status → 종류 (OpenDART 개발가이드 '메시지 설명')
DART_CODES = {
    "010": "auth",           # 등록되지 않은 키
    "011": "auth",           # 사용할 수 없는 키(일시 중지)
    "012": "auth",           # 접근할 수 없는 IP
    "901": "auth",           # 개인정보 보유기간 만료로 사용할 수 없는 키
    "013": "no_data",        # 조회된 데이터가 없음
    "014": "not_found",      # 파일이 존재하지 않음
    "020": "rate_limit",     # 요청 제한 초과(일반적으로 하루 20,000건)
    "021": "too_large",      # 조회 가능한 회사 개수 초과(최대 100건)
    "100": "bad_request",    # 필드의 부적절한 값
    "101": "bad_request",    # 부적절한 접근
    "800": "server",         # 시스템 점검
    "900": "server",         # 정의되지 않은 오류
}
