"""QA 평가 파이프라인 (Harness §13 / §20 / §36).

`app/llm/analyzer.py` 와 같은 자리에 있는 계층이다. Provider 가 돌려준 JSON 을 도메인
타입으로 바꾸면서 **모델이 매긴 점수를 그대로 믿지 않는다.**

점수를 다시 계산하는 이유가 이 파일의 핵심이다. LLM 은 항목 점수를 잘 매기면서도 합계를
틀리는 일이 흔하다. 총점이 화면과 집계에 그대로 쓰이므로, 합계는 코드가 계산하고 모델
값과 어긋나면 경고로 남긴다 — 조용히 어느 한쪽을 고르면 나중에 "왜 이 점수인가"에
답할 수 없게 된다 (Harness §4.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.config import Settings
from app.core.logging import get_logger
from app.llm.base import LLMProvider
from app.qa.prompts import (
    MAX_SCORE_ITEMS,
    MAX_VIOLATIONS,
    QA_PROMPT_VERSION,
    QA_SCHEMA_VERSION,
    QA_USER_TEMPLATE,
    build_system_prompt,
    qa_json_schema,
)
from app.qa.rubrics import rubric_fingerprint
from app.qa.schemas import (
    ComplianceViolation,
    QAContent,
    QAGrade,
    QAOutcome,
    ScoreItem,
    ViolationSeverity,
)
from app.stt.schemas import TranscriptSegment

logger = get_logger(__name__)

_MAX_ITEM_CHARS = 300
_MAX_SUMMARY_CHARS = 1000
_MAX_EVIDENCE_CHARS = 500
_MAX_CATEGORY_CHARS = 80

# 모델이 매긴 총점과 코드가 계산한 합계가 이보다 더 벌어지면 경고를 남긴다.
# 반올림 차이까지 경고로 만들면 경고가 무의미해진다.
_SCORE_TOLERANCE = 0.5

# 심각도별 감점. 프롬프트에 적힌 값과 같아야 한다 — 여기가 최종 판정이다.
_VIOLATION_PENALTY: dict[ViolationSeverity, float] = {
    ViolationSeverity.CRITICAL: 30.0,
    ViolationSeverity.MAJOR: 15.0,
    ViolationSeverity.MINOR: 5.0,
}

# 등급 경계. 프롬프트에도 같은 값이 적혀 있지만 판정은 코드가 한다.
_GRADE_THRESHOLDS: tuple[tuple[float, QAGrade], ...] = (
    (90.0, QAGrade.EXCELLENT),
    (80.0, QAGrade.GOOD),
    (70.0, QAGrade.FAIR),
)


@dataclass(frozen=True, slots=True)
class _Prepared:
    text: str
    truncated: bool


class QAEvaluator:
    """Transcript 하나를 상담 원칙과 컴플라이언스 기준으로 평가한다."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        settings: Settings,
        rubric: str,
        compliance: str,
        glossary: str = "",
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._rubric = rubric
        self._compliance = compliance
        self._glossary = glossary

    def evaluate(self, segments: list[TranscriptSegment]) -> QAOutcome:
        """세그먼트를 이어 붙여 평가한다.

        Raises:
            LLMError: Provider 호출이나 응답 파싱에 실패한 경우.
        """
        prepared = self._prepare(segments)

        response = self._provider.complete_json(
            system_prompt=build_system_prompt(
                self._rubric, self._compliance, glossary=self._glossary
            ),
            user_content=QA_USER_TEMPLATE.format(transcript=prepared.text),
            json_schema=qa_json_schema(),
            timeout_seconds=float(self._settings.llm_timeout_seconds),
        )

        content, warnings = _coerce(response.content)
        if prepared.truncated:
            warnings.append("transcript_truncated")

        return QAOutcome(
            content=content,
            provider=response.provider,
            model_name=response.model_name,
            prompt_version=QA_PROMPT_VERSION,
            schema_version=QA_SCHEMA_VERSION,
            rubric_version=rubric_fingerprint(self._rubric),
            compliance_version=rubric_fingerprint(self._compliance),
            duration_seconds=response.duration_seconds,
            transcript_truncated=prepared.truncated,
            warnings=tuple(warnings) + response.warnings,
        )

    def _prepare(self, segments: list[TranscriptSegment]) -> _Prepared:
        """분석기와 같은 규칙으로 자른다. 앞부분을 남긴다 — 상담의 목적이 앞에 있다."""
        limit = self._settings.llm_max_transcript_chars
        text = "\n".join(segment.text for segment in segments if segment.text.strip())

        if len(text) <= limit:
            return _Prepared(text=text, truncated=False)

        logger.warning(
            "transcript truncated for qa evaluation",
            extra={
                "event": "QA_INPUT_TRUNCATED",
                "original_chars": len(text),
                "limit_chars": limit,
            },
        )
        return _Prepared(text=text[:limit], truncated=True)


def _coerce(raw: dict) -> tuple[QAContent, list[str]]:
    """모델 응답을 도메인 타입으로 바꾸고, 점수는 코드가 다시 계산한다."""
    warnings: list[str] = []

    items = _score_items(raw.get("score_items"), warnings)
    violations = _violations(raw.get("violations"), warnings)

    overall = _recomputed_total(items, raw.get("overall_score"), warnings)
    compliance = _recomputed_compliance(violations, raw.get("compliance_score"), warnings)

    content = QAContent(
        overall_score=overall,
        grade=_grade_for(overall, items),
        score_items=items,
        compliance_score=compliance,
        violations=violations,
        strengths=_string_list(raw.get("strengths"), "strengths", warnings),
        improvements=_string_list(raw.get("improvements"), "improvements", warnings),
        summary=_text(raw.get("summary"), _MAX_SUMMARY_CHARS),
    )
    return content, warnings


def _score_items(value: object, warnings: list[str]) -> list[ScoreItem]:
    if not isinstance(value, list):
        if value is not None:
            warnings.append("score_items_not_a_list")
        return []

    items: list[ScoreItem] = []
    for raw in value[:MAX_SCORE_ITEMS]:
        if not isinstance(raw, dict):
            continue
        category = _text(raw.get("category"), _MAX_CATEGORY_CHARS)
        max_score = _number(raw.get("max_score"))
        if not category or max_score is None or max_score <= 0:
            continue
        score = _number(raw.get("score"))
        if score is None:
            continue
        if score > max_score:
            # 배점을 넘는 점수는 합계를 부풀린다. 잘라 내고 그 사실을 남긴다.
            warnings.append("score_above_max")
            score = max_score
        items.append(
            ScoreItem(
                category=category,
                score=max(0.0, round(score, 1)),
                max_score=round(max_score, 1),
                comment=_text(raw.get("comment"), _MAX_ITEM_CHARS),
            )
        )
    return items


def _violations(value: object, warnings: list[str]) -> list[ComplianceViolation]:
    if not isinstance(value, list):
        if value is not None:
            warnings.append("violations_not_a_list")
        return []

    found: list[ComplianceViolation] = []
    for raw in value[:MAX_VIOLATIONS]:
        if not isinstance(raw, dict):
            continue
        rule = _text(raw.get("rule"), _MAX_CATEGORY_CHARS)
        evidence = _text(raw.get("evidence"), _MAX_EVIDENCE_CHARS)
        if not rule:
            continue
        if not evidence:
            # 근거 발화가 없으면 위반으로 세지 않는다. 프롬프트가 요구한 조건이며,
            # 근거 없는 감점은 상담원에게 다툴 여지를 주지 않는다.
            warnings.append("violation_without_evidence_dropped")
            continue
        found.append(
            ComplianceViolation(
                rule=rule,
                severity=_enum(
                    raw.get("severity"),
                    ViolationSeverity,
                    ViolationSeverity.MINOR,
                    "severity",
                    warnings,
                ),
                evidence=evidence,
                comment=_text(raw.get("comment"), _MAX_ITEM_CHARS),
            )
        )
    return found


def _recomputed_total(
    items: list[ScoreItem], reported: object, warnings: list[str]
) -> float:
    """항목 점수의 합계를 총점으로 삼는다.

    항목이 하나도 오지 않았다면 합계는 0 인데, 그것을 "0점짜리 상담"으로 저장하면
    평가 실패가 최악의 점수로 둔갑한다. 그럴 때는 모델이 준 총점을 쓰고 경고를 남긴다.
    """
    if not items:
        warnings.append("no_score_items")
        fallback = _number(reported)
        return _bounded(fallback if fallback is not None else 0.0)

    total = round(sum(item.score for item in items), 1)
    stated = _number(reported)
    if stated is not None and abs(stated - total) > _SCORE_TOLERANCE:
        # 어느 쪽이 맞는지 여기서는 알 수 없다. 합계를 쓰되 어긋났다는 사실을 남긴다.
        warnings.append("overall_score_mismatch")
    return _bounded(total)


def _recomputed_compliance(
    violations: list[ComplianceViolation], reported: object, warnings: list[str]
) -> float:
    """위반 목록에서 감점을 계산한다.

    모델이 점수를 직접 매기게 두면, 위반을 나열하고도 100점을 주는 응답이 나온다.
    감점 규칙은 산술이므로 코드가 계산하는 편이 항상 옳다.
    """
    penalty = sum(_VIOLATION_PENALTY[v.severity] for v in violations)
    score = _bounded(100.0 - penalty)

    stated = _number(reported)
    if stated is not None and abs(stated - score) > _SCORE_TOLERANCE:
        warnings.append("compliance_score_mismatch")
    return score


def _grade_for(overall: float, items: list[ScoreItem]) -> QAGrade:
    """총점에서 등급을 정한다.

    채점 항목이 없으면 총점 자체를 신뢰할 수 없으므로 등급도 매기지 않는다.
    그럴듯한 등급을 붙이는 것보다 "판단불가"가 정확하다.
    """
    if not items:
        return QAGrade.UNKNOWN
    for threshold, grade in _GRADE_THRESHOLDS:
        if overall >= threshold:
            return grade
    return QAGrade.POOR


def _bounded(value: float) -> float:
    return round(min(100.0, max(0.0, value)), 1)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


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
        # 값 자체는 남기지 않는다. 녹취 내용이 섞여 들어왔을 수 있다 (Harness §15).
        warnings.append(f"{field}_out_of_enum")
        return default


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]
