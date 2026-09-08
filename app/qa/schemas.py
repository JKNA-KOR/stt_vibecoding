"""QA 도메인 타입.

LLM 이 만들어 낸 값은 그대로 믿지 않는다. 등급·심각도처럼 집계에 쓰이는 값은 열거형으로
고정하고, 점수는 범위를 강제한다 (Harness §13). 평가 항목 이름만은 자유 문자열인데,
운영자가 자체 기준 매뉴얼을 넣을 수 있어야 하기 때문이다 — 항목을 열거형으로 고정하면
자체 기준을 넣는 순간 그 항목들이 전부 버려진다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class QAGrade(StrEnum):
    """총점 구간에 대응하는 등급. 모델이 임의 등급을 지어내지 못하게 고정한다."""

    EXCELLENT = "우수"
    GOOD = "양호"
    FAIR = "보통"
    POOR = "미흡"
    UNKNOWN = "판단불가"


class ViolationSeverity(StrEnum):
    """컴플라이언스 위반의 무게. 감점 폭과 화면 강조에 쓰인다."""

    CRITICAL = "심각"
    MAJOR = "주의"
    MINOR = "경미"


@dataclass(frozen=True, slots=True)
class ScoreItem:
    """상담 원칙 한 항목에 대한 채점 결과."""

    category: str
    score: float
    max_score: float
    comment: str

    @property
    def ratio(self) -> float:
        return self.score / self.max_score if self.max_score > 0 else 0.0


@dataclass(frozen=True, slots=True)
class ComplianceViolation:
    """컴플라이언스 위반 한 건.

    `evidence` 는 녹취에서 인용한 발화다. **Untrusted Data 이며 Confidential 이다**
    (Harness §8.1 / §13) — 로그에 남기지 않고, 화면에는 textContent 로만 넣는다.
    """

    rule: str
    severity: ViolationSeverity
    evidence: str
    comment: str


@dataclass(frozen=True, slots=True)
class QAContent:
    """평가 본문. 저장·표시되는 값 전체다."""

    overall_score: float
    grade: QAGrade
    score_items: list[ScoreItem]
    compliance_score: float
    violations: list[ComplianceViolation]
    strengths: list[str]
    improvements: list[str]
    summary: str

    @property
    def has_critical_violation(self) -> bool:
        return any(v.severity is ViolationSeverity.CRITICAL for v in self.violations)


@dataclass(frozen=True, slots=True)
class QAOutcome:
    """평가 결과 + 그 결과가 어떤 조건에서 나왔는지 (Harness §20).

    루브릭이 바뀌면 점수가 달라진다. 어떤 기준으로 매긴 점수인지 함께 남기지 않으면
    "지난달 82점과 이번달 74점"을 비교할 수 없다.
    """

    content: QAContent
    provider: str
    model_name: str
    prompt_version: str
    schema_version: str
    # 평가에 쓰인 루브릭 본문의 해시. 기준이 바뀌었는지 사후에 판별한다.
    rubric_version: str
    compliance_version: str
    duration_seconds: float
    transcript_truncated: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)
