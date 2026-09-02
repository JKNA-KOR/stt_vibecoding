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
    """시작·종료 시각을 가진 텍스트 조각."""

    index: int
    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class STTOptions:
    """엔진 호출 파라미터. 전부 설정에서 유래하며 코드에 하드코딩하지 않는다 (Harness §5.2)."""

    language: str | None
    beam_size: int
    vad_enabled: bool
    task: str = "transcribe"


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
