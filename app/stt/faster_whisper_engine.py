"""faster-whisper 엔진 구현체 (Harness §2.2 / §5.2 / §20 / §42).

이 모듈만이 `faster_whisper` 를 임포트한다. 다른 계층이 이 패키지를 직접 참조하기 시작하면
엔진 교체 가능성(NFR-002)이 깨지므로, 새 기능이 필요하면 `STTEngine` 인터페이스를 넓힌다.

동작 파라미터(모델·device·compute_type·language·beam_size·VAD)는 전부 설정에서 온다.
디코딩 세부 파라미터만 이 모듈의 상수로 두며, 변경 시 `ENGINE_DECODE_VERSION` 을 올리고
Golden Regression 을 재실행해야 한다 (Harness §36, OPS-003).
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from app.core.config import Settings
from app.core.exceptions import AudioDecodeError, STTModelError
from app.core.logging import get_logger
from app.core.security import sha256_file
from app.stt.base import (
    EngineDescription,
    ProgressCallback,
    STTEngine,
    build_provenance,
    report_progress,
)
from app.stt.schemas import STTOptions, TranscriptionResult, TranscriptSegment

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

logger = get_logger(__name__)

# 디코딩 규칙 버전. 아래 상수를 하나라도 바꾸면 올린다 (Harness §36).
ENGINE_DECODE_VERSION = "1.0.0"

# Whisper 계열은 무음·잡음 구간에서 직전 문장을 반복 생성하는 실패 모드가 있다.
# 녹취 기록물에서는 없는 발화가 만들어지는 쪽이 문장이 매끄럽지 않은 쪽보다 훨씬 위험하므로
# 이전 문맥 조건화를 끈다.
_CONDITION_ON_PREVIOUS_TEXT = False
# 단어 단위 타임스탬프는 CPU 에서 비용이 크고 현재 요구사항에 없다 (FR-T-004 는 세그먼트 단위).
_WORD_TIMESTAMPS = False

# 아티팩트 해시 대상. 가중치 파일만 해시해도 모델 동일성 판별에는 충분하다 (Harness §20).
_WEIGHT_FILENAMES: tuple[str, ...] = ("model.bin",)


class FasterWhisperEngine(STTEngine):
    """CTranslate2 기반 Whisper 구현체.

    프로세스당 하나만 두고 재사용한다. 모델 로드는 무겁고(medium int8 기준 약 1.5GB)
    Worker 동시 실행 수는 `STT_MAX_CONCURRENT_JOBS` 로 제한된다 (Harness §24).
    """

    engine_name: ClassVar[str] = "faster-whisper"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model: WhisperModel | None = None
        self._model_dir: Path | None = None
        self._artifact_hash = "unloaded"
        # 워커 스레드가 동시에 load() 를 호출해도 모델이 두 번 올라가지 않게 한다.
        self._lock = threading.Lock()

    # --- 수명주기 -----------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            self._model = self._create_model()

    def unload(self) -> None:
        with self._lock:
            self._model = None
            self._artifact_hash = "unloaded"
            logger.info("stt model unloaded", extra={"event": "STT_MODEL_UNLOADED"})

    def describe(self) -> EngineDescription:
        return EngineDescription(
            engine=self.engine_name,
            model_name=self._settings.stt_model_name,
            # faster-whisper 는 모델 자체의 버전 문자열을 제공하지 않는다.
            # 아티팩트 해시가 실질적인 버전 식별자이므로 라이브러리 버전을 함께 기록한다.
            model_version=f"faster-whisper/{_library_version()}",
            model_artifact_hash=self._artifact_hash,
            compute_type=self._settings.stt_compute_type,
            device_type=self._settings.stt_device,
            is_loaded=self.is_loaded,
        )

    def _create_model(self) -> WhisperModel:
        """모델 디렉터리를 확정한 뒤 로드하고 아티팩트 해시를 계산한다.

        모델 이름을 `WhisperModel` 에 그대로 넘기지 않고 경로를 먼저 확정하는 이유는,
        해시를 계산할 대상 파일을 알아야 하기 때문이다 (Harness §20).
        """
        WhisperModel = _import_whisper_model()

        model_dir = self._resolve_model_dir()
        try:
            model = WhisperModel(
                str(model_dir),
                device=self._settings.stt_device,
                compute_type=self._settings.stt_compute_type,
                cpu_threads=self._settings.stt_cpu_threads,
                local_files_only=True,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            logger.exception(
                "stt model load failed",
                extra={"event": "STT_MODEL_LOAD_FAILED", "reason": type(exc).__name__},
            )
            raise STTModelError(
                internal_detail=f"model load failed: {type(exc).__name__}: {exc}"
            ) from exc

        self._model_dir = model_dir
        self._artifact_hash = _compute_artifact_hash(model_dir)
        logger.info(
            "stt model loaded",
            extra={
                "event": "STT_MODEL_LOADED",
                "engine": self.engine_name,
                "model_name": self._settings.stt_model_name,
                "compute_type": self._settings.stt_compute_type,
                "device_type": self._settings.stt_device,
                "model_artifact_hash": self._artifact_hash,
            },
        )
        return model

    def _resolve_model_dir(self) -> Path:
        """설정의 모델 참조를 로컬 디렉터리로 해석한다.

        `STT_ALLOW_MODEL_DOWNLOAD` 가 꺼져 있으면 네트워크를 쓰지 않는다. 운영 환경은
        모델을 사전 반입하고 이 값을 꺼 둔 채 기동한다 (Harness §42).

        Raises:
            STTModelError: 아티팩트를 찾을 수 없거나 내려받을 수 없는 경우.
        """
        reference = self._settings.stt_model_reference
        candidate = Path(reference).expanduser()
        if candidate.is_dir():
            return candidate.resolve()

        allow_download = self._settings.stt_allow_model_download
        download_model = _import_download_model()

        try:
            resolved = download_model(reference, local_files_only=not allow_download)
        except (OSError, ValueError) as exc:
            logger.exception(
                "stt model artifact unavailable",
                extra={
                    "event": "STT_MODEL_ARTIFACT_MISSING",
                    "allow_download": allow_download,
                    "reason": type(exc).__name__,
                },
            )
            raise STTModelError(
                internal_detail=(
                    f"model '{reference}' unavailable "
                    f"(allow_download={allow_download}): {type(exc).__name__}"
                )
            ) from exc
        return Path(resolved).resolve()

    # --- 추론 ---------------------------------------------------------------

    def transcribe(
        self,
        audio_path: Path,
        options: STTOptions,
        *,
        progress: ProgressCallback | None = None,
    ) -> TranscriptionResult:
        self.load()
        model = self._model
        if model is None:  # pragma: no cover - load() 가 성공하면 도달하지 않는다
            raise STTModelError(internal_detail="model is not loaded after load()")

        try:
            segment_iter, info = model.transcribe(
                str(audio_path),
                language=options.language,
                task=options.task,
                beam_size=options.beam_size,
                vad_filter=options.vad_enabled,
                condition_on_previous_text=_CONDITION_ON_PREVIOUS_TEXT,
                word_timestamps=_WORD_TIMESTAMPS,
                # 도메인 용어 힌트. 비어 있으면 넘기지 않는다 — 빈 문자열도 프롬프트로
                # 취급하는 버전이 있어 결과가 달라질 수 있다 (Harness §36).
                initial_prompt=options.vocabulary_hint or None,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            raise self._to_domain_error(exc, stage="open") from exc

        # faster-whisper 의 반환값은 지연 생성기다. 실제 추론은 아래 순회에서 일어난다.
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        segments = self._collect_segments(segment_iter, duration=duration, progress=progress)
        report_progress(progress, 1.0)

        detected_language = getattr(info, "language", None)
        logger.info(
            "transcription completed",
            extra={
                "event": "STT_COMPLETED",
                "engine": self.engine_name,
                "segment_count": len(segments),
                "audio_duration_seconds": round(duration, 3),
                "detected_language": detected_language,
            },
        )

        return TranscriptionResult(
            segments=segments,
            detected_language=detected_language,
            language_probability=getattr(info, "language_probability", None),
            audio_duration_seconds=duration,
            provenance=build_provenance(
                self.describe(),
                options,
                detected_language=detected_language,
                application_version=self._settings.app_version,
            ),
            extra={
                "engine_decode_version": ENGINE_DECODE_VERSION,
                "condition_on_previous_text": _CONDITION_ON_PREVIOUS_TEXT,
                "word_timestamps": _WORD_TIMESTAMPS,
                "duration_after_vad": getattr(info, "duration_after_vad", None),
            },
        )

    def _collect_segments(
        self,
        segment_iter: Any,
        *,
        duration: float,
        progress: ProgressCallback | None,
    ) -> list[TranscriptSegment]:
        """생성기를 순회하며 도메인 타입으로 변환한다.

        엔진 고유 객체가 이 함수 밖으로 나가지 않게 하는 것이 핵심이다 (Harness §2.2).
        세그먼트 텍스트는 Untrusted Data 이므로 로그에 남기지 않는다 (Harness §13 / §15).
        """
        segments: list[TranscriptSegment] = []
        try:
            for index, raw in enumerate(segment_iter):
                start = float(raw.start)
                end = float(raw.end)
                segments.append(
                    TranscriptSegment(
                        index=index,
                        start=start,
                        end=end,
                        text=str(raw.text).strip(),
                        confidence=_confidence_of(raw),
                    )
                )
                if duration > 0:
                    report_progress(progress, end / duration)
        except (OSError, ValueError, RuntimeError) as exc:
            raise self._to_domain_error(exc, stage="decode") from exc
        return segments

    def _to_domain_error(self, exc: Exception, *, stage: str) -> Exception:
        """엔진 예외를 도메인 오류로 옮긴다 (Harness §23).

        디코딩 실패는 사용자가 다른 파일로 재시도할 수 있는 입력 문제이고, 그 밖의 실패는
        시스템 문제다. 둘을 같은 코드로 묶으면 재시도 정책(Harness §24)을 세울 수 없다.
        """
        reason = type(exc).__name__
        logger.exception(
            "transcription failed",
            extra={"event": "STT_FAILED", "stage": stage, "reason": reason},
        )
        if _is_decode_failure(exc):
            return AudioDecodeError(internal_detail=f"{stage}: {reason}: {exc}")
        return STTModelError(internal_detail=f"{stage}: {reason}: {exc}")


def _confidence_of(raw: Any) -> float | None:
    """세그먼트의 평균 로그확률을 0~1 확신도로 옮긴다.

    `avg_logprob` 는 토큰당 평균 로그확률이므로 exp 를 취하면 "토큰 하나를 맞힐 평균
    확률"에 해당한다. **정확도가 아니다** — 모델이 얼마나 확신했는지일 뿐이며, 확신에
    차서 틀리는 경우도 있다 (Harness §20).

    무음 구간에서 나오는 환각은 확신도가 높게 나오기도 하므로 `no_speech_prob` 만큼
    깎는다. 값이 없으면 지어내지 않고 `None` 을 돌려준다.
    """
    avg_logprob = getattr(raw, "avg_logprob", None)
    if not isinstance(avg_logprob, (int, float)):
        return None

    confidence = math.exp(float(avg_logprob))
    no_speech = getattr(raw, "no_speech_prob", None)
    if isinstance(no_speech, (int, float)):
        confidence *= 1.0 - min(max(float(no_speech), 0.0), 1.0)
    return round(min(max(confidence, 0.0), 1.0), 4)


def _is_decode_failure(exc: Exception) -> bool:
    """PyAV/ctranslate2 가 던진 예외가 음성 디코딩 실패인지 추정한다.

    faster-whisper 는 디코딩 오류를 전용 예외로 감싸지 않아 타입만으로는 구분되지 않는다.
    메시지 기반 판별은 견고하지 않으므로, 모호하면 시스템 오류로 분류해 재시도 경로를 남긴다.
    """
    message = str(exc).lower()
    return any(token in message for token in ("decode", "invalid data", "no audio", "codec"))


def _import_whisper_model() -> type[WhisperModel]:
    """`WhisperModel` 을 지연 임포트한다.

    임포트 실패는 배포 누락(모델 라이브러리 미설치)이며 운영 중에 드러나면 안 되는 종류다.
    그래도 도달했을 때 분류되지 않은 예외가 그대로 올라가면 Job 실패 코드가 잡히지 않으므로
    도메인 오류로 감싼다 (Harness §23).
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise STTModelError(internal_detail=f"faster-whisper import failed: {exc}") from exc
    return WhisperModel


def _import_download_model() -> Callable[..., str]:
    try:
        from faster_whisper.utils import download_model
    except ImportError as exc:
        raise STTModelError(internal_detail=f"faster-whisper import failed: {exc}") from exc
    return download_model


def _library_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("faster-whisper")
    except PackageNotFoundError:  # pragma: no cover - 설치 환경에서는 발생하지 않는다
        return "unknown"


def _compute_artifact_hash(model_dir: Path) -> str:
    """모델 가중치 파일의 SHA-256 을 계산한다 (Harness §20 / DAT-010).

    같은 이름의 모델이라도 아티팩트가 바뀌면 결과가 달라질 수 있으므로, Provenance 에는
    이름이 아니라 내용 해시를 남긴다. 가중치가 없으면 해시를 지어내지 않고 그 사실을 기록한다.
    """
    for filename in _WEIGHT_FILENAMES:
        weight = model_dir / filename
        if weight.is_file():
            return sha256_file(weight)
    logger.warning(
        "model weight file not found for hashing",
        extra={"event": "STT_MODEL_HASH_UNAVAILABLE"},
    )
    return "unavailable"
