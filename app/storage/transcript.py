"""Transcript 파일 저장소 (Harness §44 / §50 / §51).

Transcript 본문은 DB 가 아니라 파일에 둔다. DB 에는 메타데이터와 상대경로만 남기고,
API 응답에는 경로를 노출하지 않는다.

RAW 와 NORMALIZED 는 서로 다른 파일로 저장된다. 후처리 결과가 원본을 덮어쓰는 일은 없다
(Harness §50 / §61-16).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings
from app.core.exceptions import StorageError
from app.core.logging import get_logger
from app.core.security import resolve_within
from app.stt.schemas import TranscriptKind, TranscriptSegment

logger = get_logger(__name__)

# 저장 파일 스키마 버전. 형식이 바뀌면 올리고 읽기 경로에서 분기한다.
TRANSCRIPT_FILE_VERSION = 1


class TranscriptStore:
    """Transcript 본문 파일의 저장·조회·삭제."""

    def __init__(self, settings: Settings) -> None:
        self._root = settings.transcript_dir

    def ensure_directories(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        *,
        job_id: str,
        kind: TranscriptKind,
        segments: list[TranscriptSegment],
        metadata: dict[str, object],
    ) -> str:
        """Transcript 를 JSON 파일로 저장하고 저장 루트 기준 상대경로를 반환한다.

        Raises:
            StorageError: 파일 기록에 실패한 경우.
        """
        now = datetime.now(UTC)
        relpath = f"{now:%Y/%m}/{job_id}.{kind.value.lower()}.{uuid.uuid4().hex[:8]}.json"
        destination = resolve_within(self._root, relpath)
        destination.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "file_version": TRANSCRIPT_FILE_VERSION,
            "job_id": job_id,
            "kind": kind.value,
            "created_at": now.isoformat(),
            "metadata": metadata,
            "segments": [
                {
                    "index": segment.index,
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text,
                    # 모델 확신도. 값을 주지 않는 엔진에서는 null 이다.
                    "confidence": segment.confidence,
                    # 발화자. 후처리를 거친 Transcript 에만 값이 있다.
                    "speaker": segment.speaker,
                }
                for segment in segments
            ],
        }

        # 부분 기록 파일이 남지 않도록 임시 파일에 쓴 뒤 원자적으로 교체한다.
        temp_path = destination.with_suffix(destination.suffix + ".tmp")
        try:
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            temp_path.replace(destination)
        except OSError as exc:
            logger.exception(
                "transcript write failed",
                extra={
                    "event": "TRANSCRIPT_WRITE_FAILED",
                    "job_id": job_id,
                    "reason": type(exc).__name__,
                },
            )
            raise StorageError(internal_detail=f"write failed: {type(exc).__name__}") from exc

        return relpath

    def read_segments(self, relpath: str) -> list[TranscriptSegment]:
        """저장된 Transcript 의 세그먼트를 읽는다.

        Raises:
            StorageError: 파일이 없거나 형식이 깨진 경우.
        """
        path = self._resolve(relpath)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise StorageError(internal_detail="transcript file missing") from exc
        except (OSError, json.JSONDecodeError) as exc:
            logger.exception(
                "transcript read failed",
                extra={"event": "TRANSCRIPT_READ_FAILED", "reason": type(exc).__name__},
            )
            raise StorageError(internal_detail=f"read failed: {type(exc).__name__}") from exc

        return [
            TranscriptSegment(
                index=int(item["index"]),
                start=float(item["start"]),
                end=float(item["end"]),
                text=str(item["text"]),
                # 이 필드 이전에 쓰인 파일에는 값이 없다. 없으면 없는 대로 읽는다.
                confidence=_optional_float(item.get("confidence")),
                speaker=_optional_text(item.get("speaker")),
            )
            for item in payload.get("segments", [])
        ]

    def read_metadata(self, relpath: str) -> dict[str, object]:
        path = self._resolve(relpath)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError(internal_detail=f"read failed: {type(exc).__name__}") from exc
        result = payload.get("metadata", {})
        return result if isinstance(result, dict) else {}

    def delete(self, relpath: str) -> bool:
        """Transcript 파일을 삭제한다. 실패는 무시하지 않는다 (Harness §21)."""
        path = self._resolve(relpath)
        try:
            path.unlink()
        except FileNotFoundError:
            logger.warning(
                "transcript file already absent on delete",
                extra={"event": "TRANSCRIPT_DELETE_MISSING"},
            )
            return False
        except OSError as exc:
            logger.exception(
                "transcript delete failed",
                extra={"event": "TRANSCRIPT_DELETE_FAILED", "reason": type(exc).__name__},
            )
            raise StorageError(internal_detail=f"unlink failed: {type(exc).__name__}") from exc
        return True

    def _resolve(self, relpath: str) -> Path:
        # DB 에 저장된 값이라도 경로 검증을 건너뛰지 않는다 (다층 방어, Harness §6).
        return resolve_within(self._root, relpath)


def _optional_float(value: object) -> float | None:
    """숫자면 float, 아니면 None. 구버전 파일과 손상된 값 모두 여기서 걸러진다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _optional_text(value: object) -> str | None:
    """문자열이면 그대로, 아니면 None. 구버전 파일에는 이 키가 없다."""
    return value if isinstance(value, str) and value else None
