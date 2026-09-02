"""STT 엔진 인터페이스 (Harness §2.2, NFR-002).

faster-whisper 는 구현체일 뿐 애플리케이션의 종속점이 아니다. 상위 계층(jobs, api)은
이 모듈의 인터페이스와 `app.stt.schemas` 의 도메인 타입만 알고 있어야 하며, 엔진 고유
타입(`faster_whisper.Segment`, ctranslate2 객체 등)을 밖으로 흘려보내지 않는다.

새 엔진을 추가하려면 `STTEngine` 을 구현하고 `app/stt/factory.py` 의 레지스트리에 등록한다.
그 외의 코드는 수정할 필요가 없어야 하며, 그렇지 않다면 추상화가 새고 있는 것이다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from app.stt.schemas import ModelProvenance, STTOptions, TranscriptionResult

# 진행률 콜백. 0.0~1.0 사이의 값을 받으며, 콜백에서 발생한 예외는 전사를 중단시키지 않는다.
ProgressCallback = Callable[[float], None]


@dataclass(frozen=True, slots=True)
class EngineDescription:
    """엔진의 현재 상태. `/ready` 와 관리자 화면이 사용한다 (FR-H-002, FR-M-002).

    모델 아티팩트 경로처럼 내부 구조를 드러내는 값은 담지 않는다 (Harness §44).
    """

    engine: str
    model_name: str
    model_version: str
    model_artifact_hash: str
    compute_type: str
    device_type: str
    is_loaded: bool


class STTEngine(ABC):
    """음성 파일 하나를 텍스트로 변환하는 엔진.

    구현체 공통 계약:
      * `load()` 는 멱등이어야 하고, 여러 번 호출해도 모델을 다시 올리지 않는다.
      * `transcribe()` 는 `load()` 가 선행되지 않았다면 스스로 로드한다.
      * 엔진 내부 오류는 `STTModelError` 로 변환해 던진다. 원인 상세는 로그 전용이다 (Harness §23).
      * Transcript 본문은 어떤 경우에도 로그에 남기지 않는다 (Harness §15).
    """

    engine_name: ClassVar[str]

    @property
    @abstractmethod
    def is_loaded(self) -> bool:
        """모델이 메모리에 올라와 요청을 받을 수 있는 상태인지."""

    @abstractmethod
    def load(self) -> None:
        """모델 아티팩트를 메모리에 올린다. 멱등하다.

        Raises:
            STTModelError: 아티팩트가 없거나 로드에 실패한 경우.
        """

    @abstractmethod
    def unload(self) -> None:
        """모델을 내린다. 로드되지 않은 상태에서 호출해도 오류가 아니다."""

    @abstractmethod
    def describe(self) -> EngineDescription:
        """로드 여부와 무관하게 호출 가능해야 한다. Health/관리자 응답에 쓰인다."""

    @abstractmethod
    def transcribe(
        self,
        audio_path: Path,
        options: STTOptions,
        *,
        progress: ProgressCallback | None = None,
    ) -> TranscriptionResult:
        """음성 파일을 전사한다.

        Args:
            audio_path: 저장소 검증을 통과한 음성 파일의 절대경로.
            options: 설정에서 유래한 호출 파라미터 (Harness §5.2).
            progress: 0.0~1.0 진행률 콜백. 긴 음성의 처리 상태 보고에 쓴다.

        Returns:
            세그먼트와 Provenance 를 포함한 전사 결과.

        Raises:
            STTModelError: 모델 로드 또는 추론 실패.
            AudioDecodeError: 엔진이 음성을 디코딩하지 못한 경우.
        """


def build_provenance(
    description: EngineDescription,
    options: STTOptions,
    *,
    detected_language: str | None,
    application_version: str,
) -> ModelProvenance:
    """엔진 상태와 호출 옵션을 Provenance 로 합친다 (Harness §5.3 / §20).

    구현체마다 같은 조립 코드를 반복하지 않도록 여기에 모아 둔다. 언어는 자동감지 결과가
    있으면 그것을 기록한다. 요청 시점의 설정값(`options.language`)이 `None` 이었다는 사실은
    `stt_config_version` 이 따로 담고 있다.
    """
    return ModelProvenance(
        engine=description.engine,
        model_name=description.model_name,
        model_version=description.model_version,
        model_artifact_hash=description.model_artifact_hash,
        compute_type=description.compute_type,
        device_type=description.device_type,
        language=detected_language or options.language,
        beam_size=options.beam_size,
        vad_enabled=options.vad_enabled,
        application_version=application_version,
    )


def report_progress(progress: ProgressCallback | None, ratio: float) -> None:
    """진행률을 보고한다. 콜백 실패가 전사를 깨뜨리지 않게 감싼다.

    진행률은 부가 정보이므로 여기서 예외를 삼키는 것은 오류 은폐(Harness §4.3)가 아니다.
    다만 조용히 넘어가지 않도록 호출부 로거가 아닌 콜백 자신이 기록하도록 계약을 둔다.
    """
    if progress is None:
        return
    bounded = 0.0 if ratio < 0.0 else (1.0 if ratio > 1.0 else ratio)
    try:
        progress(bounded)
    except Exception:  # noqa: BLE001 - 진행률 보고 실패가 전사를 중단시켜서는 안 된다
        return
