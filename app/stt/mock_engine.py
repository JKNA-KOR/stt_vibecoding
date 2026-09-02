"""테스트·개발용 Mock STT 엔진 (Harness §2.2 / §27, NFR-003).

모델 아티팩트 없이도 업로드 → Job → Transcript → 다운로드 전 경로를 검증할 수 있게 한다.
출력은 입력 음성의 SHA-256 에서 결정론적으로 생성되므로 같은 입력에는 항상 같은 결과가 나오고,
이 덕분에 통합 테스트가 텍스트 내용까지 단언할 수 있다.

실제 음성 내용을 전사하지 않으므로 운영 환경에서 선택되면 안 된다.
`app/stt/factory.py` 가 prod 환경에서의 선택을 기동 시점에 차단한다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import ClassVar

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import sha256_file
from app.stt.base import (
    EngineDescription,
    ProgressCallback,
    STTEngine,
    build_provenance,
    report_progress,
)
from app.stt.preprocessing import probe_audio
from app.stt.schemas import STTOptions, TranscriptionResult, TranscriptSegment

logger = get_logger(__name__)

# 출력 규칙이 바뀌면 올린다. Mock 결과를 기대값으로 쓰는 테스트가 함께 갱신되어야 한다.
MOCK_ENGINE_VERSION = "1.0.0"

# 한 세그먼트가 덮는 길이(초). 실제 Whisper 세그먼트 길이와 비슷한 값으로 잡는다.
_SEGMENT_SECONDS = 5.0
_MAX_SEGMENTS = 2000

# 결정론적 선택을 위한 고정 문장 풀. 개인정보처럼 보이는 문자열은 넣지 않는다 (Harness §46).
_PHRASES: tuple[str, ...] = (
    "안녕하세요 상담원 김입니다 무엇을 도와드릴까요",
    "네 말씀하신 내용 확인해 보겠습니다",
    "잠시만 기다려 주시면 조회해 드리겠습니다",
    "확인 결과 요청하신 건은 정상 처리되었습니다",
    "추가로 궁금하신 점이 있으실까요",
    "안내드린 내용은 문자로도 발송해 드리겠습니다",
    "불편을 드려서 죄송합니다 바로 조치하겠습니다",
    "다시 한번 확인해 주시고 말씀해 주세요",
    "처리까지는 영업일 기준 이틀 정도 소요됩니다",
    "이용해 주셔서 감사합니다 좋은 하루 보내세요",
)


class MockSTTEngine(STTEngine):
    """음성 해시로부터 고정된 문장을 배열해 돌려주는 가짜 엔진."""

    engine_name: ClassVar[str] = "mock"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        # 올릴 아티팩트가 없다. 상태 플래그만 맞춰 실제 엔진과 같은 수명주기를 갖게 한다.
        self._loaded = True
        logger.info("mock stt engine loaded", extra={"event": "STT_MODEL_LOADED", "engine": "mock"})

    def unload(self) -> None:
        self._loaded = False

    def describe(self) -> EngineDescription:
        return EngineDescription(
            engine=self.engine_name,
            model_name="mock",
            model_version=MOCK_ENGINE_VERSION,
            # 실제 아티팩트가 없다는 사실이 Provenance 에 드러나야 한다. 가짜 해시를 만들지 않는다.
            model_artifact_hash="mock",
            compute_type="none",
            device_type="none",
            is_loaded=self._loaded,
        )

    def transcribe(
        self,
        audio_path: Path,
        options: STTOptions,
        *,
        progress: ProgressCallback | None = None,
    ) -> TranscriptionResult:
        if not self._loaded:
            self.load()

        # 실제 엔진과 같은 실패 지점을 갖도록 디코딩 가능 여부는 똑같이 확인한다.
        probe = probe_audio(audio_path)
        digest = sha256_file(audio_path)

        segments = self._build_segments(digest, probe.duration_seconds, progress=progress)
        report_progress(progress, 1.0)

        logger.info(
            "mock transcription completed",
            extra={
                "event": "STT_COMPLETED",
                "engine": "mock",
                "segment_count": len(segments),
                "audio_duration_seconds": round(probe.duration_seconds, 3),
            },
        )

        return TranscriptionResult(
            segments=segments,
            detected_language=options.language or self._settings.stt_language,
            language_probability=1.0,
            audio_duration_seconds=probe.duration_seconds,
            provenance=build_provenance(
                self.describe(),
                options,
                detected_language=options.language or self._settings.stt_language,
                application_version=self._settings.app_version,
            ),
            extra={"mock": True, "mock_engine_version": MOCK_ENGINE_VERSION},
        )

    def _build_segments(
        self,
        digest: str,
        duration_seconds: float,
        *,
        progress: ProgressCallback | None,
    ) -> list[TranscriptSegment]:
        """해시를 시드로 문장을 배열한다.

        `random` 대신 해시를 직접 인덱스로 쓰는 이유는, 파이썬 버전이 바뀌어도 결과가
        달라지지 않게 하기 위해서다. Golden 테스트가 이 안정성에 의존한다.
        """
        if duration_seconds <= 0:
            return []

        count = min(int(duration_seconds // _SEGMENT_SECONDS) + 1, _MAX_SEGMENTS)
        # 세그먼트 수보다 긴 바이트열이 필요하므로 해시를 반복 확장한다.
        stream = bytearray()
        seed = digest.encode("ascii")
        while len(stream) < count:
            seed = hashlib.sha256(seed).digest()
            stream.extend(seed)

        segments: list[TranscriptSegment] = []
        for index in range(count):
            start = index * _SEGMENT_SECONDS
            end = min(start + _SEGMENT_SECONDS, duration_seconds)
            if end <= start:
                break
            segments.append(
                TranscriptSegment(
                    index=index,
                    start=round(start, 3),
                    end=round(end, 3),
                    text=_PHRASES[stream[index] % len(_PHRASES)],
                )
            )
            if count > 20 and index % 20 == 0:
                report_progress(progress, (index + 1) / count)
        return segments
