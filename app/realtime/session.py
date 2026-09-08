"""실시간 전사 세션 (Harness §8.1 / §21 / §24).

브라우저가 보낸 PCM 을 모았다가 조각이 찰 때마다 WAV 로 만들어 STT 엔진에 넘긴다.

설계에서 지킨 것 세 가지.

  1. **음성은 디스크에 남지 않는다.** 조각 WAV 는 메모리에서 만들어 임시 파일로 잠깐
     내려갔다가 전사 직후 지워진다. 실시간 화면은 보관 대상이 아니므로, 남기지 않는 것이
     기본이다 (Harness §8.1 / §21). 참조 없는 Confidential 파일을 만들지 않는다 (§22).
  2. **세션에 상한이 있다.** 길이와 동시 세션 수 모두 설정으로 막는다. 잊고 켜 둔 탭이
     자원을 계속 먹는 경로를 열어 두지 않는다 (§24).
  3. **전사 본문은 로그에 남기지 않는다.** 조각 수와 길이만 기록한다 (§15).
"""

from __future__ import annotations

import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.stt.base import STTEngine
from app.stt.schemas import STTOptions, TranscriptSegment

logger = get_logger(__name__)

# 브라우저가 보낼 수 있는 샘플레이트 범위. 밖의 값은 손상되었거나 조작된 것으로 본다.
_MIN_SAMPLE_RATE = 8000
_MAX_SAMPLE_RATE = 48000

# 16bit PCM 한 표본의 바이트 수.
_BYTES_PER_SAMPLE = 2

# 한 번에 받을 수 있는 메시지 크기. 정상 클라이언트는 이보다 훨씬 작게 보낸다.
MAX_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class SegmentResult:
    """조각 하나의 전사 결과."""

    index: int
    start_seconds: float
    end_seconds: float
    text: str


class RealtimeSession:
    """PCM 을 모았다가 조각 단위로 전사한다.

    한 WebSocket 연결이 하나의 세션을 갖는다. 스레드 안전하지 않다 — 연결 하나를
    한 태스크가 다루는 것을 전제로 한다.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        engine: STTEngine,
        sample_rate: int,
        temp_dir: Path,
        vocabulary_hint: str = "",
    ) -> None:
        if not _MIN_SAMPLE_RATE <= sample_rate <= _MAX_SAMPLE_RATE:
            # 클라이언트가 보낸 값이다. 범위를 벗어나면 WAV 헤더가 망가진다 (Harness §6).
            raise ValidationError(
                "지원하지 않는 샘플레이트입니다.",
                internal_detail=f"sample_rate={sample_rate} out of range",
            )

        self._settings = settings
        self._engine = engine
        self._sample_rate = sample_rate
        self._temp_dir = temp_dir
        # 업로드 경로와 같은 사전을 쓴다. 두 경로가 다른 어휘를 보면 같은 통화가 화면에
        # 따라 다르게 적히고, 그 차이를 설명할 수 없게 된다.
        self._vocabulary_hint = vocabulary_hint
        self._buffer = bytearray()
        self._segment_index = 0
        self._consumed_samples = 0
        self._segment_bytes = (
            sample_rate * _BYTES_PER_SAMPLE * settings.realtime_segment_seconds
        )

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def elapsed_seconds(self) -> float:
        """지금까지 받은 음성의 길이. 세션 상한 판단에 쓴다."""
        pending = len(self._buffer) // _BYTES_PER_SAMPLE
        return (self._consumed_samples + pending) / self._sample_rate

    def add_chunk(self, chunk: bytes) -> None:
        """PCM 조각을 버퍼에 넣는다.

        Raises:
            ValidationError: 조각이 상한을 넘거나 16bit 정렬이 맞지 않는 경우.
        """
        if len(chunk) > MAX_CHUNK_BYTES:
            raise ValidationError(
                "전송 단위가 너무 큽니다.",
                internal_detail=f"chunk is {len(chunk)} bytes",
            )
        if len(chunk) % _BYTES_PER_SAMPLE != 0:
            # 16bit 정렬이 어긋나면 이후 모든 표본이 밀린다. 조용히 잘라 붙이지 않는다.
            raise ValidationError(
                "음성 데이터 형식이 올바르지 않습니다.",
                internal_detail=f"chunk length {len(chunk)} is not 16-bit aligned",
            )
        self._buffer.extend(chunk)

    @property
    def has_full_segment(self) -> bool:
        return len(self._buffer) >= self._segment_bytes

    def transcribe_ready_segment(self) -> SegmentResult | None:
        """조각이 찼으면 하나 전사한다. 아직이면 `None`."""
        if not self.has_full_segment:
            return None
        return self._transcribe(bytes(self._buffer[: self._segment_bytes]), trim=True)

    def flush(self) -> SegmentResult | None:
        """남은 버퍼를 마지막 조각으로 전사한다.

        너무 짧으면 전사하지 않는다. 0.5초짜리 조각은 대개 잡음만 남기며, 그 결과가
        화면 끝에 붙으면 사람이 오독한다.
        """
        min_bytes = self._sample_rate * _BYTES_PER_SAMPLE // 2
        if len(self._buffer) < min_bytes:
            self._buffer.clear()
            return None
        return self._transcribe(bytes(self._buffer), trim=True)

    def _transcribe(self, payload: bytes, *, trim: bool) -> SegmentResult:
        start = self._consumed_samples / self._sample_rate
        sample_count = len(payload) // _BYTES_PER_SAMPLE

        path = self._write_wav(payload)
        try:
            result = self._engine.transcribe(
                path,
                STTOptions(
                    language=self._settings.stt_language or None,
                    beam_size=self._settings.stt_beam_size,
                    vad_enabled=self._settings.stt_vad_enabled,
                    vocabulary_hint=self._vocabulary_hint,
                ),
            )
        finally:
            # 전사가 실패해도 음성 조각은 남기지 않는다 (Harness §21 / §22).
            path.unlink(missing_ok=True)

        if trim:
            del self._buffer[: len(payload)]
        self._consumed_samples += sample_count
        self._segment_index += 1

        text = " ".join(segment.text.strip() for segment in result.segments).strip()
        logger.info(
            "realtime segment transcribed",
            extra={
                "event": "REALTIME_SEGMENT",
                "segment_index": self._segment_index,
                "segment_seconds": round(sample_count / self._sample_rate, 2),
                # 본문은 남기지 않는다. 길이만 남긴다 (Harness §15).
                "char_count": len(text),
            },
        )
        return SegmentResult(
            index=self._segment_index,
            start_seconds=round(start, 2),
            end_seconds=round(start + sample_count / self._sample_rate, 2),
            text=text,
        )

    def _write_wav(self, payload: bytes) -> Path:
        """PCM 을 WAV 로 감싼다.

        엔진은 파일을 받는다. 브라우저가 코덱으로 인코딩한 것을 그대로 넘기면 조각 경계
        마다 컨테이너 헤더가 없어 디코딩이 깨지므로, 원시 PCM 을 받아 여기서 매번 온전한
        WAV 를 만든다 — 어느 지점에서 잘라도 항상 재생 가능한 파일이 나온다.
        """
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            suffix=".wav", dir=self._temp_dir, delete=False
        ) as handle:
            path = Path(handle.name)

        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(_BYTES_PER_SAMPLE)
            writer.setframerate(self._sample_rate)
            writer.writeframes(payload)
        return path


def silence(sample_rate: int, seconds: float) -> bytes:
    """무음 PCM. 테스트가 세션 동작을 확인하는 데 쓴다."""
    return struct.pack("<h", 0) * int(sample_rate * seconds)


def as_transcript_segments(results: list[SegmentResult]) -> list[TranscriptSegment]:
    """세션 결과를 도메인 타입으로 옮긴다. 다운로드·저장 경로에서 재사용한다."""
    return [
        TranscriptSegment(
            index=index,
            start=result.start_seconds,
            end=result.end_seconds,
            text=result.text,
        )
        for index, result in enumerate(results)
        if result.text
    ]
