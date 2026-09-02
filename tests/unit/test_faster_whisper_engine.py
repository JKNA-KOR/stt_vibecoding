"""faster-whisper 구현체 테스트.

실제 모델 아티팩트가 필요한 전사 경로는 Golden 테스트(`tests/golden/`)가 담당한다.
여기서는 모델 없이도 확인해야 하는 계약(상태 보고와 오류 분류)만 다룬다.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from app.core.config import Settings
from app.core.exceptions import ErrorCode, STTModelError
from app.stt.faster_whisper_engine import FasterWhisperEngine


@pytest.fixture
def engine(settings: Settings) -> FasterWhisperEngine:
    return FasterWhisperEngine(settings.model_copy(update={"stt_engine": "faster-whisper"}))


def test_describe_before_load_reports_unloaded(engine: FasterWhisperEngine) -> None:
    """모델을 올리지 않은 상태에서도 `/ready` 가 상태를 답할 수 있어야 한다 (FR-H-002)."""
    description = engine.describe()

    assert description.engine == "faster-whisper"
    assert description.is_loaded is False
    # 로드 전에 아티팩트 해시를 아는 척하지 않는다 (Harness §20).
    assert description.model_artifact_hash == "unloaded"


def test_describe_does_not_leak_paths(engine: FasterWhisperEngine) -> None:
    """Health/관리자 응답에 내부 경로가 섞이면 안 된다 (Harness §44)."""
    values = [str(value) for value in asdict(engine.describe()).values()]

    assert not any(value.startswith("/") for value in values)


def test_missing_artifact_is_classified_as_model_error(
    engine: FasterWhisperEngine, tmp_path
) -> None:
    """아티팩트를 못 찾으면 조용히 넘어가지 않고 분류된 오류로 실패한다 (Harness §4.3 / §23)."""
    engine._settings = engine._settings.model_copy(  # noqa: SLF001
        update={
            "stt_model_path": str(tmp_path / "does-not-exist"),
            "stt_allow_model_download": False,
        }
    )

    with pytest.raises(STTModelError) as excinfo:
        engine.load()

    assert excinfo.value.code is ErrorCode.STT_MODEL_ERROR
    # 사용자에게는 내부 경로가 아니라 분류 문구만 전달된다 (Harness §44).
    assert str(tmp_path) not in excinfo.value.message
