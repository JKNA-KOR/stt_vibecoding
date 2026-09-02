"""분석 상태 (Harness §25).

Job 상태와 분리한 이유는, 분석 실패가 전사 결과를 무효로 만들지 않기 때문이다.
Job 은 COMPLETED 로 남고 분석만 FAILED 가 된다.
"""

from __future__ import annotations

from enum import StrEnum


class AnalysisStatus(StrEnum):
    # 분석을 요청하지 않았거나 기능이 꺼져 있다.
    NONE = "NONE"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    # 기능이 꺼진 상태에서 요청된 경우. 실패와 구분한다.
    SKIPPED = "SKIPPED"
