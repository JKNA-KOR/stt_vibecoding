"""QA 평가기 테스트 (Harness §4.3 / §13 / §20).

이 계층의 핵심은 **모델이 매긴 점수를 그대로 믿지 않는 것**이다. 총점과 컴플라이언스
점수는 코드가 다시 계산하며, 모델 값과 어긋나면 경고로 남는다.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.llm.schemas import LLMResponse
from app.qa.evaluator import QAEvaluator
from app.qa.schemas import QAGrade, ViolationSeverity
from app.stt.schemas import TranscriptSegment


class _FixedProvider:
    """지정한 JSON 을 그대로 돌려주는 Provider. 평가기만 떼어 본다."""

    provider_name = "fixed"

    def __init__(self, content: dict) -> None:
        self._content = content
        self.system_prompt = ""
        self.user_content = ""

    def complete_json(self, *, system_prompt, user_content, json_schema, timeout_seconds):
        self.system_prompt = system_prompt
        self.user_content = user_content
        return LLMResponse(
            content=self._content,
            provider=self.provider_name,
            model_name="fixed-model",
            duration_seconds=0.1,
        )

    def describe(self):  # pragma: no cover - 평가 경로에서 쓰이지 않는다
        raise NotImplementedError


def _evaluate(
    settings: Settings,
    content: dict,
    *,
    rubric: str = "# 기준",
    compliance: str = "# 금지",
):
    provider = _FixedProvider(content)
    evaluator = QAEvaluator(
        provider, settings=settings, rubric=rubric, compliance=compliance
    )
    outcome = evaluator.evaluate(
        [TranscriptSegment(index=0, start=0.0, end=1.0, text="고객 문의입니다")]
    )
    return outcome, provider


def _items(*pairs: tuple[float, float]) -> list[dict]:
    return [
        {"category": f"항목{i}", "score": score, "max_score": max_score, "comment": ""}
        for i, (score, max_score) in enumerate(pairs)
    ]


# --- 점수 재계산 -----------------------------------------------------------------


def test_total_comes_from_the_items_not_the_model(settings: Settings) -> None:
    """모델이 합계를 틀리는 일이 흔하다. 총점은 항목 합계가 진실이다."""
    outcome, _ = _evaluate(
        settings,
        {"overall_score": 100, "score_items": _items((18, 20), (25, 30), (20, 20))},
    )

    assert outcome.content.overall_score == 63.0
    assert "overall_score_mismatch" in outcome.warnings


def test_matching_total_produces_no_warning(settings: Settings) -> None:
    outcome, _ = _evaluate(
        settings, {"overall_score": 63, "score_items": _items((18, 20), (25, 30), (20, 20))}
    )

    assert outcome.content.overall_score == 63.0
    assert "overall_score_mismatch" not in outcome.warnings


def test_score_above_its_max_is_clamped(settings: Settings) -> None:
    """배점을 넘는 점수는 합계를 부풀린다. 잘라 내고 그 사실을 남긴다."""
    outcome, _ = _evaluate(settings, {"score_items": _items((30, 20))})

    assert outcome.content.overall_score == 20.0
    assert "score_above_max" in outcome.warnings


def test_no_items_does_not_become_a_zero_point_consultation(settings: Settings) -> None:
    """평가 실패가 최악의 점수로 둔갑해서는 안 된다 (Harness §4.3)."""
    outcome, _ = _evaluate(settings, {"overall_score": 77, "score_items": []})

    assert outcome.content.overall_score == 77.0
    assert outcome.content.grade is QAGrade.UNKNOWN
    assert "no_score_items" in outcome.warnings


# --- 등급 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (95.0, QAGrade.EXCELLENT),
        (90.0, QAGrade.EXCELLENT),
        (85.0, QAGrade.GOOD),
        (70.0, QAGrade.FAIR),
        (69.9, QAGrade.POOR),
    ],
)
def test_grade_follows_the_total(settings: Settings, score: float, expected: QAGrade) -> None:
    """등급 판정은 프롬프트가 아니라 코드가 한다. 모델이 등급을 지어내도 무시된다."""
    outcome, _ = _evaluate(
        settings, {"grade": "우수", "score_items": _items((score, 100.0))}
    )

    assert outcome.content.grade is expected


# --- 컴플라이언스 ----------------------------------------------------------------


def test_compliance_score_is_computed_from_the_violations(settings: Settings) -> None:
    """위반을 나열하고도 100점을 주는 응답이 나온다. 감점은 산술이므로 코드가 계산한다."""
    outcome, _ = _evaluate(
        settings,
        {
            "compliance_score": 100,
            "score_items": _items((100, 100)),
            "violations": [
                {
                    "rule": "반말 사용",
                    "severity": "심각",
                    "evidence": "그건 안 돼요",
                    "comment": "",
                },
                {"rule": "추측 안내", "severity": "경미", "evidence": "아마도요", "comment": ""},
            ],
        },
    )

    # 100 - 30(심각) - 5(경미)
    assert outcome.content.compliance_score == 65.0
    assert "compliance_score_mismatch" in outcome.warnings
    assert outcome.content.has_critical_violation is True


def test_violation_without_evidence_is_dropped(settings: Settings) -> None:
    """근거 없는 감점은 상담원에게 다툴 여지를 주지 않는다."""
    outcome, _ = _evaluate(
        settings,
        {
            "score_items": _items((100, 100)),
            "violations": [
                {"rule": "반말 사용", "severity": "심각", "evidence": "", "comment": ""}
            ],
        },
    )

    assert outcome.content.violations == []
    assert outcome.content.compliance_score == 100.0
    assert "violation_without_evidence_dropped" in outcome.warnings


def test_unknown_severity_falls_back_without_losing_the_violation(settings: Settings) -> None:
    outcome, _ = _evaluate(
        settings,
        {
            "score_items": _items((100, 100)),
            "violations": [
                {"rule": "알 수 없음", "severity": "치명적", "evidence": "발화", "comment": ""}
            ],
        },
    )

    assert len(outcome.content.violations) == 1
    assert outcome.content.violations[0].severity is ViolationSeverity.MINOR
    assert "severity_out_of_enum" in outcome.warnings


def test_compliance_score_never_goes_below_zero(settings: Settings) -> None:
    outcome, _ = _evaluate(
        settings,
        {
            "score_items": _items((100, 100)),
            "violations": [
                {"rule": f"위반{i}", "severity": "심각", "evidence": "발화", "comment": ""}
                for i in range(5)
            ],
        },
    )

    assert outcome.content.compliance_score == 0.0


# --- 프롬프트 경계 (Harness §13, SEC-024) -----------------------------------------


def test_rubric_and_transcript_are_kept_apart(settings: Settings) -> None:
    """기준은 지시문 쪽, 녹취는 데이터 쪽이다. 둘을 이어 붙이지 않는다."""
    _, provider = _evaluate(
        settings, {"score_items": _items((10, 10))}, rubric="# 나의 기준", compliance="# 나의 금지"
    )

    assert "<rubric>" in provider.system_prompt
    assert "나의 기준" in provider.system_prompt
    assert "나의 금지" in provider.system_prompt
    assert "고객 문의입니다" not in provider.system_prompt
    assert "고객 문의입니다" in provider.user_content


def test_rubric_fingerprint_changes_with_the_rubric(settings: Settings) -> None:
    """기준이 바뀌면 점수도 달라진다. 어떤 기준이었는지 결과에 남아야 한다 (Harness §20)."""
    first, _ = _evaluate(settings, {"score_items": _items((10, 10))}, rubric="# A")
    second, _ = _evaluate(settings, {"score_items": _items((10, 10))}, rubric="# B")

    assert first.rubric_version != second.rubric_version
