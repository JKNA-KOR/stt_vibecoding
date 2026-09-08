"""실시간 전사 세션 테스트 (Harness §6 / §21 / §24).

세션이 지켜야 하는 것은 세 가지다 — 입력을 믿지 않는 것, 조각 경계를 정확히 자르는 것,
그리고 **음성 조각을 디스크에 남기지 않는 것**.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.realtime.session import RealtimeSession, silence
from app.stt.factory import create_engine

_RATE = 16000


def _session(settings: Settings, tmp_path: Path, *, sample_rate: int = _RATE) -> RealtimeSession:
    return RealtimeSession(
        settings=settings,
        engine=create_engine(settings),
        sample_rate=sample_rate,
        temp_dir=tmp_path / "realtime",
    )


@pytest.fixture
def rt_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"enable_realtime_stt": True, "realtime_segment_seconds": 2}
    )


# --- 입력 검증 (Harness §6) --------------------------------------------------------


@pytest.mark.parametrize("rate", [0, 4000, 96000, -16000])
def test_out_of_range_sample_rate_is_refused(
    rt_settings: Settings, tmp_path: Path, rate: int
) -> None:
    """클라이언트가 보낸 값이다. 범위를 벗어나면 WAV 헤더가 망가진다."""
    with pytest.raises(ValidationError):
        _session(rt_settings, tmp_path, sample_rate=rate)


def test_misaligned_chunk_is_refused(rt_settings: Settings, tmp_path: Path) -> None:
    """16bit 정렬이 어긋나면 이후 모든 표본이 밀린다. 조용히 잘라 붙이지 않는다."""
    session = _session(rt_settings, tmp_path)

    with pytest.raises(ValidationError, match="형식"):
        session.add_chunk(b"\x00\x01\x02")


def test_oversized_chunk_is_refused(rt_settings: Settings, tmp_path: Path) -> None:
    session = _session(rt_settings, tmp_path)

    with pytest.raises(ValidationError):
        session.add_chunk(b"\x00" * (2 * 1024 * 1024))


# --- 조각 경계 -------------------------------------------------------------------


def test_segment_is_not_transcribed_before_it_is_full(
    rt_settings: Settings, tmp_path: Path
) -> None:
    session = _session(rt_settings, tmp_path)
    session.add_chunk(silence(_RATE, 1.0))

    assert session.has_full_segment is False
    assert session.transcribe_ready_segment() is None


def test_full_segment_is_transcribed_and_consumed(
    rt_settings: Settings, tmp_path: Path
) -> None:
    session = _session(rt_settings, tmp_path)
    session.add_chunk(silence(_RATE, 5.0))

    first = session.transcribe_ready_segment()

    assert first is not None
    assert first.index == 1
    assert first.start_seconds == 0.0
    assert first.end_seconds == 2.0
    # 소비된 만큼만 줄어든다. 남은 3초는 다음 조각의 몫이다.
    assert session.elapsed_seconds == 5.0


def test_segments_are_timed_consecutively(rt_settings: Settings, tmp_path: Path) -> None:
    """조각 시각이 이어지지 않으면 화면의 타임라인이 어긋난다."""
    session = _session(rt_settings, tmp_path)
    session.add_chunk(silence(_RATE, 6.0))

    results = []
    while session.has_full_segment:
        results.append(session.transcribe_ready_segment())

    assert [r.start_seconds for r in results] == [0.0, 2.0, 4.0]
    assert [r.end_seconds for r in results] == [2.0, 4.0, 6.0]


def test_flush_transcribes_the_tail(rt_settings: Settings, tmp_path: Path) -> None:
    session = _session(rt_settings, tmp_path)
    session.add_chunk(silence(_RATE, 1.2))

    tail = session.flush()

    assert tail is not None
    assert tail.end_seconds == pytest.approx(1.2, abs=0.05)


def test_flush_drops_a_scrap_too_short_to_mean_anything(
    rt_settings: Settings, tmp_path: Path
) -> None:
    """0.2초짜리 조각은 대개 잡음만 남긴다. 그 결과가 화면 끝에 붙으면 오독을 부른다."""
    session = _session(rt_settings, tmp_path)
    session.add_chunk(silence(_RATE, 0.2))

    assert session.flush() is None


# --- 음성 보관 (Harness §21 / §22) --------------------------------------------------


def test_audio_scraps_do_not_survive_transcription(
    rt_settings: Settings, tmp_path: Path
) -> None:
    """실시간 화면은 보관 대상이 아니다. 참조 없는 Confidential 파일을 남기지 않는다."""
    temp_dir = tmp_path / "realtime"
    session = _session(rt_settings, tmp_path)
    session.add_chunk(silence(_RATE, 5.0))

    while session.has_full_segment:
        session.transcribe_ready_segment()
    session.flush()

    assert list(temp_dir.glob("*.wav")) == []


def test_audio_scrap_is_removed_even_when_transcription_fails(
    rt_settings: Settings, tmp_path: Path
) -> None:
    """전사가 실패해도 음성 조각은 남기지 않는다."""

    class _BrokenEngine:
        engine_name = "broken"

        def transcribe(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("engine exploded")

    temp_dir = tmp_path / "realtime"
    session = RealtimeSession(
        settings=rt_settings,
        engine=_BrokenEngine(),  # type: ignore[arg-type]
        sample_rate=_RATE,
        temp_dir=temp_dir,
    )
    session.add_chunk(silence(_RATE, 3.0))

    with pytest.raises(RuntimeError):
        session.transcribe_ready_segment()

    assert list(temp_dir.glob("*.wav")) == []
