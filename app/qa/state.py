"""QA 평가 상태 (Harness §25).

`AnalysisStatus` 와 같은 이유로 Job 상태와 분리한다. QA 가 실패해도 전사와 분석은
유효하며, Job 은 COMPLETED 로 남고 QA 상태만 FAILED 가 된다.
"""

from __future__ import annotations

from enum import StrEnum


class QAStatus(StrEnum):
    # 평가를 요청하지 않았거나 기능이 꺼져 있다.
    NONE = "NONE"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    # 기능이 꺼진 상태에서 요청된 경우. 실패와 구분한다.
    SKIPPED = "SKIPPED"
