"""Mock 엔진 테스트 (NFR-003).

Mock 결과의 결정론성은 통합·회귀 테스트가 기대값을 단언할 수 있는 근거이므로
여기서 명시적으로 고정한다.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.core.config import Settings
from app.stt.mock_engine import MOCK_ENGINE_VERSION, MockSTTEngine
from app.stt.schemas import STTOptions

_OPTIONS = STTOptions(language="ko", beam_size=5, vad_enabled=True)


def test_transcribe_loads_engine_on_demand(
    settings: Settings, make_wav: Callable[..., Path]
) -> None:
    engine = MockSTTEngine(settings)
    assert engine.is_loaded is False

    engine.transcribe(make_wav(seconds=6.0), _OPTIONS)

    assert engine.is_loaded is True


def test_segments_cover_audio_duration(
    settings: Settings, make_wav: Callable[..., Path]
) -> None:
    result = MockSTTEngine(settings).transcribe(make_wav(seconds=12.0), _OPTIONS)

    assert result.segments
    assert 11.9 < result.audio_duration_seconds < 12.1
    assert result.segments[0].start == 0.0
    # 세그먼트가 실제 음성 길이를 넘어가면 SRT/VTT 자막 시각이 어긋난다.
    assert result.segments[-1].end <= result.audio_duration_seconds + 0.01
    assert all(
        earlier.end <= later.start + 0.01
        for earlier, later in zip(result.segments, result.segments[1:], strict=False)
    )


def test_output_is_deterministic(settings: Settings, make_wav: Callable[..., Path]) -> None:
    engine = MockSTTEngine(settings)
    audio = make_wav(seconds=8.0)

    first = engine.transcribe(audio, _OPTIONS)
    second = engine.transcribe(audio, _OPTIONS)

    assert [s.text for s in first.segments] == [s.text for s in second.segments]


def test_different_audio_yields_different_output(
    settings: Settings, make_wav: Callable[..., Path]
) -> None:
    engine = MockSTTEngine(settings)

    first = engine.transcribe(make_wav("a.wav", seconds=8.0, frequency=440.0), _OPTIONS)
    second = engine.transcribe(make_wav("b.wav", seconds=8.0, frequency=880.0), _OPTIONS)

    assert [s.text for s in first.segments] != [s.text for s in second.segments]


def test_progress_reaches_completion(settings: Settings, make_wav: Callable[..., Path]) -> None:
    reported: list[float] = []

    MockSTTEngine(settings).transcribe(make_wav(seconds=8.0), _OPTIONS, progress=reported.append)

    assert reported
    assert reported[-1] == 1.0
    assert all(0.0 <= value <= 1.0 for value in reported)


def test_progress_callback_failure_does_not_break_transcription(
    settings: Settings, make_wav: Callable[..., Path]
) -> None:
    """진행률 보고는 부가 기능이다. 실패해도 전사 결과를 잃어서는 안 된다."""

    def explode(_: float) -> None:
        raise RuntimeError("progress sink is down")

    result = MockSTTEngine(settings).transcribe(
        make_wav(seconds=6.0), _OPTIONS, progress=explode
    )

    assert result.segments


def test_provenance_is_recorded(settings: Settings, make_wav: Callable[..., Path]) -> None:
    """Harness §5.3 / §20: 결과 재현에 필요한 정보가 빠짐없이 남아야 한다."""
    result = MockSTTEngine(settings).transcribe(make_wav(seconds=6.0), _OPTIONS)
    provenance = result.provenance

    assert provenance.engine == "mock"
    assert provenance.model_version == MOCK_ENGINE_VERSION
    assert provenance.beam_size == 5
    assert provenance.vad_enabled is True
    assert provenance.application_version == settings.app_version
    # 가짜 엔진임이 Provenance 에 드러나야 한다. 그럴듯한 해시를 지어내지 않는다.
    assert provenance.model_artifact_hash == "mock"
    assert set(provenance.as_dict()) >= {"engine", "model_artifact_hash", "application_version"}


def test_very_short_audio_is_handled(settings: Settings, make_wav: Callable[..., Path]) -> None:
    result = MockSTTEngine(settings).transcribe(make_wav(seconds=0.05), _OPTIONS)

    assert all(
        segment.end <= result.audio_duration_seconds + 0.01 for segment in result.segments
    )
