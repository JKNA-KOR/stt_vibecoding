"""STT 엔진 선택 (Harness §2.2 / §4.3 / §35).

엔진 종류는 설정으로만 결정되고, 상위 계층은 어떤 구현체가 선택되었는지 몰라도 된다.
프로세스당 하나의 인스턴스를 재사용한다 — 모델 로드가 무겁고, Worker 프로세스 수와
동시 실행 수는 `STT_MAX_CONCURRENT_JOBS` 로 이미 제한되기 때문이다 (Harness §24).
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from app.core.config import ConfigurationError, Settings
from app.core.logging import get_logger
from app.stt.base import STTEngine
from app.stt.faster_whisper_engine import FasterWhisperEngine
from app.stt.mock_engine import MockSTTEngine

logger = get_logger(__name__)

# 새 엔진은 여기에만 등록하면 된다. 등록되지 않은 이름은 기동 시점에 거부된다.
_REGISTRY: dict[str, Callable[[Settings], STTEngine]] = {
    FasterWhisperEngine.engine_name: FasterWhisperEngine,
    MockSTTEngine.engine_name: MockSTTEngine,
}

_instance: STTEngine | None = None
_instance_lock = threading.Lock()


def create_engine(settings: Settings) -> STTEngine:
    """설정에 맞는 엔진 인스턴스를 새로 만든다. 모델 로드는 하지 않는다.

    Raises:
        ConfigurationError: 알 수 없는 엔진이거나, 운영 환경에서 Mock 을 선택한 경우.
    """
    name = settings.stt_engine

    if name == MockSTTEngine.engine_name and settings.is_production:
        # Harness §4.3 / §35: 운영 환경이 가짜 결과를 내는 것은 조용한 실패 중 최악이다.
        # 설정 실수를 런타임까지 끌고 가지 않고 기동 시점에 막는다.
        raise ConfigurationError(
            "prod 환경에서 STT_ENGINE='mock' 은 허용되지 않는다. "
            "Mock 엔진은 실제 음성을 전사하지 않는다."
        )

    factory = _REGISTRY.get(name)
    if factory is None:
        raise ConfigurationError(
            f"알 수 없는 STT_ENGINE='{name}'. 사용 가능: {', '.join(sorted(_REGISTRY))}"
        )

    logger.info(
        "stt engine selected",
        extra={"event": "STT_ENGINE_SELECTED", "engine": name},
    )
    return factory(settings)


def get_engine(settings: Settings) -> STTEngine:
    """프로세스 단위로 공유되는 엔진 인스턴스를 반환한다."""
    global _instance
    if _instance is not None:
        return _instance
    with _instance_lock:
        if _instance is None:
            _instance = create_engine(settings)
    return _instance


def reset_engine() -> None:
    """공유 인스턴스를 폐기한다.

    테스트 격리와, 관리자 화면의 모델 재로드(FR-M-002)에서 사용한다.
    """
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.unload()
        _instance = None
