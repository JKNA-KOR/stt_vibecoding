"""보안 회귀 테스트 (SEC-005 ~ SEC-023, Harness §6 / §9 / §11 / §15 / §35).

여기 있는 항목은 한 번 무너지면 조용히 무너지는 것들이다 — 기능 테스트는 그대로
통과하면서 로그에 비밀번호가 남거나, prod 가 DEBUG 로 뜨거나, 경로 검사가 우회된다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import ConfigurationError, Settings
from app.core.exceptions import ValidationError
from app.core.logging import StructuredFormatter, redact
from app.core.security import (
    extract_extension,
    resolve_within,
    sanitize_display_filename,
    verify_password,
)


def _prod(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "prod",
        "debug": False,
        "log_level": "INFO",
        "session_secret": SecretStr("s" * 40),
        "session_cookie_secure": True,
    }
    base.update(overrides)
    return Settings(**base)


# --- 운영 환경 설정 강제 (Harness §35 / §9 / §11) ------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"debug": True}, "DEBUG"),
        ({"log_level": "DEBUG"}, "LOG_LEVEL"),
        ({"session_secret": SecretStr("short")}, "SESSION_SECRET"),
        ({"session_cookie_secure": False}, "SESSION_COOKIE_SECURE"),
        ({"cors_allow_origins": "*"}, "CORS"),
        ({"bootstrap_admin_password": SecretStr("left-behind")}, "BOOTSTRAP"),
        ({"queue_backend": "inline"}, "inline"),
        ({"bcrypt_rounds": 6}, "BCRYPT_ROUNDS"),
        ({"enable_llm_correction": True}, "LLM"),
        ({"auth_provider": "oidc"}, "AUTH_PROVIDER"),
    ],
)
def test_production_refuses_unsafe_configuration(
    overrides: dict[str, object], expected: str
) -> None:
    """위험한 설정으로는 기동하지 못한다. 런타임까지 끌고 가지 않는다 (Harness §4.3)."""
    with pytest.raises(ConfigurationError, match=expected):
        _prod(**overrides)


def test_safe_production_configuration_starts() -> None:
    settings = _prod()

    assert settings.is_production is True
    assert settings.cors_origins == []


# --- 경로 안전성 (Harness §6, SEC-005) ----------------------------------------


@pytest.mark.parametrize(
    "attempt",
    [
        "../../etc/passwd",
        "subdir/../../../outside",
        "/etc/shadow",
        "../",
    ],
)
def test_path_traversal_is_blocked(tmp_path: Path, attempt: str) -> None:
    root = tmp_path / "storage"
    root.mkdir()

    with pytest.raises(ValidationError):
        resolve_within(root, attempt)


def test_percent_encoded_traversal_stays_inside_root(tmp_path: Path) -> None:
    """URL 디코딩은 웹 프레임워크의 몫이다.

    인코딩된 문자열이 그대로 도달하면 리터럴 디렉터리명이 되어 루트를 벗어나지 못한다.
    이 계층이 디코딩을 흉내 내면 오히려 이중 디코딩 취약점이 생긴다.
    """
    root = tmp_path / "storage"
    root.mkdir()

    resolved = resolve_within(root, "..%2f..%2fetc")

    assert resolved.is_relative_to(root.resolve())


def test_symlink_escape_is_blocked(tmp_path: Path) -> None:
    """`resolve()` 후 검사하므로 심볼릭 링크 우회도 막힌다."""
    root = tmp_path / "storage"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside)

    with pytest.raises(ValidationError):
        resolve_within(root, "link/secret.txt")


def test_resolve_within_allows_legitimate_paths(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()

    assert resolve_within(root, "2026/09/abc.wav").is_relative_to(root.resolve())


@pytest.mark.parametrize(
    ("raw", "forbidden"),
    [
        ("../../etc/passwd.wav", ".."),
        ("C:\\Windows\\system32\\evil.wav", "\\"),
        ("normal/../../escape.wav", ".."),
    ],
)
def test_display_filename_is_sanitized(raw: str, forbidden: str) -> None:
    cleaned = sanitize_display_filename(raw)

    assert forbidden not in cleaned
    assert "/" not in cleaned


def test_double_extension_is_detected(tmp_path: Path) -> None:
    """`audio.mp3.exe` 의 실제 확장자는 exe 다 (Harness §6)."""
    assert extract_extension("audio.mp3.exe") == "exe"
    assert extract_extension("call.WAV") == "wav"
    assert extract_extension("noext") == ""


# --- 로그 마스킹 (Harness §15, SEC-022) ----------------------------------------


@pytest.mark.parametrize(
    "key",
    ["password", "SESSION_SECRET", "api_key", "authorization", "cookie", "transcript", "segments"],
)
def test_sensitive_keys_are_redacted(key: str) -> None:
    assert redact("super-secret-value", key) == "[REDACTED]"


def test_redaction_is_recursive() -> None:
    payload = {"outer": {"password": "hunter2", "safe": "ok"}}

    result = redact(payload)

    assert result["outer"]["password"] == "[REDACTED]"
    assert result["outer"]["safe"] == "ok"


def test_long_values_are_truncated() -> None:
    """Transcript 전문이 우발적으로 흘러드는 것을 길이로도 막는다."""
    result = redact("가" * 5000, "some_field")

    assert len(result) < 1000
    assert "truncated" in result


def test_formatter_omits_stacktrace_in_production() -> None:
    """Harness §35: 운영 로그에 스택트레이스를 싣지 않는다."""
    formatter = StructuredFormatter(
        service="stt", environment="prod", application_version="1.0.0"
    )
    try:
        raise RuntimeError("boom with /internal/path detail")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
        )

    payload = json.loads(formatter.format(record))

    assert payload["exception_type"] == "RuntimeError"
    assert "stacktrace" not in payload


def test_formatter_keeps_stacktrace_outside_production() -> None:
    formatter = StructuredFormatter(
        service="stt", environment="dev", application_version="1.0.0"
    )
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
        )

    assert "stacktrace" in json.loads(formatter.format(record))


# --- 비밀번호 검증 (Harness §9) ------------------------------------------------


def test_broken_hash_does_not_raise() -> None:
    """해시 형식이 깨져 있어도 예외를 흘리지 않고 인증 실패로 다룬다."""
    assert verify_password("anything", "not-a-bcrypt-hash") is False
    assert verify_password("anything", "") is False
