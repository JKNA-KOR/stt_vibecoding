"""신뢰도 필터 테스트 (Harness §4.3 / §36 / §50).

무음 구간 환각("감사합니다", "자막제공자" 등)을 겨냥한 기능이다. **발화를 지우는
동작이므로** 안전장치가 실제로 동작하는지가 이 파일의 주제다.
"""

from __future__ import annotations

import pytest

from app.stt.normalization import drop_low_confidence, normalize_segments
from app.stt.schemas import TranscriptSegment


def _segment(index: int, text: str, confidence: float | None) -> TranscriptSegment:
    return TranscriptSegment(
        index=index,
        start=float(index),
        end=float(index + 1),
        text=text,
        confidence=confidence,
    )


# --- 기본 동작 ------------------------------------------------------------------


def test_filter_is_off_by_default() -> None:
    """발화를 지우는 기능이 아무 설정 없이 도는 일은 없어야 한다."""
    segments = [_segment(0, "정상 발화", 0.82), _segment(1, "환각", 0.20)]

    assert drop_low_confidence(segments, 0.0) == segments


def test_hallucination_below_threshold_is_dropped() -> None:
    """실측: 정상 발화 0.82 / 무음 구간 환각 0.20."""
    segments = [
        _segment(0, "네 고객센터입니다", 0.815),
        _segment(1, "주문번호를 알려주시겠어요", 0.817),
        _segment(2, "감사합니다", 0.202),
    ]

    kept = drop_low_confidence(segments, 0.3)

    assert [s.text for s in kept] == ["네 고객센터입니다", "주문번호를 알려주시겠어요"]


@pytest.mark.parametrize("threshold", [0.3, 0.5, 0.8])
def test_segments_at_the_threshold_are_kept(threshold: float) -> None:
    """경계값은 남긴다. 애매하면 지우지 않는 쪽으로 튼다."""
    segments = [_segment(0, "경계", threshold)]

    assert drop_low_confidence(segments, threshold) == segments


# --- 안전장치 (Harness §4.3) -------------------------------------------------------


def test_segments_without_confidence_are_never_dropped() -> None:
    """값을 주지 않는 엔진이 있다. 모른다고 버리면 전사가 통째로 사라진다."""
    segments = [_segment(0, "확신도 없음", None), _segment(1, "환각", 0.1)]

    kept = drop_low_confidence(segments, 0.5)

    assert [s.text for s in kept] == ["확신도 없음"]


def test_nothing_is_dropped_when_everything_would_be() -> None:
    """빈 Transcript 는 '발화가 없었다' 로 보인다. 그것은 사실과 다르고 문제를 감춘다."""
    segments = [_segment(0, "저음질", 0.2), _segment(1, "저음질", 0.15)]

    kept = drop_low_confidence(segments, 0.5)

    assert kept == segments


def test_empty_input_is_handled() -> None:
    assert drop_low_confidence([], 0.5) == []


# --- 정규화와의 결합 ---------------------------------------------------------------


def test_normalization_preserves_confidence_and_speaker() -> None:
    """신뢰도와 화자는 정규화가 만들어 내는 값이 아니다. 인덱스를 다시 매겨도 남아야 한다."""
    segments = [
        TranscriptSegment(
            index=0, start=0.0, end=1.0, text="", confidence=0.9, speaker="상담원"
        ),
        TranscriptSegment(
            index=1, start=1.0, end=2.0, text="  발화  ", confidence=0.8, speaker="고객"
        ),
    ]

    cleaned = normalize_segments(segments)

    assert len(cleaned) == 1
    assert cleaned[0].index == 0
    assert cleaned[0].text == "발화"
    assert cleaned[0].confidence == 0.8
    assert cleaned[0].speaker == "고객"


def test_normalization_can_apply_the_filter_directly() -> None:
    segments = [_segment(0, "정상", 0.8), _segment(1, "환각", 0.1)]

    cleaned = normalize_segments(segments, min_confidence=0.3)

    assert [s.text for s in cleaned] == ["정상"]
