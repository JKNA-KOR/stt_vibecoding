"""엔진 선택 규칙 테스트 (NFR-002 / NFR-003, Harness §2.2 / §4.3 / §35)."""

from __future__ import annotations

import pytest

from app.core.config import ConfigurationError, Settings
from app.stt.base import EngineDescription
from app.stt.factory import create_engine, get_engine, reset_engine
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
