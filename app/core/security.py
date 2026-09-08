"""보안 원시 기능 (Harness §6 / §9 / §11).

비밀번호 해싱, 세션·CSRF 토큰 서명, 경로 안전성 검증을 한곳에 모아
개별 라우트가 각자 다른 방식으로 구현하는 것을 막는다.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.exceptions import ValidationError

# bcrypt 는 72바이트를 초과하는 입력을 조용히 잘라낸다.
# 잘림을 모른 채 긴 비밀번호를 허용하면 유효 강도가 사용자 기대와 달라지므로 명시적으로 막는다.
_BCRYPT_MAX_PASSWORD_BYTES = 72
_MIN_PASSWORD_LENGTH = 12

_SESSION_SALT = "stt-service.session.v1"
_CSRF_SALT = "stt-service.csrf.v1"

_UNSAFE_FILENAME_CHARS = re.compile(r"[^\w.\- ()가-힣]", re.UNICODE)


# --- 비밀번호 ---------------------------------------------------------------


def validate_password_strength(password: str) -> None:
    """비밀번호 정책 검증. 위반 시 사용자 입력 오류로 처리한다 (Harness §4.3)."""
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ValidationError(f"비밀번호는 {_MIN_PASSWORD_LENGTH}자 이상이어야 합니다.")
    if len(password.encode("utf-8")) > _BCRYPT_MAX_PASSWORD_BYTES:
        raise ValidationError("비밀번호가 너무 깁니다.")
    classes = sum(
        bool(pattern.search(password))
        for pattern in (
            re.compile(r"[a-z]"),
            re.compile(r"[A-Z]"),
            re.compile(r"[0-9]"),
            re.compile(r"[^A-Za-z0-9]"),
        )
    )
    if classes < 3:
        raise ValidationError(
            "비밀번호는 영문 대/소문자, 숫자, 특수문자 중 3종류 이상을 포함해야 합니다."
        )


DEFAULT_BCRYPT_ROUNDS = 12


def hash_password(password: str, *, rounds: int = DEFAULT_BCRYPT_ROUNDS) -> str:
    """bcrypt 해시를 반환한다. 평문은 어디에도 저장하지 않는다 (Harness §9).

    Args:
        rounds: 비용 계수. 설정(`BCRYPT_ROUNDS`)에서 오며, prod 하한은 `Settings` 가
            강제한다. 해시 문자열에 계수가 포함되므로 값을 올려도 기존 해시는 계속
            검증된다 — 재해싱은 다음 로그인 성공 시점에 하면 된다.
    """
    validate_password_strength(password)
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)
    ).decode("ascii")


@lru_cache(maxsize=4)
def dummy_password_hash(rounds: int = DEFAULT_BCRYPT_ROUNDS) -> str:
    """존재하지 않는 사용자에 대해서도 같은 비용의 검증을 수행하기 위한 해시 (FR-A-006).

    임의 비밀번호로 실제 bcrypt 해시를 한 번 만들어 캐시한다. 형식만 흉내 낸 문자열을 쓰면
    `verify_password` 가 해싱 없이 즉시 False 를 돌려주어, 사용자 존재 여부가 응답 시간
    차이로 드러난다. 이 값은 어떤 입력과도 일치하지 않으며 프로세스마다 다르다.
    """
    return bcrypt.hashpw(
        secrets.token_urlsafe(32).encode("ascii"), bcrypt.gensalt(rounds=rounds)
    ).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """비밀번호를 검증한다. 해시 형식이 깨져 있어도 예외를 밖으로 흘리지 않고 False 를 반환한다.

    여기서 False 를 돌려주는 것은 오류 은폐(Harness §4.3)가 아니라 인증 실패라는
    정상적인 도메인 결과이며, 호출부가 `LOGIN_FAILED` 를 감사 기록한다.
    """
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


# --- 세션 / CSRF 토큰 --------------------------------------------------------


class TokenSigner:
    """세션 및 CSRF 토큰 서명자. Secret 은 설정에서만 주입받는다 (Harness §9)."""

    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("SESSION_SECRET 은 32자 이상이어야 한다 (Harness §9)")
        self._session = URLSafeTimedSerializer(secret, salt=_SESSION_SALT)
        self._csrf = URLSafeTimedSerializer(secret, salt=_CSRF_SALT)

    def issue_session(self, payload: dict[str, Any]) -> str:
        return self._session.dumps(payload)

    def read_session(self, token: str, max_age_seconds: int) -> dict[str, Any] | None:
        """유효한 세션이면 payload 를, 만료/위조면 None 을 반환한다."""
        try:
            data = self._session.loads(token, max_age=max_age_seconds)
        except (BadSignature, SignatureExpired):
            return None
        return data if isinstance(data, dict) else None

    def issue_csrf(self, session_id: str) -> str:
        return self._csrf.dumps({"sid": session_id})

    def verify_csrf(self, token: str, session_id: str, max_age_seconds: int) -> bool:
        """CSRF 토큰이 현재 세션에 바인딩된 것인지 확인한다 (Harness §11 / SEC-035)."""
        try:
            data = self._csrf.loads(token, max_age=max_age_seconds)
        except (BadSignature, SignatureExpired):
            return False
        return isinstance(data, dict) and hmac.compare_digest(str(data.get("sid", "")), session_id)


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


# --- 파일명 / 경로 안전성 (Harness §6) ---------------------------------------


def sanitize_display_filename(raw_name: str, *, max_length: int = 200) -> str:
    """사용자 제공 파일명을 표시 전용으로 정제한다.

    이 값은 화면 표시와 다운로드 파일명에만 쓰이고, 서버 저장 경로에는 절대 쓰이지 않는다.
    저장 경로는 서버가 만든 UUID 만 사용한다 (Harness §6).
    """
    name = unicodedata.normalize("NFC", raw_name or "").strip()
    # 경로 구분자와 상위 디렉터리 참조는 표시 문자열에서도 제거한다.
    name = name.replace("\\", "/").split("/")[-1]
    name = name.replace("..", "_")
    name = _UNSAFE_FILENAME_CHARS.sub("_", name)
    name = name.strip(". ") or "unnamed"
    return name[:max_length]


def extract_extension(raw_name: str) -> str:
    """소문자 확장자(점 제외)를 반환한다. 없으면 빈 문자열."""
    suffix = Path(raw_name.replace("\\", "/").split("/")[-1]).suffix
    return suffix.lower().lstrip(".")


def resolve_within(root: Path, *parts: str) -> Path:
    """`root` 하위로 한정된 경로를 만든다. 벗어나면 즉시 실패한다 (Harness §6, SEC-007).

    `Path.resolve()` 후 `is_relative_to` 로 검사하므로 `..`, 절대경로 주입, 심볼릭 링크
    우회를 모두 차단한다.
    """
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*parts).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise ValidationError(
            "잘못된 경로 요청입니다.",
            internal_detail="path traversal attempt blocked",
        )
    return candidate


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """파일의 SHA-256 을 계산한다 (Harness §45).

    해시는 파일 내용 자체가 아니므로 로그·감사에 사용할 수 있다.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


# --- 응답 보안 헤더 (Harness §11) --------------------------------------------

# Transcript 는 Untrusted Data 이므로(Harness §13) 인라인 스크립트를 허용하지 않는 CSP 를 적용해
# 저장형 XSS 의 실행 경로를 한 번 더 막는다. 프론트엔드는 외부 JS 파일만 사용한다.
# 마이크는 기본적으로 잠가 둔다. 실시간 STT 를 켤 때만 이 출처에 열린다 —
# 기능이 꺼져 있는데 마이크 권한이 열려 있을 이유가 없다 (Harness §11 / §54).
_MICROPHONE_BLOCKED = "microphone=()"
_MICROPHONE_SELF = "microphone=(self)"

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": f"geolocation=(), {_MICROPHONE_BLOCKED}, camera=()",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "media-src 'self'; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}


def security_headers(*, allow_microphone: bool) -> dict[str, str]:
    """설정에 맞춘 응답 헤더.

    바뀌는 것은 마이크 하나뿐이다. 실시간 STT 가 꺼져 있으면 `microphone=()` 로 두어
    화면 코드가 실수로 `getUserMedia` 를 부르더라도 브라우저가 막는다 — 기능 플래그가
    꺼졌는데 권한만 열려 있는 상태를 만들지 않는다 (Harness §54).
    """
    headers = dict(SECURITY_HEADERS)
    if allow_microphone:
        headers["Permissions-Policy"] = headers["Permissions-Policy"].replace(
            _MICROPHONE_BLOCKED, _MICROPHONE_SELF
        )
    return headers
