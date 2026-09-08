"""녹취서버 수집 드라이버 (Harness §5.2 / §6 / §22).

두 방식을 같은 인터페이스 뒤에 둔다. 상위(수집 서비스)는 어느 쪽이 선택되었는지 몰라도
되며, 새 방식을 붙일 때 레지스트리에만 등록하면 된다 — STT 엔진·LLM Provider 와 같은
구조다 (NFR-002).

**수집된 파일은 업로드와 같은 검증을 지난다.** 이 계층은 "파일을 가져오는 일"만 하고,
확장자·시그니처·크기·재생시간 검사는 `JobService.create_job` 이 그대로 수행한다.
입구가 늘어도 규칙이 갈라지면 약한 쪽이 우회로가 된다 (Harness §6).
"""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import ConfigurationError, Settings
from app.core.exceptions import StorageError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

# 한 번에 읽어 들일 조각 크기. 업로드 경로와 같은 값을 쓴다.
_CHUNK_BYTES = 1024 * 1024
# 녹취서버 목록 응답 상한. 응답이 통째로 메모리에 올라오므로 상한을 둔다 (Harness §24).
_MAX_LIST_BYTES = 8 * 1024 * 1024


@dataclass(slots=True)
class PendingRecording:
    """수집 대상 한 건.

    `open_stream` 이 지연 호출인 이유는, 목록만 보고 건너뛸 건에 대해 음성을 내려받지
    않기 위해서다 — 이미 처리한 건을 매번 받아 오면 수집이 느려지고 대역을 낭비한다.
    """

    source_id: str
    filename: str
    recorded_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # 실제 바이트를 흘려보내는 함수. 수집 서비스가 필요할 때만 부른다.
    open_stream: Any = None
    # folder 방식에서 원본 파일을 옮기기 위해 남긴다.
    local_path: Path | None = None


class RecordingSource(ABC):
    """녹취서버에서 처리 대상을 가져오는 통로."""

    source_name: str

    @abstractmethod
    def fetch(self, limit: int) -> list[PendingRecording]:
        """처리 대상 목록을 가져온다. 음성 본문은 아직 읽지 않는다."""

    @abstractmethod
    def mark_done(self, recording: PendingRecording) -> None:
        """수집에 성공한 건을 정리한다 (파일 이동 등)."""

    @abstractmethod
    def mark_failed(self, recording: PendingRecording, reason: str) -> None:
        """수집에 실패한 건을 정리한다. 원본은 지우지 않는다."""

    @abstractmethod
    def check(self) -> dict[str, object]:
        """연결·경로 상태를 점검한다. 관리 화면의 '연결 테스트' 가 쓴다."""


class FolderRecordingSource(RecordingSource):
    """공유 폴더에 떨어진 파일을 집어 온다.

    폐쇄망에서 가장 흔한 방식이며 녹취서버 쪽 개발이 필요 없다. 파일명 옆에 같은 이름의
    `.json` 을 두면 메타데이터로 읽는다 (docs/INTERFACE.md).

    처리한 파일은 **지우지 않고 옮긴다.** 수집이 잘못되었을 때 원본이 남아 있어야 다시
    넣을 수 있다 (Harness §22).
    """

    source_name = "folder"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._inbox = settings.recording_inbox_dir
        self._processed = settings.recording_processed_dir
        self._failed = settings.recording_failed_dir
        self._allowed = {
            f".{ext.strip().lower()}"
            for ext in settings.allowed_audio_extensions.split(",")
            if ext.strip()
        }

    def fetch(self, limit: int) -> list[PendingRecording]:
        if not self._inbox.is_dir():
            # 폴더가 없으면 조용히 0건으로 넘기지 않는다. 설정 실수를 알려야 한다 (§4.3).
            raise ConfigurationError(
                f"수집 폴더가 없다: {self._inbox}. RECORDING_INBOX_DIR 을 확인한다"
            )

        found: list[PendingRecording] = []
        # 이름순으로 처리한다. 수집 순서가 매번 달라지면 재현이 어렵다.
        for path in sorted(self._inbox.iterdir()):
            if len(found) >= limit:
                break
            if not path.is_file() or path.suffix.lower() not in self._allowed:
                continue
            found.append(self._to_pending(path))
        return found

    def _to_pending(self, path: Path) -> PendingRecording:
        metadata = self._read_sidecar(path)
        recorded_at = _parse_datetime(metadata.get("recorded_at"))
        if recorded_at is None:
            # 사이드카가 없으면 파일 수정 시각을 통화 시각으로 본다. 정확하지 않을 수
            # 있으므로 메타데이터에 출처를 남긴다.
            recorded_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            metadata.setdefault("recorded_at_source", "file_mtime")

        return PendingRecording(
            source_id=str(metadata.get("id") or path.name),
            filename=path.name,
            recorded_at=recorded_at,
            metadata=metadata,
            open_stream=lambda: _stream_file(path),
            local_path=path,
        )

    def _read_sidecar(self, path: Path) -> dict[str, Any]:
        """같은 이름의 `.json` 이 있으면 메타데이터로 읽는다.

        읽기에 실패해도 수집을 멈추지 않는다 — 메타데이터는 부가 정보이고, 음성 자체는
        멀쩡하다. 다만 조용히 넘기지 않고 경고로 남긴다.
        """
        sidecar = path.with_suffix(".json")
        if not sidecar.is_file():
            return {}
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "recording sidecar could not be read",
                extra={
                    "event": "INGEST_SIDECAR_UNREADABLE",
                    "reason": type(exc).__name__,
                },
            )
            return {}
        return payload if isinstance(payload, dict) else {}

    def mark_done(self, recording: PendingRecording) -> None:
        self._move(recording, self._processed)

    def mark_failed(self, recording: PendingRecording, reason: str) -> None:
        logger.warning(
            "recording moved to the failed folder",
            extra={
                "event": "INGEST_ITEM_FAILED",
                "source_id": recording.source_id,
                "reason": reason[:200],
            },
        )
        self._move(recording, self._failed)

    def _move(self, recording: PendingRecording, destination: Path) -> None:
        """원본과 사이드카를 함께 옮긴다. 둘이 흩어지면 나중에 짝을 맞출 수 없다."""
        if recording.local_path is None or not recording.local_path.exists():
            return
        destination.mkdir(parents=True, exist_ok=True)

        # 같은 이름이 이미 있으면 덮어쓰지 않는다. 덮어쓰면 원본이 사라진다.
        target = _unique_path(destination / recording.local_path.name)
        try:
            shutil.move(str(recording.local_path), str(target))
            sidecar = recording.local_path.with_suffix(".json")
            if sidecar.exists():
                shutil.move(str(sidecar), str(target.with_suffix(".json")))
        except OSError as exc:
            raise StorageError(
                internal_detail=f"failed to move recording: {type(exc).__name__}"
            ) from exc

    def check(self) -> dict[str, object]:
        pending = 0
        if self._inbox.is_dir():
            pending = sum(
                1
                for path in self._inbox.iterdir()
                if path.is_file() and path.suffix.lower() in self._allowed
            )
        return {
            "source": self.source_name,
            "reachable": self._inbox.is_dir(),
            "inbox": str(self._inbox),
            "pending": pending,
            "detail": "" if self._inbox.is_dir() else "수집 폴더가 없습니다.",
        }


class HttpRecordingSource(RecordingSource):
    """녹취서버 REST API 를 조회해 내려받는다.

    필드 이름은 설정으로 매핑한다. 이미 운영 중인 서버를 우리 규격에 맞춰 고치라고 할 수
    없는 경우가 많기 때문이다 — 그래서 이쪽만 매핑을 허용한다 (docs/INTERFACE.md).

    API 키는 헤더로만 나가며 로그·오류 메시지에 남지 않는다 (Harness §9 / §15).
    """

    source_name = "http"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = settings.recording_api_base_url.rstrip("/")
        self._api_key = settings.recording_api_key.get_secret_value()
        self._timeout = float(settings.recording_api_timeout_seconds)
        self._user_agent = f"{settings.app_name}/{settings.app_version}"

    def fetch(self, limit: int) -> list[PendingRecording]:
        payload = self._get_json(f"/recordings?limit={limit}")
        raw_items = payload.get(self._settings.recording_list_items_key)
        if not isinstance(raw_items, list):
            raise ValidationError(
                "녹취서버 응답 형식이 올바르지 않습니다.",
                internal_detail=(
                    f"list key '{self._settings.recording_list_items_key}' is missing"
                ),
            )

        found: list[PendingRecording] = []
        for raw in raw_items[:limit]:
            item = self._to_pending(raw)
            if item is not None:
                found.append(item)
        return found

    def _to_pending(self, raw: object) -> PendingRecording | None:
        """응답 한 건을 도메인 타입으로 옮긴다.

        형태가 어긋난 건은 건너뛰되 조용히 버리지 않는다 — 몇 건이 왜 빠졌는지 모르면
        "가져왔는데 없다"가 된다 (Harness §4.3).
        """
        if not isinstance(raw, dict):
            logger.warning(
                "recording item is not an object",
                extra={"event": "INGEST_ITEM_MALFORMED"},
            )
            return None

        fields = self._settings
        source_id = _text(raw.get(fields.recording_field_id))
        filename = _text(raw.get(fields.recording_field_filename))
        download = _text(raw.get(fields.recording_field_download_url))
        if not source_id or not filename or not download:
            logger.warning(
                "recording item is missing required fields",
                extra={
                    "event": "INGEST_ITEM_MALFORMED",
                    "has_id": bool(source_id),
                    "has_filename": bool(filename),
                    "has_download": bool(download),
                },
            )
            return None

        url = self._absolute(download)
        return PendingRecording(
            source_id=source_id,
            filename=filename,
            recorded_at=_parse_datetime(raw.get(fields.recording_field_recorded_at)),
            metadata={"download_url": url},
            open_stream=lambda: self._stream_download(url),
        )

    def _absolute(self, url: str) -> str:
        """상대경로면 기준 주소로 붙인다.

        붙인 결과가 기준 주소를 벗어나면 거절한다 — 녹취서버 응답이 조작되면 우리 서버가
        임의의 주소로 요청을 보내는 통로(SSRF)가 된다 (Harness §6).
        """
        if not url.startswith(("http://", "https://")):
            return f"{self._base_url}/{url.lstrip('/')}"

        base = urllib.parse.urlparse(self._base_url)
        target = urllib.parse.urlparse(url)
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            raise ValidationError(
                "녹취서버가 알 수 없는 주소를 지정했습니다.",
                internal_detail=f"download host {target.netloc} is not the configured host",
            )
        return url

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _get_json(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - 주소는 설정값이며 검증을 거친다
            self._base_url + path, headers=self._headers()
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                return json.loads(response.read(_MAX_LIST_BYTES))
        except urllib.error.HTTPError as exc:
            # 상태코드만 남긴다. 응답 본문에 키가 반사될 수 있다 (Harness §9).
            raise ValidationError(
                "녹취서버 조회에 실패했습니다.",
                internal_detail=f"recording api http {exc.code}",
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ValidationError(
                "녹취서버에 연결할 수 없습니다.",
                internal_detail=f"recording api unreachable: {type(exc).__name__}",
            ) from exc
        except json.JSONDecodeError as exc:
            raise ValidationError(
                "녹취서버 응답을 해석할 수 없습니다.",
                internal_detail="recording api response is not json",
            ) from exc

    def _stream_download(self, url: str) -> Iterator[bytes]:
        request = urllib.request.Request(url, headers=self._headers())  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                while chunk := response.read(_CHUNK_BYTES):
                    yield chunk
        except urllib.error.HTTPError as exc:
            raise ValidationError(
                "녹취 파일을 내려받지 못했습니다.",
                internal_detail=f"download http {exc.code}",
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ValidationError(
                "녹취 파일을 내려받지 못했습니다.",
                internal_detail=f"download failed: {type(exc).__name__}",
            ) from exc

    def mark_done(self, recording: PendingRecording) -> None:
        """HTTP 방식은 원본을 건드리지 않는다.

        녹취서버의 상태를 우리가 바꾸는 것은 위험하다 — 삭제나 상태 변경 API 를 부르면
        수집이 실패했을 때 되돌릴 수 없다. 중복 수집은 `source_id` 멱등 키로 막는다.
        """

    def mark_failed(self, recording: PendingRecording, reason: str) -> None:
        logger.warning(
            "recording could not be ingested",
            extra={
                "event": "INGEST_ITEM_FAILED",
                "source_id": recording.source_id,
                "reason": reason[:200],
            },
        )

    def check(self) -> dict[str, object]:
        try:
            payload = self._get_json("/recordings?limit=1")
        except ValidationError as exc:
            return {
                "source": self.source_name,
                "reachable": False,
                "endpoint": self._base_url,
                "pending": 0,
                "detail": exc.message,
                "internal": exc.internal_detail or "",
            }
        items = payload.get(self._settings.recording_list_items_key)
        return {
            "source": self.source_name,
            "reachable": True,
            "endpoint": self._base_url,
            "pending": len(items) if isinstance(items, list) else 0,
            "detail": "",
        }


_REGISTRY: dict[str, type[RecordingSource]] = {
    FolderRecordingSource.source_name: FolderRecordingSource,
    HttpRecordingSource.source_name: HttpRecordingSource,
}


def create_source(settings: Settings) -> RecordingSource:
    """설정에 맞는 수집 드라이버를 만든다.

    Raises:
        ConfigurationError: 수집이 꺼져 있거나 알 수 없는 방식인 경우.
    """
    name = settings.recording_source
    if name == "off":
        raise ConfigurationError(
            "녹취서버 수집이 꺼져 있다. RECORDING_SOURCE 를 folder 또는 http 로 설정한다"
        )
    driver = _REGISTRY.get(name)
    if driver is None:
        raise ConfigurationError(
            f"알 수 없는 RECORDING_SOURCE='{name}'. 사용 가능: {', '.join(sorted(_REGISTRY))}"
        )
    return driver(settings)


def _stream_file(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            yield chunk


def _unique_path(target: Path) -> Path:
    """이미 있는 이름이면 뒤에 번호를 붙인다. 덮어쓰지 않는다."""
    if not target.exists():
        return target
    for index in range(1, 1000):
        candidate = target.with_name(f"{target.stem}.{index}{target.suffix}")
        if not candidate.exists():
            return candidate
    raise StorageError(internal_detail=f"cannot find a free name for {target.name}")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = [
    "FolderRecordingSource",
    "HttpRecordingSource",
    "PendingRecording",
    "RecordingSource",
    "create_source",
]
