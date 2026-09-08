"""STT Core 도메인 타입.

엔진 구현체(faster-whisper 등)에 종속되지 않는 순수 데이터 구조만 둔다.
새 엔진을 붙일 때 이 타입만 만족시키면 되도록 유지한다 (Harness §2.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class TranscriptKind(StrEnum):
    """Harness §50: 원본과 후처리 결과를 분리해 보관하기 위한 구분자."""

    RAW = "RAW"
    NORMALIZED = "NORMALIZED"
    # 향후 LLM 후처리 결과는 원본을 덮어쓰지 않고 이 종류로 추가된다 (Harness §50 / §61-16).
    LLM_CORRECTED = "LLM_CORRECTED"


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """시작·종료 시각을 가진 텍스트 조각.

    `confidence` 는 **모델이 스스로 매긴 확신도**이지 측정된 정확도가 아니다. Whisper 계열이
    내는 평균 로그확률을 0~1 로 옮긴 값이며, 낮으면 의심해 볼 구간이라는 신호일 뿐이다.
    이 값을 "정확도"라고 부르면 사람이 검증된 수치로 오해한다 — 화면에서도 "추정"으로
    표기한다 (Harness §20 / §4.3). 값을 주지 않는 엔진에서는 `None` 이다.
    """

    index: int
    start: float
    end: float
    text: str
    confidence: float | None = None
    # 발화자. **음향 기반 화자분리가 아니다** — 문맥으로 추정한 값이며, 후처리를
    # 거치지 않은 Transcript 에서는 `None` 이다 (Harness §4.3).
    speaker: str | None = None


@dataclass(frozen=True, slots=True)
class STTOptions:
    """엔진 호출 파라미터. 전부 설정에서 유래하며 코드에 하드코딩하지 않는다 (Harness §5.2)."""

    language: str | None
    beam_size: int
    vad_enabled: bool
    task: str = "transcribe"
    # 도메인 용어를 모델에 힌트로 준다. Whisper 계열의 `initial_prompt` 에 해당하며,
    # 사전에 등록된 용어를 인식 결과 쪽으로 끌어당긴다. 비어 있으면 보내지 않는다.
    #
    # **이것은 지시문이 아니라 어휘 힌트다.** Whisper 는 프롬프트를 명령으로 해석하지
    # 않고 다음 토큰 예측의 문맥으로만 쓴다. 길이 상한이 있으므로(약 224 토큰) 호출부가
    # 잘라서 넘긴다.
    vocabulary_hint: str = ""


@dataclass(frozen=True, slots=True)
class ModelProvenance:
    """Harness §5.3 / §20: STT 결과 재현에 필요한 모델·설정 정보."""

    engine: str
    model_name: str
    model_version: str
    model_artifact_hash: str
    compute_type: str
    device_type: str
    language: str | None
    beam_size: int
    vad_enabled: bool
    application_version: str

    def as_dict(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "model_artifact_hash": self.model_artifact_hash,
            "compute_type": self.compute_type,
            "device_type": self.device_type,
            "language": self.language,
            "beam_size": self.beam_size,
            "vad_enabled": self.vad_enabled,
            "application_version": self.application_version,
        }


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """엔진이 반환하는 전사 결과.

    `text` 와 `segments` 는 Untrusted Data 다 (Harness §13). 어떤 경로로도
    시스템 명령이나 프롬프트로 해석해서는 안 되며, 로그에도 남기지 않는다 (Harness §15).
    """

    segments: list[TranscriptSegment]
    detected_language: str | None
    language_probability: float | None
    audio_duration_seconds: float
    provenance: ModelProvenance
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(segment.text for segment in self.segments)

    @property
    def char_count(self) -> int:
        return sum(len(segment.text) for segment in self.segments)

    @property
    def mean_confidence(self) -> float | None:
        """길이로 가중한 평균 확신도.

        단순 평균을 쓰지 않는 이유는, 0.5초짜리 감탄사와 20초짜리 설명이 같은 무게를
        가지면 전체 인상이 왜곡되기 때문이다. 값을 주는 세그먼트가 하나도 없으면
        `None` 이다 — 0 으로 내려 쓰면 "확신도 0"으로 오해된다 (Harness §4.3).
        """
        weighted = 0.0
        total = 0.0
        for segment in self.segments:
            if segment.confidence is None:
                continue
            span = max(segment.end - segment.start, 0.0) or 1.0
            weighted += segment.confidence * span
            total += span
        return round(weighted / total, 4) if total > 0 else None
