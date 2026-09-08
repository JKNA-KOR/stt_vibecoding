"""Transcript 분석 파이프라인 (Harness §13 / §20 / §36).

Provider 가 돌려준 JSON 을 도메인 타입으로 바꾸는 것이 이 계층의 일이다. 모델 응답을
그대로 믿지 않는다 — 스키마를 강제해도 값이 비거나 열거형을 벗어날 수 있으므로,
저장 전에 여기서 한 번 더 거른다.

**분석은 원본을 바꾸지 않는다.** Transcript 는 그대로 두고 별도 레코드로 남는다
(Harness §50). 분석이 실패해도 전사 결과는 온전하다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.config import Settings
from app.core.logging import get_logger
from app.llm.base import LLMProvider
from app.llm.prompts import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    USER_CONTENT_TEMPLATE,
    analysis_json_schema,
)
from app.llm.schemas import (
    AnalysisContent,
    AnalysisOutcome,
    ContactClassification,
    CustomerReaction,
    Sentiment,
)
from app.stt.schemas import TranscriptSegment

logger = get_logger(__name__)

# 목록형 필드의 개별 항목 길이 상한. 모델이 문단을 통째로 밀어 넣는 것을 막는다.
_MAX_ITEM_CHARS = 300
_MAX_OPINION_CHARS = 1000


@dataclass(frozen=True, slots=True)
class _Prepared:
    text: str
    truncated: bool


class TranscriptAnalyzer:
    """Transcript 하나를 분석한다."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        settings: Settings,
        prompt_template: str,
        glossary: str = "",
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._prompt = prompt_template
        # 용어 설명은 지시문 쪽에 붙는다. 녹취(DATA)와 섞지 않는다 (Harness §13).
        self._glossary = glossary

    def analyze(self, segments: list[TranscriptSegment]) -> AnalysisOutcome:
        """세그먼트를 이어 붙여 분석한다.

        Raises:
            LLMError: Provider 호출이나 응답 파싱에 실패한 경우.
        """
        prepared = self._prepare(segments)

        response = self._provider.complete_json(
            system_prompt=_with_glossary(self._prompt, self._glossary),
            user_content=USER_CONTENT_TEMPLATE.format(transcript=prepared.text),
            json_schema=analysis_json_schema(),
            timeout_seconds=float(self._settings.llm_timeout_seconds),
        )

        content, warnings = _coerce(response.content)
        if prepared.truncated:
            warnings.append("transcript_truncated")

        return AnalysisOutcome(
            content=content,
            provider=response.provider,
            model_name=response.model_name,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            duration_seconds=response.duration_seconds,
            transcript_truncated=prepared.truncated,
            warnings=tuple(warnings) + response.warnings,
        )

    def _prepare(self, segments: list[TranscriptSegment]) -> _Prepared:
        """세그먼트를 하나의 텍스트로 만든다.

        길이 상한을 넘으면 **앞쪽을 남기고** 자른다. 상담은 앞부분에 목적이 드러나는
        경우가 많고, 뒤를 남기면 맥락 없이 결론만 보게 된다. 잘랐다는 사실은 결과에
        함께 기록되어 화면에도 표시된다.
        """
        limit = self._settings.llm_max_transcript_chars
        text = "\n".join(segment.text for segment in segments if segment.text.strip())

        if len(text) <= limit:
            return _Prepared(text=text, truncated=False)

        logger.warning(
            "transcript truncated for analysis",
            extra={
                "event": "LLM_INPUT_TRUNCATED",
                "original_chars": len(text),
                "limit_chars": limit,
            },
        )
        return _Prepared(text=text[:limit], truncated=True)


def _with_glossary(prompt: str, glossary: str) -> str:
    """지시문에 용어 설명을 덧붙인다.

    태그로 감싸는 이유는 프롬프트의 다른 부분과 경계를 분명히 하기 위해서다. 용어는
    관리자가 넣은 값이라 녹취보다는 신뢰할 수 있지만, 그래도 지시문과 한 덩어리로
    섞지 않는다 (Harness §13).
    """
    if not glossary.strip():
        return prompt
    return (
        f"{prompt}\n\n"
        "다음은 이 업무에서 쓰는 용어다. 녹취에 나오면 이 뜻으로 해석한다.\n"
        f"<glossary>\n{glossary.strip()}\n</glossary>"
    )


def _coerce(raw: dict) -> tuple[AnalysisContent, list[str]]:
    """모델 응답을 도메인 타입으로 바꾼다.

    알 수 없는 열거형 값은 예외 대신 `판단불가` 로 떨어뜨리고 경고를 남긴다. 분석은
    부가 기능이므로, 한 필드가 어긋났다고 결과 전체를 버리는 것은 과하다. 다만 조용히
    넘기지도 않는다 — 무엇이 어긋났는지 경고로 남는다 (Harness §4.3).
    """
    warnings: list[str] = []

    content = AnalysisContent(
        summary=_string_list(raw.get("summary"), "summary", warnings),
        keywords=_string_list(raw.get("keywords"), "keywords", warnings),
        action_items=_string_list(raw.get("action_items"), "action_items", warnings),
        customer_reaction=_enum(
            raw.get("customer_reaction"),
            CustomerReaction,
            CustomerReaction.UNKNOWN,
            "customer_reaction",
            warnings,
        ),
        contact_classification=_enum(
            raw.get("contact_classification"),
            ContactClassification,
            ContactClassification.UNKNOWN,
            "contact_classification",
            warnings,
        ),
        sentiment=_enum(
            raw.get("sentiment"), Sentiment, Sentiment.UNKNOWN, "sentiment", warnings
        ),
        opinion=_text(raw.get("opinion"), _MAX_OPINION_CHARS),
    )
    return content, warnings


def _string_list(value: object, field: str, warnings: list[str]) -> list[str]:
    if not isinstance(value, list):
        if value is not None:
            warnings.append(f"{field}_not_a_list")
        return []
    items = [_text(item, _MAX_ITEM_CHARS) for item in value if isinstance(item, str)]
    return [item for item in items if item]


def _enum[E: StrEnum](
    value: object, enum_type: type[E], default: E, field: str, warnings: list[str]
) -> E:
    if not isinstance(value, str):
        return default
    try:
        return enum_type(value)
    except ValueError:
        # 모델이 스키마 밖의 분류명을 만들어 낸 경우다. 값 자체는 남기지 않는다 —
        # 녹취 내용이 섞여 들어왔을 수 있다 (Harness §15).
        warnings.append(f"{field}_out_of_enum")
        return default


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]
