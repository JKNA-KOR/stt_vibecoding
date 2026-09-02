"""정규화 규칙 단위 테스트 (FR-T-001 / FR-T-006).

규칙을 바꾸면 이 파일과 `NORMALIZER_VERSION` 이 함께 갱신되어야 한다 (Harness §36).
"""

from __future__ import annotations

import pytest

from app.stt.normalization import (
    NORMALIZER_NAME,
    NORMALIZER_VERSION,
    lineage_metadata,
    normalize_segments,
    normalize_text,
)
from app.stt.schemas import TranscriptSegment


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("안녕\n하세요", "안녕 하세요"),
        ("  가\t\t나   다  ", "가 나 다"),
        ("가​나", "가나"),
        ("﻿시작", "시작"),
        # NFD 자모 분리형은 완성형으로 통일된다.
        ("가", "가"),
        ("네...... 알겠습니다", "네... 알겠습니다"),
        # 3개까지는 실제 발화 표기일 수 있으므로 보존한다.
        ("네... 네", "네... 네"),
        ("정말요!!!!!", "정말요!!!"),
        ("", ""),
    ],
)
def test_normalize_text(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


def test_normalize_text_does_not_change_words() -> None:
    """정규화는 표기만 정리하고 어휘를 바꾸지 않는다 (Harness §2.1: 교정은 범위 밖)."""
    sentence = "음 그니까 그게요 어제 말씀하신 그 건이요"
    assert normalize_text(sentence) == sentence


def test_normalize_segments_drops_empty_and_reindexes() -> None:
    segments = [
        TranscriptSegment(0, 0.0, 2.0, " 안녕하세요 "),
        TranscriptSegment(1, 2.0, 4.0, "   "),
        TranscriptSegment(2, 4.0, 6.0, "네 알겠습니다"),
    ]
    result = normalize_segments(segments)

    assert [s.text for s in result] == ["안녕하세요", "네 알겠습니다"]
    assert [s.index for s in result] == [0, 1]


def test_normalize_segments_merges_repeated_text() -> None:
    """Whisper 의 반복 생성 구간을 하나로 합치되 시각 범위는 보존한다."""
    segments = [
        TranscriptSegment(0, 4.0, 6.0, "네 알겠습니다"),
        TranscriptSegment(1, 6.0, 8.0, "네 알겠습니다"),
        TranscriptSegment(2, 8.0, 10.0, "네 알겠습니다"),
        TranscriptSegment(3, 10.0, 12.0, "감사합니다"),
    ]
    result = normalize_segments(segments)

    assert len(result) == 2
    assert result[0].start == 4.0
    assert result[0].end == 10.0


def test_normalize_segments_keeps_non_adjacent_repeats() -> None:
    """떨어져 있는 같은 문장은 실제 발화일 수 있으므로 합치지 않는다."""
    segments = [
        TranscriptSegment(0, 0.0, 2.0, "네"),
        TranscriptSegment(1, 2.0, 4.0, "확인했습니다"),
        TranscriptSegment(2, 4.0, 6.0, "네"),
    ]
    assert len(normalize_segments(segments)) == 3


def test_normalize_segments_fixes_inverted_timestamps() -> None:
    segments = [TranscriptSegment(0, 8.0, 7.0, "감사합니다")]
    result = normalize_segments(segments)

    assert result[0].end >= result[0].start


def test_normalize_segments_does_not_mutate_input() -> None:
    """RAW Transcript 는 보존되어야 한다 (Harness §50, FR-T-002)."""
    segments = [TranscriptSegment(0, 0.0, 2.0, " 안녕하세요 ")]
    normalize_segments(segments)

    assert segments[0].text == " 안녕하세요 "


def test_lineage_metadata_records_processor_version() -> None:
    """Harness §51 이 요구하는 계보 필드."""
    metadata = lineage_metadata(input_version="cfg-abc123")

    assert metadata["processor"] == NORMALIZER_NAME
    assert metadata["processor_version"] == NORMALIZER_VERSION
    assert metadata["input_version"] == "cfg-abc123"
    # created_at 은 저장 시점에 TranscriptStore 가 붙인다. 여기서 만들면 시각이 두 개가 된다.
    assert "created_at" not in metadata
