"""엔진 선택 규칙 테스트 (NFR-002 / NFR-003, Harness §2.2 / §4.3 / §35)."""

from __future__ import annotations

import pytest

from app.core.config import ConfigurationError, Settings
from app.stt.base import EngineDescription
from app.stt.factory import create_engine, get_engine, reset_engine
from app.stt.groq_whisper_engine import GroqWhisperEngine
from app.stt.mock_engine import MockSTTEngine


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    reset_engine()
    yield
    reset_engine()


def _prod_settings(**overrides: object) -> Settings:
    """Harness 검증을 통과하는 최소 prod 설정."""
    base: dict[str, object] = {
        "app_env": "prod",
        "debug": False,
        "log_level": "INFO",
        "session_secret": "s" * 40,
        "session_cookie_secure": True,
    }
    base.update(overrides)
    return Settings(**base)


def test_mock_engine_is_selected_by_config(settings: Settings) -> None:
    engine = create_engine(settings)

    assert isinstance(engine, MockSTTEngine)
    assert engine.is_loaded is False


def test_describe_works_before_load(settings: Settings) -> None:
    """`/ready` 는 모델이 올라가기 전에도 상태를 답할 수 있어야 한다 (FR-H-002)."""
    description = create_engine(settings).describe()

    assert isinstance(description, EngineDescription)
    assert description.is_loaded is False


def test_mock_engine_is_rejected_in_production() -> None:
    """운영 환경이 가짜 결과를 내는 일은 기동 시점에 막는다 (Harness §4.3 / §35)."""
    with pytest.raises(ConfigurationError, match="mock"):
        create_engine(_prod_settings(stt_engine="mock"))


def test_unknown_engine_is_rejected(settings: Settings) -> None:
    unknown = settings.model_copy(update={"stt_engine": "whisper-cpp"})

    with pytest.raises(ConfigurationError, match="whisper-cpp"):
        create_engine(unknown)


def test_get_engine_reuses_one_instance(settings: Settings) -> None:
    """모델 로드는 무거우므로 프로세스당 하나만 둔다 (Harness §24)."""
    assert get_engine(settings) is get_engine(settings)


def test_reset_engine_discards_instance(settings: Settings) -> None:
    first = get_engine(settings)
    first.load()
    reset_engine()

    assert first.is_loaded is False
    assert get_engine(settings) is not first


# --- 외부 전사 API (Harness §8.2, SEC-021) ---------------------------------------
#
# 음성 원본이 사내를 벗어나는 결정이다. 설정 실수로 그렇게 되는 일은 없어야 한다.


def _remote_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "stt_engine": "groq-whisper",
        "stt_api_key": "gsk-test-key",
        "stt_model_name": "whisper-large-v3",
        "allow_external_stt": True,
        "session_secret": "test-session-secret-value-at-least-32-chars",
    }
    base.update(overrides)
    return Settings(**base)


def test_remote_engine_is_selected_by_config() -> None:
    """새 엔진을 붙이는 데 상위 계층 수정이 필요 없어야 한다 (NFR-002)."""
    engine = create_engine(_remote_settings())

    assert isinstance(engine, GroqWhisperEngine)
    assert engine.is_loaded is False


def test_remote_engine_needs_explicit_approval_to_send_audio_outside() -> None:
    with pytest.raises(ConfigurationError, match="ALLOW_EXTERNAL_STT"):
        _remote_settings(allow_external_stt=False)


def test_remote_engine_without_a_key_fails_at_startup() -> None:
    """키 없이 떠 있다가 첫 전사에서 401 로 드러나는 것보다 낫다 (Harness §4.3)."""
    with pytest.raises(ConfigurationError, match="STT_API_KEY"):
        _remote_settings(stt_api_key="")


def test_plaintext_http_endpoint_is_refused() -> None:
    """평문 구간을 지나면 녹취 음성이 그대로 노출된다 (Harness §8.2)."""
    with pytest.raises(ConfigurationError, match="https"):
        _remote_settings(stt_api_base_url="http://api.example.com/v1")


def test_local_http_endpoint_is_allowed() -> None:
    """사내에 세운 호환 엔드포인트까지 막을 이유는 없다."""
    settings = _remote_settings(stt_api_base_url="http://stt.internal:8000/v1")

    assert isinstance(create_engine(settings), GroqWhisperEngine)


def test_local_engine_is_unaffected_by_the_external_flag() -> None:
    """기본값은 여전히 로컬이다. 음성은 사내를 벗어나지 않는다."""
    settings = Settings(app_env="test", session_secret="t" * 40)

    assert settings.stt_engine == "faster-whisper"
    assert settings.allow_external_stt is False
