"""Transcript 후처리 (Harness §4.3 / §13 / §20 / §50).

정규화된 Transcript 를 LLM 에 보내 문장을 다듬고 화자를 추정한다. 결과는 **원본을
덮어쓰지 않고** `LLM_CORRECTED` 라는 별도 종류로 저장된다 (§50).

이 계층의 핵심은 **모델이 문장을 잃어버리지 않게 하는 것**이다. LLM 은 목록을 다루면서
조용히 몇 줄을 빠뜨리거나 합치는 일이 흔한데, 그것이 그대로 저장되면 녹취에서 발화가
사라진다 — 전사 결과에서 이보다 나쁜 실패는 없다. 그래서 index 로 원본과 맞춰 붙이고,
돌아오지 않은 문장은 **원문을 그대로 남긴다.**

긴 녹취는 나눠 보낸다. 한 번에 다 보내면 출력 토큰 한도에 걸려 뒷부분이 잘린다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.logging import get_logger
from app.llm.base import LLMProvider
from app.llm.refine_prompts import (
    REFINE_PROMPT_VERSION,
    REFINE_SCHEMA_VERSION,
    REFINE_USER_TEMPLATE,
    SPEAKER_LABELS,
    SPEAKER_UNKNOWN,
    build_system_prompt,
    refine_json_schema,
)
from app.stt.schemas import TranscriptSegment

logger = get_logger(__name__)

# 한 번에 보낼 문장 수. 늘리면 호출 수가 줄지만 출력이 잘릴 위험이 커진다.
_BATCH_SIZE = 40
# 보정된 문장 하나의 길이 상한. 원문의 몇 배가 되면 모델이 말을 지어낸 것이다.
_MAX_GROWTH_RATIO = 3.0
_MAX_TEXT_CHARS = 2000


@dataclass(frozen=True, slots=True)
class RefineOutcome:
    segments: list[TranscriptSegment]
    provider: str
    model_name: str
    prompt_version: str
    schema_version: str
    # 실제로 문장이 바뀐 개수와 화자가 붙은 개수. 후처리가 무엇을 했는지 드러낸다.
    changed_count: int
    speaker_count: int
    duration_seconds: float
    warnings: tuple[str, ...] = field(default_factory=tuple)


class TranscriptRefiner:
    """정규화 Transcript 를 다듬고 화자를 붙인다."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        settings: Settings,
        glossary: str = "",
        correct: bool = True,
        diarize: bool = True,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._glossary = glossary
        self._correct = correct
        self._diarize = diarize

    def refine(self, segments: list[TranscriptSegment]) -> RefineOutcome:
        """세그먼트를 나눠 보내며 후처리한다.

        Raises:
            LLMError: Provider 호출이나 응답 파싱에 실패한 경우.
        """
        refined: list[TranscriptSegment] = []
        warnings: list[str] = []
        changed = 0
        speakers = 0
        duration = 0.0

        for start in range(0, len(segments), _BATCH_SIZE):
            batch = segments[start : start + _BATCH_SIZE]
            response = self._provider.complete_json(
                system_prompt=build_system_prompt(
                    glossary=self._glossary, diarize=self._diarize
                ),
                user_content=REFINE_USER_TEMPLATE.format(transcript=_render(batch)),
                json_schema=refine_json_schema(),
                timeout_seconds=float(self._settings.llm_timeout_seconds),
            )
            duration += response.duration_seconds
            warnings.extend(response.warnings)

            mapped = _index_results(response.content, warnings)
            for segment in batch:
                updated, was_changed, has_speaker = self._merge(segment, mapped.get(segment.index))
                refined.append(updated)
                changed += int(was_changed)
                speakers += int(has_speaker)

        logger.info(
            "transcript refined",
            extra={
                "event": "REFINE_COMPLETED",
                "segment_count": len(refined),
                "changed_count": changed,
                "speaker_count": speakers,
                # 본문은 남기지 않는다. 개수만 남긴다 (Harness §15).
                "warning_count": len(warnings),
            },
        )
        return RefineOutcome(
            segments=refined,
            provider=self._provider.provider_name,
            model_name=self._settings.llm_model_name,
            prompt_version=REFINE_PROMPT_VERSION,
            schema_version=REFINE_SCHEMA_VERSION,
            changed_count=changed,
            speaker_count=speakers,
            duration_seconds=round(duration, 3),
            warnings=tuple(warnings),
        )

    def _merge(
        self, original: TranscriptSegment, result: dict | None
    ) -> tuple[TranscriptSegment, bool, bool]:
        """원본과 모델 결과를 합친다.

        **결과가 없으면 원문을 그대로 남긴다.** 빠뜨린 문장을 버리면 녹취에서 발화가
        사라지는데, 그것이 후처리의 가장 나쁜 실패다 (Harness §4.3).

        시각은 절대 바꾸지 않는다. 모델은 시각을 모르고, 바꿀 이유도 없다.
        """
        if result is None:
            return original, False, False

        text = original.text
        if self._correct:
            candidate = result.get("text")
            if isinstance(candidate, str) and candidate.strip():
                text = _bounded_text(candidate.strip(), original.text)

        speaker = None
        if self._diarize:
            label = result.get("speaker")
            if isinstance(label, str) and label in SPEAKER_LABELS:
                speaker = None if label == SPEAKER_UNKNOWN else label

        return (
            TranscriptSegment(
                index=original.index,
                start=original.start,
                end=original.end,
                text=text,
                confidence=original.confidence,
                speaker=speaker,
            ),
            text != original.text,
            speaker is not None,
        )


def _render(segments: list[TranscriptSegment]) -> str:
    """모델에 보낼 형태. index 를 붙여 결과를 되짚을 수 있게 한다."""
    return "\n".join(f"[{segment.index}] {segment.text}" for segment in segments)


def _index_results(content: dict, warnings: list[str]) -> dict[int, dict]:
    """응답을 index 로 찾을 수 있게 만든다.

    순서가 아니라 index 로 맞추는 이유는, 모델이 순서를 바꾸거나 몇 개를 빠뜨려도
    나머지가 엉뚱한 문장에 붙지 않게 하기 위해서다.
    """
    raw = content.get("segments")
    if not isinstance(raw, list):
        warnings.append("refine_segments_not_a_list")
        return {}

    mapped: dict[int, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        mapped[index] = item
    return mapped


def _bounded_text(candidate: str, original: str) -> str:
    """보정 결과가 원문에서 너무 벗어나면 원문을 쓴다.

    모델이 문장을 요약하거나 설명을 덧붙이는 실패 모드가 있다. 길이로 완전히 걸러낼
    수는 없지만, 몇 배로 늘어난 문장은 확실히 지어낸 것이다.
    """
    if len(candidate) > _MAX_TEXT_CHARS:
        return original
    if original and len(candidate) > len(original) * _MAX_GROWTH_RATIO:
        return original
    return candidate
