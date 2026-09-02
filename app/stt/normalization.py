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

from app.stt.schemas import TranscriptSegment

NORMALIZER_NAME = "rule-based-normalizer"

# 규칙이 바뀌면 반드시 증가시킨다 (Harness §20 / §36 / §51).
NORMALIZER_VERSION = "1.0.0"

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


def normalize_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    """세그먼트 목록을 정규화한다.

    수행하는 것:
      * 텍스트 표기 정리 (`normalize_text`)
      * 빈 세그먼트 제거 후 인덱스 재부여
      * 시각 역전 보정 (`end < start` 인 경우 `end = start`)
      * 동일 텍스트가 연속 반복되는 구간 병합

    마지막 항목은 Whisper 계열의 반복 생성 실패 모드를 겨냥한 것으로, 완전히 동일한
    텍스트가 바로 이어질 때만 적용하고 시각 범위는 병합해 보존한다. 원본이 필요하면
    RAW Transcript 를 보면 된다 (Harness §50).
    """
    cleaned: list[TranscriptSegment] = []
    for segment in segments:
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
            TranscriptSegment(index=len(cleaned), start=start, end=end, text=text)
        )
    return cleaned


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
