"""Transcript 정규화 (Harness §50 / §51, FR-T-001 / FR-T-006).

정규화는 원본을 **덮어쓰지 않고** 새 산출물을 만든다. RAW 는 그대로 보존되며, 여기서 만든
결과는 `TranscriptKind.NORMALIZED` 로 별도 저장된다 (Harness §50, FR-T-002).

정규화 규칙 변경은 Release 영향 변경이다 (Harness §36, OPS-003). 규칙을 바꾸면
`NORMALIZER_VERSION` 을 올리고 `tests/regression/` 의 회귀 테스트를 재실행해야 한다.

설계 원칙: **발화 내용을 바꾸지 않는다.** 표기 정리(공백·제어문자·유니코드 정규화)만 수행하고,
어휘 치환·맞춤법 교정·요약은 하지 않는다. 그런 변형은 LLM 후처리 단계의 몫이며 본 버전 범위
밖이다 (Harness §2.1).
"""

from __future__ import annotations

import re
import unicodedata

from app.core.logging import get_logger
from app.stt.schemas import TranscriptSegment

logger = get_logger(__name__)

NORMALIZER_NAME = "rule-based-normalizer"

# 규칙이 바뀌면 반드시 증가시킨다 (Harness §20 / §36 / §51).
NORMALIZER_VERSION = "1.1.0"

# 제로폭 문자와 양방향 서식 제어문자. 눈에 보이지 않으면서 검색·비교를 어긋나게 하므로 제거한다.
# 리터럴로 적으면 코드에서 보이지 않아 유지보수가 불가능하므로 코드포인트로 표기한다.
_ZERO_WIDTH = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")

# 개행·탭을 포함한 모든 공백 런을 하나의 스페이스로 접는다.
_WHITESPACE_RUN = re.compile(r"\s+")

# 같은 문장부호가 4개 이상 이어지는 경우만 3개로 줄인다.
# Whisper 가 무음 구간에서 만들어내는 "......." 같은 산출물을 정리하되,
# 사용자가 실제로 말한 억양 표기(".." 나 "!!")는 건드리지 않는다.
_PUNCTUATION_RUN = re.compile(r"([.!?~\-\u2014\u2026])\1{3,}")


def normalize_text(raw: str) -> str:
    """세그먼트 한 줄을 정규화한다. 어휘는 바꾸지 않는다."""
    # NFC 로 통일하지 않으면 자모 분리형(NFD)과 완성형이 서로 다른 문자열로 취급되어
    # 검색(FR-T-007)과 회귀 비교가 어긋난다.
    text = unicodedata.normalize("NFC", raw)
    text = _ZERO_WIDTH.sub("", text)
    # 개행·탭을 먼저 공백으로 접는다. 이 순서를 뒤집으면 아래 제어문자 제거가 줄바꿈을
    # 통째로 삭제해 앞뒤 단어가 붙어버린다.
    text = _WHITESPACE_RUN.sub(" ", text)
    # 남은 인쇄 불가 제어문자(카테고리 C*) 제거. 스페이스는 위에서 이미 정리되었다.
    text = "".join(ch for ch in text if ch == " " or unicodedata.category(ch)[0] != "C")
    text = _PUNCTUATION_RUN.sub(lambda m: m.group(1) * 3, text)
    return text.strip()


def normalize_segments(
    segments: list[TranscriptSegment], *, min_confidence: float = 0.0
) -> list[TranscriptSegment]:
    """세그먼트 목록을 정규화한다.

    수행하는 것:
      * 텍스트 표기 정리 (`normalize_text`)
      * 빈 세그먼트 제거 후 인덱스 재부여
      * 시각 역전 보정 (`end < start` 인 경우 `end = start`)
      * 동일 텍스트가 연속 반복되는 구간 병합
      * 신뢰도가 `min_confidence` 미만인 세그먼트 제거 (기본 꺼짐)

    반복 병합은 Whisper 계열의 반복 생성 실패 모드를 겨냥한 것으로, 완전히 동일한
    텍스트가 바로 이어질 때만 적용하고 시각 범위는 병합해 보존한다.

    신뢰도 필터는 무음 구간 환각(`감사합니다`, `자막제공자` 등)을 겨냥한다. **발화를
    지우는 동작이므로 기본값은 꺼짐이고**, 켜더라도 아래 두 가지 안전장치를 둔다.

      * 신뢰도가 없는 세그먼트(값을 주지 않는 엔진)는 지우지 않는다 — 모른다고 버리면
        해당 엔진의 전사가 통째로 사라진다.
      * **전부 걸러지면 아무것도 지우지 않는다.** 음질이 나쁜 파일에서 빈 Transcript 가
        나오면 "발화가 없었다" 로 보이는데, 그것은 사실과 다르고 문제를 감춘다 (§4.3).

    어느 쪽이든 원본이 필요하면 RAW Transcript 를 보면 된다 (Harness §50).
    """
    kept = drop_low_confidence(segments, min_confidence)

    cleaned: list[TranscriptSegment] = []
    for segment in kept:
        text = normalize_text(segment.text)
        if not text:
            continue
        start = max(segment.start, 0.0)
        end = max(segment.end, start)

        previous = cleaned[-1] if cleaned else None
        if previous is not None and previous.text == text:
            # 반복 구간을 하나로 합치고 끝 시각만 늘린다.
            cleaned[-1] = TranscriptSegment(
                index=previous.index,
                start=previous.start,
                end=max(previous.end, end),
                text=previous.text,
            )
            continue

        cleaned.append(
            TranscriptSegment(
                index=len(cleaned),
                start=start,
                end=end,
                text=text,
                # 신뢰도와 화자는 정규화가 만들어 내는 값이 아니다. 그대로 옮긴다.
                confidence=segment.confidence,
                speaker=segment.speaker,
            )
        )
    return cleaned


def drop_low_confidence(
    segments: list[TranscriptSegment], threshold: float
) -> list[TranscriptSegment]:
    """신뢰도가 낮은 세그먼트를 걸러 낸다.

    무음 구간 환각은 신뢰도가 눈에 띄게 낮게 나온다 (실측: 정상 발화 0.82 / 환각 0.20).
    그 차이를 이용해 걸러내되, 발화를 지우는 동작이므로 조심스럽게 다룬다.
    """
    if threshold <= 0:
        return segments

    kept = [
        segment
        for segment in segments
        # 값을 주지 않는 엔진의 세그먼트는 판단할 근거가 없으므로 남긴다.
        if segment.confidence is None or segment.confidence >= threshold
    ]

    if not kept:
        # 전부 걸러졌다. 빈 Transcript 는 "발화가 없었다" 로 보이므로 더 나쁘다.
        logger.warning(
            "every segment fell below the confidence threshold; keeping all of them",
            extra={
                "event": "STT_CONFIDENCE_FILTER_SKIPPED",
                "threshold": threshold,
                "segment_count": len(segments),
            },
        )
        return segments

    dropped = len(segments) - len(kept)
    if dropped:
        # 몇 개를 왜 지웠는지 남긴다. 본문은 남기지 않는다 (Harness §15).
        logger.info(
            "dropped low-confidence segments",
            extra={
                "event": "STT_LOW_CONFIDENCE_DROPPED",
                "threshold": threshold,
                "dropped_count": dropped,
                "kept_count": len(kept),
            },
        )
    return kept


def lineage_metadata(*, input_version: str) -> dict[str, object]:
    """Harness §51 이 요구하는 계보 정보.

    Args:
        input_version: 입력이 된 RAW Transcript 를 식별하는 값 (`stt_config_version` 등).

    `created_at` 은 저장 시점에 `TranscriptStore` 가 기록하므로 여기서 만들지 않는다.
    두 곳에서 서로 다른 시각이 생기는 것을 피한다.
    """
    return {
        "processor": NORMALIZER_NAME,
        "processor_version": NORMALIZER_VERSION,
        "input_version": input_version,
    }
