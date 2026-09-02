"""Transcript 내보내기 형식 (FR-T-004).

Transcript 는 Untrusted Data 다 (Harness §13). 여기서 만드는 것은 순수 텍스트 산출물이며,
어떤 형식도 실행 가능한 문서(HTML 등)로 만들지 않는다. 화면 표시는 템플릿 자동 이스케이프가 담당한다.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import StrEnum

from app.core.exceptions import ValidationError
from app.stt.schemas import TranscriptSegment


class TranscriptFormat(StrEnum):
    TXT = "txt"
    SRT = "srt"
    VTT = "vtt"
    JSON = "json"


# 다운로드 응답의 Content-Type. 브라우저가 문서로 렌더링하지 않도록 전부 텍스트 계열로 고정한다.
MEDIA_TYPES: dict[TranscriptFormat, str] = {
    TranscriptFormat.TXT: "text/plain; charset=utf-8",
    TranscriptFormat.SRT: "text/plain; charset=utf-8",
    TranscriptFormat.VTT: "text/vtt; charset=utf-8",
    TranscriptFormat.JSON: "application/json; charset=utf-8",
}


def parse_format(raw: str) -> TranscriptFormat:
    """요청 파라미터를 형식 Enum 으로 변환한다. 알 수 없는 값은 거부한다."""
    try:
        return TranscriptFormat(raw.lower())
    except ValueError as exc:
        raise ValidationError(
            "지원하지 않는 다운로드 형식입니다.",
            internal_detail=f"unknown transcript format '{raw}'",
        ) from exc


def _format_timestamp(seconds: float, *, separator: str) -> str:
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def render_txt(segments: Sequence[TranscriptSegment]) -> str:
    return "\n".join(segment.text.strip() for segment in segments) + "\n"


def render_srt(segments: Sequence[TranscriptSegment]) -> str:
    blocks: list[str] = []
    for position, segment in enumerate(segments, start=1):
        start = _format_timestamp(segment.start, separator=",")
        end = _format_timestamp(segment.end, separator=",")
        blocks.append(f"{position}\n{start} --> {end}\n{segment.text.strip()}\n")
    return "\n".join(blocks)


def render_vtt(segments: Sequence[TranscriptSegment]) -> str:
    lines = ["WEBVTT", ""]
    for segment in segments:
        start = _format_timestamp(segment.start, separator=".")
        end = _format_timestamp(segment.end, separator=".")
        lines.append(f"{start} --> {end}")
        lines.append(segment.text.strip())
        lines.append("")
    return "\n".join(lines)


def render_json(segments: Sequence[TranscriptSegment], *, metadata: dict[str, object]) -> str:
    payload = {
        "metadata": metadata,
        "segments": [
            {
                "index": segment.index,
                "start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "text": segment.text,
            }
            for segment in segments
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render(
    fmt: TranscriptFormat,
    segments: Sequence[TranscriptSegment],
    *,
    metadata: dict[str, object] | None = None,
) -> str:
    """요청한 형식으로 Transcript 를 직렬화한다."""
    if fmt is TranscriptFormat.TXT:
        return render_txt(segments)
    if fmt is TranscriptFormat.SRT:
        return render_srt(segments)
    if fmt is TranscriptFormat.VTT:
        return render_vtt(segments)
    return render_json(segments, metadata=metadata or {})
