"""음성 파일 저장소 (Harness §6 / §21 / §22 / §45).

업로드 파일은 신뢰할 수 없는 외부 입력이므로, 저장 전에 다음을 모두 통과해야 한다.

    확장자 allowlist → 크기 제한 → 파일 시그니처 → 재생시간 제한

저장 경로는 서버가 만든 UUID 로만 구성하며, 사용자 제공 파일명은 경로에 관여하지 않는다.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings
from app.core.exceptions import (
    AudioTooLongError,
    FileTooLargeError,
    StorageError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.security import extract_extension, resolve_within, sanitize_display_filename, sha256_file
from app.stt.preprocessing import AudioProbe, probe_audio, verify_file_signature

logger = get_logger(__name__)

_UPLOAD_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class StoredAudio:
    """검증을 마치고 영구 저장소에 자리 잡은 음성 파일."""

    relpath: str
    display_filename: str
    extension: str
    size_bytes: int
    sha256: str
    probe: AudioProbe


class AudioStore:
    """음성 파일의 저장·조회·삭제를 담당한다."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._root = settings.audio_dir
        self._temp_dir = settings.temp_dir

    def ensure_directories(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._temp_dir.mkdir(parents=True, exist_ok=True)

    # --- 업로드 ------------------------------------------------------------

    def create_temp_path(self) -> Path:
        """임시파일 경로를 만든다. 지정된 Temp Directory 밖으로 나가지 않는다 (Harness §22)."""
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        return resolve_within(self._temp_dir, f"upload-{uuid.uuid4().hex}.part")

    def stream_to_temp(self, chunks: Iterator[bytes], temp_path: Path) -> int:
        """업로드 스트림을 임시파일에 쓰면서 누적 크기를 검사한다.

        Content-Length 헤더는 위조 가능하므로 실제로 읽은 바이트로 판단한다 (Harness §6 / §24).

        Returns:
            기록된 총 바이트 수.

        Raises:
            FileTooLargeError: 허용 크기를 초과한 시점에 즉시 중단한다.
        """
        limit = self._settings.max_upload_bytes
        written = 0
        with temp_path.open("wb") as handle:
            for chunk in chunks:
                written += len(chunk)
                if written > limit:
                    raise FileTooLargeError(
                        f"허용된 파일 크기({self._settings.stt_max_upload_mb}MB)를 초과했습니다.",
                        internal_detail=f"upload exceeded limit at {written} bytes",
                    )
                handle.write(chunk)
        if written == 0:
            raise ValidationError("빈 파일은 업로드할 수 없습니다.")
        return written

    def validate_and_commit(
        self, temp_path: Path, *, original_filename: str, size_bytes: int
    ) -> StoredAudio:
        """임시파일을 검증한 뒤 영구 저장소로 옮긴다.

        검증 실패 시 임시파일은 호출부의 `finally` 에서 정리된다 (Harness §22).

        Raises:
            ValidationError: 확장자 allowlist 위반 또는 시그니처 불일치.
            AudioTooLongError: 최대 재생시간 초과.
            AudioDecodeError: 컨테이너를 해석할 수 없음.
            StorageError: 파일 이동 실패.
        """
        extension = extract_extension(original_filename)
        if extension not in self._settings.allowed_extensions:
            raise ValidationError(
                "허용되지 않은 파일 형식입니다.",
                internal_detail=f"extension '{extension}' not in allowlist",
            )

        # 확장자 → 시그니처 → 재생시간 순으로 비용이 싼 검사부터 수행한다.
        verify_file_signature(temp_path, extension)
        probe = probe_audio(temp_path)

        if probe.duration_seconds > self._settings.max_audio_seconds:
            raise AudioTooLongError(
                f"허용된 재생시간({self._settings.stt_max_audio_minutes}분)을 초과했습니다.",
                internal_detail=f"duration {probe.duration_seconds:.1f}s exceeds limit",
            )

        digest = sha256_file(temp_path)
        relpath = self._build_relpath(extension)
        destination = resolve_within(self._root, relpath)
        destination.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.move(str(temp_path), str(destination))
        except OSError as exc:
            logger.error(
                "failed to move uploaded audio into storage",
                extra={"event": "AUDIO_STORE_FAILED", "reason": type(exc).__name__},
            )
            raise StorageError(internal_detail=f"move failed: {type(exc).__name__}") from exc

        return StoredAudio(
            relpath=relpath,
            display_filename=sanitize_display_filename(original_filename),
            extension=extension,
            size_bytes=size_bytes,
            sha256=digest,
            probe=probe,
        )

    def _build_relpath(self, extension: str) -> str:
        """저장 상대경로를 만든다.

        사용자 파일명을 그대로 쓰면 Path Traversal 위험이 있으므로 (Harness §6)
        서버가 생성한 UUID 만 사용하고, 날짜 디렉터리로 나눠 한 디렉터리의 파일 수를 제한한다.
        """
        now = datetime.now(UTC)
        return f"{now:%Y/%m}/{uuid.uuid4().hex}.{extension}"

    # --- 조회 / 삭제 --------------------------------------------------------

    def absolute_path(self, relpath: str) -> Path:
        """상대경로를 저장 루트 하위의 절대경로로 변환한다.

        DB 값이라도 신뢰하지 않고 루트 하위인지 재검증한다 (다층 방어).
        """
        return resolve_within(self._root, relpath)

    def exists(self, relpath: str) -> bool:
        return self.absolute_path(relpath).is_file()

    def delete(self, relpath: str) -> bool:
        """음성 파일을 삭제한다.

        Returns:
            실제로 파일을 지웠으면 True, 이미 없었으면 False.

        Raises:
            StorageError: 삭제에 실패한 경우. 삭제 실패를 무시하지 않는다 (Harness §21 / §22).
        """
        path = self.absolute_path(relpath)
        try:
            path.unlink()
        except FileNotFoundError:
            logger.warning(
                "audio file already absent on delete",
                extra={"event": "AUDIO_DELETE_MISSING"},
            )
            return False
        except OSError as exc:
            logger.error(
                "audio delete failed",
                extra={"event": "AUDIO_DELETE_FAILED", "reason": type(exc).__name__},
            )
            raise StorageError(internal_detail=f"unlink failed: {type(exc).__name__}") from exc
        return True


def cleanup_temp_file(path: Path) -> None:
    """임시파일을 정리한다 (Harness §22).

    정상 경로와 예외 경로 모두에서 호출되어야 하며, 삭제 실패는 무시하지 않고 로그로 남긴다.
    여기서 예외를 다시 던지면 원래의 처리 실패 원인을 가리게 되므로 기록만 한다.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.error(
            "temp file cleanup failed",
            extra={"event": "TEMP_CLEANUP_FAILED", "reason": type(exc).__name__},
        )


def purge_stale_temp_files(temp_dir: Path, *, max_age_hours: int) -> int:
    """오래된 임시파일을 제거한다 (Harness §21: 임시파일 무기한 보관 금지).

    Returns:
        삭제된 파일 수.
    """
    if not temp_dir.is_dir():
        return 0
    cutoff = datetime.now(UTC).timestamp() - max_age_hours * 3600
    removed = 0
    for entry in temp_dir.iterdir():
        if not entry.is_file() or entry.stat().st_mtime >= cutoff:
            continue
        cleanup_temp_file(entry)
        removed += 1
    return removed
