"""변환 정확도(추정) 계산 테스트 (Harness §4.3 / §20).

이 값은 **모델이 스스로 매긴 확신도**다. 측정된 정확도가 아니며, 없을 때 0 으로 내려
쓰면 "정확도 0"으로 오해된다 — 그 경계가 이 파일의 주제다.
"""

from __future__ import annotations

import math

import pytest

from app.stt.groq_whisper_engine import _confidence_of as groq_confidence
from app.stt.schemas import ModelProvenance, TranscriptionResult, TranscriptSegment


def _result(*segments: TranscriptSegment) -> TranscriptionResult:
    return TranscriptionResult(
        segments=list(segments),
        detected_language="ko",
        language_probability=None,
        audio_duration_seconds=10.0,
        provenance=ModelProvenance(
            engine="test",
            model_name="test",
            model_version="test",
            model_artifact_hash="test",
            compute_type="none",
            device_type="none",
            language="ko",
            beam_size=1,
            vad_enabled=False,
            application_version="1.0.0",
        ),
    )


def _segment(index: int, start: float, end: float, confidence: float | None) -> TranscriptSegment:
    return TranscriptSegment(
        index=index, start=start, end=end, text="발화", confidence=confidence
    )


# --- 집계 ------------------------------------------------------------------------


def test_no_confidence_anywhere_stays_none() -> None:
    """값을 주지 않는 엔진이 있다. 0 으로 내려 쓰면 '확신도 0'으로 오해된다."""
    result = _result(_segment(0, 0.0, 5.0, None), _segment(1, 5.0, 10.0, None))

    assert result.mean_confidence is None


def test_empty_transcript_has_no_confidence() -> None:
    assert _result().mean_confidence is None


def test_confidence_is_weighted_by_duration() -> None:
    """0.5초짜리 감탄사와 20초짜리 설명이 같은 무게를 가지면 전체 인상이 왜곡된다."""
    result = _result(
        _segment(0, 0.0, 1.0, 0.2),   # 1초, 낮음
        _segment(1, 1.0, 10.0, 0.9),  # 9초, 높음
    )

    # 단순 평균이면 0.55 다. 길이 가중이면 0.83 쪽으로 붙는다.
    assert result.mean_confidence == pytest.approx(0.83, abs=0.01)


def test_segments_without_confidence_are_skipped_not_counted_as_zero() -> None:
    result = _result(
        _segment(0, 0.0, 5.0, 0.9),
        _segment(1, 5.0, 10.0, None),
    )

    assert result.mean_confidence == pytest.approx(0.9, abs=0.001)


# --- 엔진 응답 변환 ---------------------------------------------------------------


def test_average_logprob_becomes_a_probability() -> None:
    raw = {"avg_logprob": -0.2}

    assert groq_confidence(raw) == pytest.approx(math.exp(-0.2), abs=0.001)


def test_silence_probability_pulls_the_confidence_down() -> None:
    """무음 구간의 환각은 확신도가 높게 나오기도 한다. no_speech_prob 만큼 깎는다."""
    confident = groq_confidence({"avg_logprob": -0.1})
    hallucinated = groq_confidence({"avg_logprob": -0.1, "no_speech_prob": 0.9})

    assert hallucinated < confident
    assert hallucinated == pytest.approx(confident * 0.1, abs=0.001)


def test_missing_logprob_is_not_invented() -> None:
    assert groq_confidence({}) is None
    assert groq_confidence({"avg_logprob": "높음"}) is None
    assert groq_confidence({"avg_logprob": True}) is None


def test_confidence_stays_within_zero_and_one() -> None:
    # 양수 로그확률은 정상 응답에서 나오지 않지만, 와도 1 을 넘지 않아야 한다.
    assert groq_confidence({"avg_logprob": 2.0}) == 1.0
    assert groq_confidence({"avg_logprob": -20.0}) >= 0.0
