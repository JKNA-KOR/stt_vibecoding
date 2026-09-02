"""음성 전처리 및 메타데이터 추출 (Harness §6 / §7 / §22 / §36).

전처리 로직 변경은 Release 영향 변경이다 (Harness §36). 변경 시 `PREPROCESSOR_VERSION`
을 올리고 Golden Regression 을 재실행해야 한다. 이 값은 Job 메타데이터에 기록되어
결과 재현에 사용된다 (Harness §20).

기본 디코딩 경로는 PyAV 이며 외부 프로세스를 띄우지 않는다. ffmpeg 경로는
`ENABLE_FFMPEG_PREPROCESS` 로 분리되어 있고, 사용 시에도 Harness §7 규칙
(인자 리스트, shell=False, timeout, 반환코드 검증, 바이너리 allowlist)을 따른다.
"""

from __future__ import annotations

import subprocess  # noqa: S404 - Harness §7 규칙을 지켜 사용한다 (shell=False, 인자 리스트, timeout)
from dataclasses import dataclass
from pathlib import Path

from app.core.exceptions import AudioDecodeError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

# 전처리 규칙이 바뀌면 반드시 증가시킨다 (Harness §20 / §36).
PREPROCESSOR_VERSION = "1.0.0"

# Harness §6: 확장자만 믿지 않고 파일 시그니처를 함께 확인한다.
# (offset, magic bytes) 목록 중 하나라도 일치하면 해당 컨테이너로 인정한다.
_FILE_SIGNATURES: dict[str, tuple[tuple[int, bytes], ...]] = {
    "wav": ((0, b"RIFF"),),
    "flac": ((0, b"fLaC"),),
    "ogg": ((0, b"OggS"),),
    "webm": ((0, b"\x1a\x45\xdf\xa3"),),
    "m4a": ((4, b"ftyp"),),
    "mp4": ((4, b"ftyp"),),
    # MP3 는 ID3 태그로 시작하거나 프레임 동기 워드(0xFF 0xEx/0xFx)로 시작한다.
    "mp3": ((0, b"ID3"), (0, b"\xff\xfb"), (0, b"\xff\xf3"), (0, b"\xff\xf2"), (0, b"\xff\xfa")),
}

_SIGNATURE_READ_BYTES = 16


@dataclass(frozen=True, slots=True)
class AudioProbe:
    """디코딩 없이 얻은 음성 메타데이터."""

    duration_seconds: float
    container: str
    codec: str
    sample_rate: int | None
    channels: int | None


def verify_file_signature(path: Path, extension: str) -> None:
    """확장자와 실제 파일 시그니처가 일치하는지 확인한다 (Harness §6, FR-U-003).

    `audio.mp3.exe` 처럼 확장자를 위장한 입력과, 확장자만 바꾼 임의 바이너리를 걸러낸다.

    Raises:
        ValidationError: 허용되지 않는 확장자이거나 시그니처가 일치하지 않는 경우.
    """
    signatures = _FILE_SIGNATURES.get(extension)
    if signatures is None:
        raise ValidationError(
            "지원하지 않는 음성 파일 형식입니다.",
            internal_detail=f"no signature rule for extension '{extension}'",
        )

    with path.open("rb") as handle:
        header = handle.read(_SIGNATURE_READ_BYTES)

    for offset, magic in signatures:
        if header[offset : offset + len(magic)] == magic:
            return

    raise ValidationError(
        "파일 내용이 확장자와 일치하지 않습니다.",
        internal_detail=f"signature mismatch for extension '{extension}'",
    )


def probe_audio(path: Path) -> AudioProbe:
    """PyAV 으로 컨테이너 메타데이터를 읽는다.

    전체 디코딩 없이 헤더만 보므로 업로드 검증 단계에서 저렴하게 호출할 수 있다.

    Raises:
        AudioDecodeError: 컨테이너를 열 수 없거나 오디오 스트림이 없는 경우.
    """
    # 지연 임포트: av 는 무거운 확장 모듈이라 API 프로세스 기동 시간을 늘린다.
    import av
    from av.error import FFmpegError

    try:
        with av.open(str(path)) as container:
            audio_streams = [s for s in container.streams if s.type == "audio"]
            if not audio_streams:
                raise AudioDecodeError(
                    internal_detail="container has no audio stream",
                )
            stream = audio_streams[0]
            if container.duration is not None:
                duration = float(container.duration) / 1_000_000.0
            elif stream.duration is not None and stream.time_base is not None:
                duration = float(stream.duration * stream.time_base)
            else:
                raise AudioDecodeError(internal_detail="duration is not available")

            return AudioProbe(
                duration_seconds=duration,
                container=container.format.name if container.format else "unknown",
                codec=stream.codec_context.name if stream.codec_context else "unknown",
                sample_rate=getattr(stream.codec_context, "sample_rate", None),
                channels=getattr(stream.codec_context, "channels", None),
            )
    except FFmpegError as exc:
        # 원인 상세는 로그에만 남기고 사용자에게는 분류 코드만 전달한다 (Harness §23 / §44).
        logger.warning(
            "audio probe failed",
            extra={"event": "AUDIO_PROBE_FAILED", "reason": type(exc).__name__},
        )
        raise AudioDecodeError(internal_detail=f"pyav open failed: {type(exc).__name__}") from exc


def convert_with_ffmpeg(
    source: Path,
    target: Path,
    *,
    ffmpeg_binary: str,
    timeout_seconds: int,
    sample_rate: int = 16000,
) -> None:
    """ffmpeg 로 16kHz 모노 WAV 를 만든다 (Harness §7).

    사용자 입력은 인자 리스트의 원소로만 전달되며 셸을 거치지 않는다.
    이 함수는 `ENABLE_FFMPEG_PREPROCESS` 가 켜졌을 때만 호출된다.

    Raises:
        AudioDecodeError: 변환 실패, 타임아웃, 또는 허용되지 않은 바이너리 경로.
    """
    binary = Path(ffmpeg_binary)
    # Harness §7: 허용된 Binary 만 실행한다. 경로가 설정으로 고정되어 있고 실제로
    # 존재하는 실행 파일인지 확인한 뒤에만 호출한다.
    if not binary.is_absolute() or not binary.is_file():
        raise AudioDecodeError(
            internal_detail=f"ffmpeg binary not allowed or missing: {ffmpeg_binary}"
        )

    command = [
        str(binary),
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(source),
        "-vn",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-f", "wav",
        "-y",
        str(target),
    ]

    try:
        completed = subprocess.run(  # noqa: S603 - 인자 리스트 + shell=False (Harness §7)
            command,
            shell=False,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioDecodeError(
            internal_detail=f"ffmpeg timeout after {timeout_seconds}s"
        ) from exc

    if completed.returncode != 0:
        # stderr 전문은 파일 경로를 포함할 수 있으므로 앞부분만 로그에 남긴다 (Harness §44).
        stderr_head = completed.stderr.decode("utf-8", errors="replace")[:200]
        # 여기는 except 블록이 아니라 반환코드 검사 지점이므로 exc_info 를 붙이지 않는다.
        logger.error(
            "ffmpeg conversion failed",
            extra={
                "event": "FFMPEG_FAILED",
                "return_code": completed.returncode,
                "stderr_head": stderr_head,
            },
        )
        raise AudioDecodeError(internal_detail=f"ffmpeg exited with {completed.returncode}")
