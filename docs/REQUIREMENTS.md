# STT Service — 요구사항 정의서 (Requirements Specification)

> 녹취 파일 → 텍스트 변환 · 저장 · 관리 웹 서비스
> Version: 1.0
> Date: 2026-09-01
> Status: Baseline
> 상위 계약: [`STT_HARNESS_RULES.md`](../STT_HARNESS_RULES.md) v1.0

---

## 0. 문서 개요

### 0.1 목적

사용자가 녹취(음성) 파일을 웹으로 업로드하면 서버가 이를 텍스트로 변환하고, 변환 결과를 파일과 DB에 저장하며, 사용자가 조회·검색·다운로드·삭제할 수 있는 사내 웹 서비스의 요구사항을 정의한다.

### 0.2 상위 규칙과의 관계

본 문서는 `STT_HARNESS_RULES.md`의 **하위 문서**다. 본 문서와 Harness 규칙이 충돌하는 경우 **Harness 규칙이 우선**한다. 모든 요구사항 항목에는 근거가 되는 Harness 조항 번호를 병기한다.

Harness §1의 우선순위를 그대로 승계한다.

```
1. 정보보안 및 개인정보보호
2. 데이터 무결성 및 감사 가능성
3. 서비스 안정성
4. 기능 정확성
5. 성능
6. 개발 편의성
```

### 0.3 요구사항 ID 체계

| Prefix | 분류 |
|---|---|
| `FR-` | 기능 요구사항 (Functional Requirement) |
| `NFR-` | 비기능 요구사항 (Non-Functional Requirement) |
| `SEC-` | 보안 요구사항 (Security Requirement) |
| `AUD-` | 감사 요구사항 (Audit Requirement) |
| `DAT-` | 데이터 요구사항 (Data Requirement) |
| `OPS-` | 운영 요구사항 (Operational Requirement) |

강제 수준은 Harness §1의 `MUST` / `MUST NOT` / `SHOULD` / `MAY`를 사용한다.

---

## 1. 범위

### 1.1 In Scope

- 녹취 음성 파일 업로드 (웹 UI + REST API)
- 업로드 파일 검증 (확장자·시그니처·크기·재생시간)
- 비동기 Job 기반 STT 변환 (faster-whisper)
- Transcript 저장 (DB 메타데이터 + 파일 본문)
- Transcript 조회 / 검색 / 다운로드 (TXT, SRT, VTT, JSON)
- Job / Transcript 목록 관리 · 삭제
- 인증 및 RBAC 4역할 권한 관리
- 사내 SSO(OIDC/LDAP) 연동을 위한 Provider 인터페이스 분리
- Audit Log 기록 및 조회
- 보관기간(Retention) 정책 기반 자동 삭제
- 관리자 화면 (Job/Worker/Queue/설정/사용자/Audit)
- Health Check, 메트릭, 구조화 로깅

### 1.2 Out of Scope (본 버전)

Harness §2.1에 따라 STT Core와 분리해야 하는 기능으로, 본 버전에서는 **구현하지 않되 확장 지점만 남긴다.**

- LLM 요약 / 교정
- RAG
- 화자분리 (Diarization)
- 감성분석 / 상담평가 / 업무분류
- 개인정보 자동 마스킹
- 외부 API 연계
- 실시간 스트리밍 STT

이들은 `ENABLE_LLM_CORRECTION`, `ENABLE_DIARIZATION` 등 Feature Flag(Harness §54) 자리만 예약한다.

---

## 2. 용어 정의

| 용어 | 정의 |
|---|---|
| Job | 하나의 음성 파일에 대한 1회 STT 변환 작업 단위 |
| Transcript | Job의 산출물인 텍스트 결과 (raw / normalized 구분) |
| Segment | Transcript를 구성하는 시작–종료 시각을 가진 텍스트 조각 |
| Actor | Audit Log에 기록되는 행위 주체 (사용자 또는 시스템) |
| Provenance | STT 결과를 재현하기 위한 모델·설정·버전 정보 (Harness §5.3) |
| Retention | 음성/Transcript/Audit의 보관기간 정책 (Harness §21) |
| RTF | Real Time Factor = 처리시간 / 음성 재생시간 (Harness §30) |

---

## 3. 이해관계자 및 역할

Harness §10의 역할 모델을 그대로 채택한다.

| Role | 권한 |
|---|---|
| `USER` | 본인이 생성한 Job 조회·Transcript 조회·본인 Job 삭제. 업로드 가능 |
| `REVIEWER` | 허용된 업무범위(자신의 Job + 동일 조직 Job) 결과 조회. 업로드 가능 |
| `ADMIN` | 전체 Job 관리, 시스템/모델 설정 관리, 사용자 권한 관리 |
| `AUDITOR` | Audit Log 조회 및 Export 전용. **업무 데이터(Transcript 본문) 조회 불가** |

`AUDITOR`가 Transcript 본문에 접근할 수 없는 것은 Harness §46(개인정보 최소수집)과 직무분리 원칙에 따른 의도된 제약이다.

권한은 **반드시 백엔드에서 검증**한다 (Harness §10 MUST). UI에서 버튼을 숨기는 것은 권한 제어가 아니다.

---

## 4. 시스템 구성

Harness §2.1의 권장 구조를 따른다.

```text
[Web UI (Jinja2 + Vanilla JS)]
      |
      v
[FastAPI / Auth / RBAC]           <-- app/api, app/auth
      |
      v
[Job Service] --> [Redis / Celery Queue]   <-- app/jobs
      |                    |
      |                    v
      |            [Celery Worker Process]
      |                    |
      |     +--------------+--------------+
      |     |                             |
      v     v                             v
[Audit Logger]                  [Audio Preprocessor]   <-- app/audit, app/stt/preprocessing
(PostgreSQL, append-only)                |
                                         v
                                    [STT Core]          <-- app/stt (STTEngine 인터페이스)
                                    (FasterWhisperEngine)
                                         |
                                         v
                                [Transcript Normalizer]  <-- app/stt/normalization
                                         |
                                         v
                                   [Result Store]        <-- app/storage
                                   (PostgreSQL + FS)
                                         |
                                         +--> (예약) LLM / Summary / RAG
```

### 4.1 기술 스택 결정 및 근거

| 영역 | 선택 | 근거 |
|---|---|---|
| Python | 3.14 | 실행 환경에 3.14만 존재. `faster-whisper 1.2.1` / `ctranslate2 4.8.2` cp314 wheel 설치 가능함을 사전 검증 |
| Web Framework | FastAPI | Request/Response Schema Validation(Harness §11 MUST)을 Pydantic으로 강제 |
| Frontend | Jinja2 + Vanilla JS | Node 빌드 체인 불필요 → Supply-chain 표면 최소화(Harness §40), 오프라인 동작(Harness §42) |
| DB | PostgreSQL 18 | Audit append-only를 DB 권한/트리거로 강제 가능(Harness §19) |
| ORM / Migration | SQLAlchemy 2.x + Alembic | Parameterized Query 및 Migration 강제(Harness §12) |
| Queue | Redis + Celery | 동시 실행 수 제한·Timeout·Retry 제한을 Worker 레벨에서 강제(Harness §24) |
| STT Engine | faster-whisper (`medium`, int8) | 환경에 GPU 부재 → `device=cpu`, `compute_type=int8` |
| Audio Decode | PyAV (faster-whisper 번들) | 별도 ffmpeg 바이너리 의존 제거. ffmpeg 경로는 Feature Flag로 분리 |

---

## 5. 기능 요구사항 (FR)

### 5.1 인증 / 세션

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| FR-A-001 | MUST | 모든 STT 기능은 인증된 사용자만 사용할 수 있다. 미인증 요청은 `AUTHENTICATION_ERROR`로 거부한다 | §10 |
| FR-A-002 | MUST | 로그인은 `AuthProvider` 인터페이스를 통해 수행하며, 구현체로 `LocalPasswordProvider`를 제공한다 | §2.2 |
| FR-A-003 | MUST | 사내 SSO 연동을 위해 `OIDCProvider` / `LDAPProvider` 인터페이스 자리를 분리해 둔다. 본 버전에서는 미구현 상태이며 활성화 시 명시적으로 실패한다 | §4.3, §54 |
| FR-A-004 | MUST | 비밀번호는 평문 저장하지 않으며 검증된 KDF(bcrypt/argon2)로 해시한다 | §9 |
| FR-A-005 | MUST | 세션은 HttpOnly·SameSite=Lax 쿠키로 관리하며, 만료시간을 설정값으로 관리한다 | §9, §11 |
| FR-A-006 | MUST | 로그인 실패는 사용자 존재 여부를 구분해 노출하지 않는다 | §11, §44 |
| FR-A-007 | MUST | 초기 ADMIN 계정은 소스에 하드코딩하지 않고 부트스트랩 스크립트 + 환경변수로 생성한다 | §9 |
| FR-A-008 | SHOULD | 반복 로그인 실패는 계정 단위로 임계치 초과 시 일시 잠금한다 | §32 |

### 5.2 파일 업로드

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| FR-U-001 | MUST | 업로드 파일은 신뢰할 수 없는 외부 입력으로 취급한다 | §6 |
| FR-U-002 | MUST | 허용 확장자 allowlist를 설정값으로 관리한다 (기본: wav, mp3, m4a, flac, ogg, webm, mp4) | §6, §5.2 |
| FR-U-003 | MUST | 확장자와 별개로 파일 시그니처(magic bytes)를 검증하고 불일치 시 `FILE_FORMAT_ERROR`로 거부한다 | §6 |
| FR-U-004 | MUST | 최대 업로드 크기를 설정값으로 제한하고 초과 시 `FILE_TOO_LARGE`로 거부한다. 스트리밍 중 누적 바이트로 검사한다 | §6, §24 |
| FR-U-005 | MUST | 최대 재생시간을 설정값으로 제한하고 초과 시 `AUDIO_TOO_LONG`으로 거부한다 | §6, §24 |
| FR-U-006 | MUST | 저장 경로는 서버가 생성한 UUID 기반 경로만 사용하며, 사용자 제공 파일명을 경로 요소로 사용하지 않는다 | §6 |
| FR-U-007 | MUST | 최종 저장 경로가 지정된 저장 루트 하위임을 정규화 후 검증한다 (Path Traversal 방지) | §6, §33 |
| FR-U-008 | MUST | 원본 파일명은 표시 목적으로만 보관하며 저장 시 정제(sanitize)한다 | §6 |
| FR-U-009 | MUST | 업로드된 음성의 SHA-256을 계산해 저장한다 | §45 |
| FR-U-010 | SHOULD | 동일 SHA-256 + 동일 STT 설정의 기존 완료 Job이 있으면 사용자에게 알린다 | §45 |

### 5.3 STT 변환 Job

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| FR-J-001 | MUST | 긴 음성은 단일 동기 HTTP 요청에서 처리하지 않는다. 업로드 즉시 `job_id`를 반환하고 비동기 처리한다 | §25 |
| FR-J-002 | MUST | Job 상태는 `CREATED → QUEUED → PROCESSING → COMPLETED\|FAILED` 상태 머신을 따르며, `CANCELLED`/`EXPIRED`를 추가로 지원한다. 정의되지 않은 전이는 거부한다 | §25 |
| FR-J-003 | MUST | Job 생성 API는 `Idempotency-Key` 헤더를 지원하며, 동일 키 재요청 시 새 Job을 만들지 않고 기존 Job을 반환한다 | §26 |
| FR-J-004 | MUST | 동시 실행 Job 수, Queue 최대 길이, Job Timeout, 최대 재시도 횟수를 설정값으로 제한한다 | §24 |
| FR-J-005 | MUST | STT 모델은 Worker 프로세스당 1회만 로딩하고 재사용한다. 요청마다 로딩하지 않는다 | §5.1 |
| FR-J-006 | MUST | Job 실패 시 원인을 오류 분류 코드로 저장하고, 사용자에게는 내부 구현 정보를 노출하지 않는다 | §23, §44 |
| FR-J-007 | MUST | 처리 중 생성된 임시파일은 성공·실패 모두 `finally`에서 정리하며, 정리 실패는 로그로 남긴다 | §22 |
| FR-J-008 | MUST | 사용자는 본인 Job만 조회할 수 있다. 타인의 `job_id`를 직접 지정한 조회는 차단한다 | §10 |
| FR-J-009 | SHOULD | 대기/처리 중 Job은 소유자 또는 ADMIN이 취소할 수 있다 | §25 |
| FR-J-010 | MUST | Job 메타데이터에 Harness §65의 필드를 모두 저장한다 | §20, §65 |

### 5.4 Transcript 저장 및 관리

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| FR-T-001 | MUST | STT 원본 결과(`raw_transcript`)와 정규화 결과(`normalized_transcript`)를 분리 저장한다 | §50 |
| FR-T-002 | MUST NOT | 후처리 결과로 원본 STT 결과를 덮어쓰지 않는다 | §50, §61-16 |
| FR-T-003 | MUST | Transcript 본문은 파일로 저장하고 DB에는 메타데이터와 경로 참조만 저장한다. 파일 경로는 API 응답에 노출하지 않는다 | §44 |
| FR-T-004 | MUST | Transcript는 TXT / SRT / VTT / JSON 형식으로 다운로드할 수 있다 | — |
| FR-T-005 | MUST | Transcript는 Untrusted Data로 취급하며, 웹 화면 출력 시 반드시 이스케이프한다 | §13 |
| FR-T-006 | MUST | 각 처리 단계(raw → normalized)에 `processor`, `processor_version`, `created_at`, `input_version`을 기록한다 | §51 |
| FR-T-007 | SHOULD | Transcript 전문 검색(제목·본문 키워드)을 제공하며, 검색은 Parameterized Query로 수행한다 | §12 |
| FR-T-008 | MUST | Transcript 다운로드 권한은 조회 권한과 별개 권한으로 관리할 수 있어야 한다 | §43 |
| FR-T-009 | MUST | 삭제는 음성 삭제와 Transcript 삭제를 구분하며, 각각 별도 Audit 이벤트를 남긴다 | §17, §21 |

### 5.5 관리자 기능

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| FR-M-001 | MUST | 관리자 화면은 일반 사용자 화면과 라우트·권한을 분리한다 | §47 |
| FR-M-002 | MUST | Job 상태, Worker 상태, Queue 길이, Model Version, Model Load 상태를 조회할 수 있다 | §31, §47 |
| FR-M-003 | MUST | 사용자 역할 변경이 가능하며 변경은 Audit 대상이다 | §17, §47 |
| FR-M-004 | MUST | 런타임 설정 변경(모델/언어/beam_size/VAD/Retention 등)은 `who/when/previous_value/new_value/reason`을 기록한다. Secret 값 자체는 기록하지 않는다 | §37 |
| FR-M-005 | MUST | 대량 삭제는 `권한확인 → 대상 건수 표시 → 재확인 → 실행 → Audit` 절차를 거친다 | §48 |
| FR-M-006 | MUST | AUDITOR는 Audit Log 조회/Export만 가능하며, Export 행위 자체도 Audit에 기록한다 | §19, §43 |

### 5.6 Health / 관측성

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| FR-H-001 | MUST | `/live`(프로세스 생존)와 `/ready`(요청 수용 준비)를 분리 제공한다 | §56 |
| FR-H-002 | MUST | `/ready`는 DB·Queue 연결 및 모델 Load 상태를 반영한다 | §56 |
| FR-H-003 | MUST | Health 응답에 내부 호스트명·경로·버전 상세 등 불필요 정보를 노출하지 않는다 | §44 |

---

## 6. 비기능 요구사항 (NFR)

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| NFR-001 | MUST | 모든 운영 파라미터는 환경변수/설정파일로 외부화하며 코드에 하드코딩하지 않는다 | §5.2, §34 |
| NFR-002 | MUST | STT 엔진 호출부는 `STTEngine` 인터페이스로 격리하여 교체 가능하게 한다 | §2.2 |
| NFR-003 | MUST | 테스트용 `MockSTTEngine`을 제공하여 모델 없이 전 기능 테스트가 가능해야 한다 | §2.2, §27 |
| NFR-004 | MUST | Queue 백엔드는 인터페이스로 격리한다 (Celery 구현 + 테스트용 Inline 구현) | §2.2 |
| NFR-005 | MUST | 다음 성능 지표를 수집한다: `audio_duration_seconds`, `processing_duration_seconds`, `real_time_factor`, `queue_wait_seconds`, `failure_rate`, `retry_rate` | §30 |
| NFR-006 | MUST | 환경은 `local / dev / test / prod`로 분리하며 환경별 값은 Config로 관리한다 | §34 |
| NFR-007 | MUST NOT | Production에서 `DEBUG=True`, 상세 Stack Trace 외부노출, SQL Debug Logging, 전체 Request Body Logging을 사용하지 않는다. 설정 검증 단계에서 강제한다 | §35 |
| NFR-008 | MUST | 배포는 `application_version` + `git_commit`으로 식별 가능해야 한다 | §38 |
| NFR-009 | MUST | 함수 단일 책임, Type Hint, Public Interface Docstring, Magic Number의 Config화를 적용한다 | §52 |
| NFR-010 | MUST | 주석은 "무엇"이 아니라 "왜"를 설명한다 | §53 |
| NFR-011 | SHOULD | 기본 동작에 외부 인터넷 연결을 요구하지 않는다. 모델은 사전 반입 스크립트로 준비한다 | §42 |
| NFR-012 | MUST | Dependency는 버전 고정 Manifest로 관리하며, 설치만 하고 Manifest를 갱신하지 않는 행위를 금지한다 | §4.5, §40 |

---

## 7. 보안 요구사항 (SEC)

### 7.1 입력 / 실행

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| SEC-001 | MUST | 모든 API는 Request Schema Validation을 거친다 | §11 |
| SEC-002 | MUST NOT | 외부 프로세스 실행 시 `shell=True`를 사용하지 않으며 사용자 입력으로 Shell 문자열을 조합하지 않는다 | §7, §61-9 |
| SEC-003 | MUST | 외부 프로세스는 인자 리스트 형태로 실행하고 Timeout·반환코드 검증·stderr 처리를 적용한다 | §7 |
| SEC-004 | MUST | 실행 가능한 외부 바이너리는 allowlist로 제한한다 | §7 |
| SEC-005 | MUST | 임시파일은 지정된 Temp Directory에만 생성한다 | §22 |
| SEC-006 | MUST NOT | 광범위한 `except Exception: pass` 또는 `return None`으로 오류를 은폐하지 않는다 | §4.3, §61-14 |
| SEC-007 | MUST | 시스템 오류와 사용자 입력 오류를 구분한다 | §4.3 |

### 7.2 인증 / 권한

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| SEC-010 | MUST | 권한 검증은 백엔드에서 수행한다 | §10 |
| SEC-011 | MUST | Object 단위 접근통제를 적용한다 (타 사용자 `job_id` 조회 차단) | §10 |
| SEC-012 | MUST NOT | 클라이언트가 `is_admin` 등 권한 값을 전달하도록 설계하지 않는다 | §10 |
| SEC-013 | MUST | 관리자 API는 별도 Role 검증을 거친다 | §10, §47 |

### 7.3 데이터 / Secret

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| SEC-020 | MUST | 음성과 Transcript는 기본 `Confidential` 등급으로 취급한다 | §8.1 |
| SEC-021 | MUST NOT | 음성·Transcript를 외부 API/외부 LLM/외부 디버깅 서비스로 전송하지 않는다 | §8.2, §61-10 |
| SEC-022 | MUST NOT | Secret을 소스코드·README·로그·오류메시지·프론트엔드 JS·테스트 Fixture에 저장하지 않는다 | §9 |
| SEC-023 | MUST | `.env`는 Git에 커밋하지 않으며 `.env.example`에는 더미 값만 둔다 | §9 |
| SEC-024 | MUST | Transcript는 데이터이며 시스템 명령으로 해석하지 않는다. 향후 LLM 연계 시 `Transcript=DATA`, `Prompt=CONTROL` 경계를 유지한다 | §13 |
| SEC-025 | MUST | DB 접근은 ORM/Parameterized Query만 사용하며 문자열 조합 SQL을 금지한다 | §12 |
| SEC-026 | MUST | 운영 DB 계정은 최소권한을 적용하고 운영/개발 DB를 분리한다 | §12 |

### 7.4 API

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| SEC-030 | MUST | 모든 요청에 `request_id`(Correlation ID)를 발급하고 응답 헤더·오류 응답에 포함한다 | §11 |
| SEC-031 | MUST | Rate Limit 또는 동시작업 제한을 적용한다 | §11, §24 |
| SEC-032 | MUST | 표준화된 오류 응답 `{code, message, request_id}`를 사용한다 | §23 |
| SEC-033 | MUST NOT | 응답에 내부 파일 경로·DB PK·Hostname·Stack Trace·GPU 상세·설정값을 노출하지 않는다 | §44 |
| SEC-034 | SHOULD | API Versioning(`/api/v1/...`), 보안 헤더, 최소 CORS 정책을 적용한다 | §11 |
| SEC-035 | MUST | 상태 변경 요청(POST/DELETE)은 CSRF 방어를 적용한다 | §11 |

### 7.5 테스트 데이터

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| SEC-040 | MUST NOT | 실제 고객 음성/Transcript를 Git에 커밋하지 않으며 운영 DB Dump를 테스트 데이터로 쓰지 않는다 | §57 |
| SEC-041 | MUST | Golden Dataset은 합성 또는 비식별 데이터를 기본으로 한다 | §28, §57 |

---

## 8. 감사 요구사항 (AUD)

### 8.1 로그 분리

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| AUD-001 | MUST | Application Log와 Audit Log를 물리적으로 분리한다 (App Log: stdout JSON / Audit Log: DB 테이블) | §14 |
| AUD-002 | MUST | Audit Log는 일반 Debug Log처럼 비활성화하거나 임의 삭제할 수 없다 | §14, §19 |
| AUD-003 | MUST | Application Log는 Structured JSON이며 `timestamp, level, service, environment, event, request_id`를 필수 포함한다 | §16 |

### 8.2 기록 금지 정보

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| AUD-010 | MUST NOT | 로그에 원본 음성, 전체 Transcript, Password, API Key, Access/Refresh Token, 주민등록번호, 계좌번호, 카드번호를 기록하지 않는다 | §15, §61-6 |
| AUD-011 | MUST | 로그 출력 직전 민감 키(`password`, `token`, `secret`, `authorization`, `transcript`, `text` 등)를 자동 마스킹하는 필터를 적용한다 | §15 |
| AUD-012 | MUST | 클라이언트 IP는 비식별화(마스킹)하여 기록한다 | §16 |
| AUD-013 | MUST | 파일 식별은 내용이 아닌 `audio_sha256`으로 수행한다 | §15, §45 |

### 8.3 필수 Audit 이벤트

Harness §17의 전 항목을 구현한다.

| 분류 | 이벤트 |
|---|---|
| 인증 | `LOGIN_SUCCESS`, `LOGIN_FAILED`, `LOGOUT`, `ACCESS_DENIED` |
| STT 처리 | `STT_JOB_CREATED`, `AUDIO_UPLOADED`, `STT_JOB_STARTED`, `STT_JOB_COMPLETED`, `STT_JOB_FAILED`, `TRANSCRIPT_VIEWED`, `TRANSCRIPT_DOWNLOADED`, `AUDIO_DOWNLOADED`, `AUDIO_DELETED`, `TRANSCRIPT_DELETED` |
| 관리자 | `USER_ROLE_CHANGED`, `SYSTEM_CONFIG_CHANGED`, `MODEL_CONFIG_CHANGED`, `MODEL_CHANGED`, `RETENTION_POLICY_CHANGED`, `AUDIT_LOG_EXPORTED` |
| Export | `BULK_EXPORT_STARTED`, `BULK_EXPORT_COMPLETED` |

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| AUD-020 | MUST | 위 이벤트를 누락 없이 기록한다 | §17 |
| AUD-021 | MUST | 각 Audit Event는 `event_time, event_type, actor_id, actor_role, action, target_type, target_id, result, request_id, source_ip(비식별), application_version`을 포함한다 | §18 |
| AUD-022 | MUST | STT 처리 이벤트는 추가로 `job_id, audio_sha256, model_name, model_version, stt_config_version`을 포함한다 | §18 |
| AUD-023 | MUST NOT | `metadata_json`에 개인정보와 Secret을 저장하지 않는다 | §64 |

### 8.4 무결성

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| AUD-030 | MUST | Audit 테이블은 Append-only로 운용한다. 애플리케이션 DB 계정에 UPDATE/DELETE 권한을 부여하지 않으며 DB 트리거로 이중 차단한다 | §19 |
| AUD-031 | MUST | Audit Log 조회·Export 행위 자체를 Audit에 기록한다 | §19 |
| AUD-032 | SHOULD | 이전 레코드 해시를 포함하는 Hash Chain으로 변조를 탐지 가능하게 한다 | §19 |
| AUD-033 | SHOULD | Audit 보관기간(`AUDIT_RETENTION_DAYS`)은 업무데이터 보관기간과 독립적으로 관리한다 | §19, §21 |

### 8.5 보안 이벤트 모니터링

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| AUD-040 | SHOULD | 반복 로그인 실패, 권한 없는 Transcript 접근, 대량 다운로드, 대량 삭제, 반복 대용량 업로드, 관리자 권한 변경, 모델 설정 변경, Audit Export를 보안 모니터링 대상으로 표시한다 | §32 |

---

## 9. 데이터 요구사항 (DAT)

### 9.1 논리 데이터 모델

```text
user (1) ──< job (1) ──< transcript
                 │
                 └──< job_event
audit_event  (독립, append-only)
idempotency_key
config_change  (설정 변경 이력)
```

### 9.2 주요 엔터티

**`user`**
`id, username, display_name, role, password_hash, auth_provider, is_active, failed_login_count, locked_until, created_at, updated_at`

Harness §46(최소수집)에 따라 주민번호·개인 전화번호·주소·조직 상세는 저장하지 않는다.

**`job`** — Harness §65 준수
`id(job_id), created_by, status, original_filename, audio_path, audio_sha256, audio_size_bytes, audio_duration_seconds, engine, model_name, model_version, model_artifact_hash, language, beam_size, vad_enabled, compute_type, device_type, preprocessor_version, normalizer_version, application_version, stt_config_version, idempotency_key, error_code, retry_count, created_at, queued_at, started_at, completed_at, processing_duration_seconds, queue_wait_seconds, real_time_factor, audio_deleted_at, expires_at`

**`transcript`** — Harness §50, §51 준수
`id, job_id, kind(RAW|NORMALIZED), storage_path, char_count, segment_count, language, processor, processor_version, input_transcript_id, created_at, expires_at`

**`audit_event`** — Harness §64 준수
`id, event_time, event_type, actor_id, actor_role, action, target_type, target_id, result, request_id, job_id, source_ip_masked, application_version, model_version, metadata_json, prev_hash, record_hash`

**`idempotency_key`** — Harness §26
`key, actor_id, endpoint, request_fingerprint, job_id, created_at`

### 9.3 보관 정책

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| DAT-001 | MUST | `AUDIO_RETENTION_DAYS`, `TRANSCRIPT_RETENTION_DAYS`, `AUDIT_RETENTION_DAYS`를 설정값으로 관리한다 | §21 |
| DAT-002 | MUST NOT | 코드에 보관기간을 하드코딩하지 않는다 | §21 |
| DAT-003 | MUST | 보관기간 만료 시 정책에 따라 삭제하고 삭제 성공/실패를 기록한다 | §21 |
| DAT-004 | MUST | 사용자 요청 삭제와 자동 만료 삭제를 Audit에서 구분한다 | §21 |
| DAT-005 | MUST | Audit Log는 업무데이터 삭제와 독립적으로 관리한다 | §21 |
| DAT-006 | MUST NOT | 임시파일을 무기한 보관하지 않는다 | §21, §22 |

### 9.4 재현성

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| DAT-010 | MUST | `Audio Hash + Application Version + Model Version + Model Config + Preprocessing Version + Normalization Version`으로 Transcript를 재현할 수 있어야 한다 | §20 |
| DAT-011 | MUST | `stt_config_version`은 STT 설정 조합의 안정적 해시로 산출한다 | §20, §37 |

---

## 10. API 명세 (요약)

Base: `/api/v1`

### 10.1 인증

| Method | Path | 권한 | 설명 |
|---|---|---|---|
| POST | `/auth/login` | - | 로그인. 세션 쿠키 발급 |
| POST | `/auth/logout` | 인증 | 로그아웃 |
| GET | `/auth/me` | 인증 | 현재 사용자 정보 (최소 필드) |

### 10.2 STT Job

| Method | Path | 권한 | 설명 |
|---|---|---|---|
| POST | `/stt/jobs` | USER+ | multipart 업로드. `Idempotency-Key` 지원. `job_id` 반환 |
| GET | `/stt/jobs` | USER+ | 본인 Job 목록 (ADMIN은 전체). 페이지네이션·상태 필터 |
| GET | `/stt/jobs/{job_id}` | 소유자/ADMIN | Job 상태 및 메타데이터 |
| POST | `/stt/jobs/{job_id}/cancel` | 소유자/ADMIN | 대기·처리 중 Job 취소 |
| DELETE | `/stt/jobs/{job_id}` | 소유자/ADMIN | Job 및 산출물 삭제 |
| GET | `/stt/jobs/{job_id}/transcript` | 소유자/ADMIN | Transcript 조회 (JSON) |
| GET | `/stt/jobs/{job_id}/transcript/download` | 다운로드 권한 | `format=txt\|srt\|vtt\|json` |
| GET | `/stt/jobs/{job_id}/audio` | 다운로드 권한 | 원본 음성 다운로드 |

### 10.3 관리자

| Method | Path | 권한 | 설명 |
|---|---|---|---|
| GET | `/admin/system` | ADMIN | 모델/Worker/Queue 상태 |
| GET | `/admin/users` | ADMIN | 사용자 목록 |
| PATCH | `/admin/users/{user_id}/role` | ADMIN | 역할 변경 (`reason` 필수) |
| GET | `/admin/config` | ADMIN | 런타임 설정 조회 (Secret 마스킹) |
| PATCH | `/admin/config` | ADMIN | 런타임 설정 변경 (`reason` 필수) |
| POST | `/admin/jobs/bulk-delete` | ADMIN | 2단계 확인 대량 삭제 |

### 10.4 감사

| Method | Path | 권한 | 설명 |
|---|---|---|---|
| GET | `/audit/events` | AUDITOR/ADMIN | Audit 조회 (조회 행위 자체도 기록) |
| GET | `/audit/events/export` | AUDITOR/ADMIN | CSV Export (`AUDIT_LOG_EXPORTED` 기록) |
| GET | `/audit/verify` | AUDITOR/ADMIN | Hash Chain 무결성 검증 |

### 10.5 Health

| Method | Path | 권한 | 설명 |
|---|---|---|---|
| GET | `/live` | - | Liveness |
| GET | `/ready` | - | Readiness (DB/Queue/모델) |

### 10.6 표준 오류 응답

```json
{
  "code": "AUDIO_DECODE_ERROR",
  "message": "음성 파일을 처리할 수 없습니다.",
  "request_id": "req-..."
}
```

오류 분류는 Harness §23의 전 항목을 사용한다.

```text
VALIDATION_ERROR / AUTHENTICATION_ERROR / AUTHORIZATION_ERROR
FILE_FORMAT_ERROR / FILE_TOO_LARGE / AUDIO_TOO_LONG / AUDIO_DECODE_ERROR
STT_MODEL_ERROR / GPU_RESOURCE_ERROR / QUEUE_ERROR
DATABASE_ERROR / STORAGE_ERROR / INTERNAL_ERROR
```

---

## 11. Job 상태 머신

```text
        CREATED
           │ enqueue
           ▼
        QUEUED ──────────────► CANCELLED
           │ worker pick          ▲
           ▼                      │
       PROCESSING ────────────────┘
           │
     ┌─────┴─────┐
     ▼           ▼
 COMPLETED     FAILED ──retry(<max)──► QUEUED
     │
     │ retention 만료
     ▼
  EXPIRED
```

허용된 전이만 수행하며, 그 외 전이 시도는 `VALIDATION_ERROR`로 거부하고 로그를 남긴다.

---

## 12. 테스트 요구사항

Harness §27의 계층을 모두 둔다.

| 계층 | 위치 | 내용 |
|---|---|---|
| Unit | `tests/unit/` | 검증기, 상태머신, 정규화, 해시, 마스킹, 설정 검증 |
| Integration | `tests/integration/` | 업로드→Job→Transcript 전 경로 (MockSTTEngine + Inline Queue) |
| Security | `tests/security/` | Path Traversal, 확장자 위장, 크기·길이 초과, 타 사용자 Job 접근, 권한 상승 시도, 로그 마스킹, `shell=True` 부재, Audit UPDATE/DELETE 차단 |
| Regression | `tests/regression/` | 정규화 규칙 회귀 |
| Golden | `tests/golden/` | 합성 음성 기반 STT 품질 회귀 |

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| TST-001 | MUST | 기능 구현은 테스트 없이 완료 처리하지 않는다 | §27 |
| TST-002 | MUST NOT | 테스트를 통과시키기 위해 테스트를 삭제·약화하지 않는다 | §4.2, §61 |
| TST-003 | MUST | 모델·설정 변경 시 Golden Dataset을 재평가한다 | §29, §36 |
| TST-004 | MUST NOT | WER 단일 지표만으로 Release를 판정하지 않는다. 숫자/금액/날짜/전문용어 정확도를 별도 지표로 본다 | §29 |
| TST-005 | MUST | Golden Dataset은 일반대화·회의·전화음질·빠른발화·작은목소리·잡음·숫자·날짜·금액·영문혼용·전문용어·복수화자·장시간 유형을 지향한다 | §28 |

---

## 13. 운영 요구사항 (OPS)

| ID | 강제 | 요구사항 | Harness |
|---|---|---|---|
| OPS-001 | MUST | Release 전 Unit/Integration/Security/Golden Regression/Dependency Scan/Config Validation/Migration Validation을 통과해야 한다 | §38 |
| OPS-002 | MUST | 모든 배포는 Rollback 가능해야 하며, 모델 변경과 코드 변경을 독립적으로 되돌릴 수 있어야 한다 | §39 |
| OPS-003 | MUST | 모델·Quantization·compute_type·beam_size·VAD·전처리·정규화 변경은 Release 영향 변경으로 취급하고 변경사유·회귀결과·성능비교·Release Note·버전증가·Rollback 방법을 남긴다 | §36 |
| OPS-004 | MUST | Docker 사용 시 비root 실행, 최소 Base Image, Secret 미포함, 불필요 포트 미개방, Health Check를 적용한다 | §41 |
| OPS-005 | MUST | 불필요한 Outbound 네트워크 호출을 하지 않으며 외부 연결 대상은 allowlist로 관리한다 | §42 |
| OPS-006 | SHOULD | SBOM 생성, 이미지 스캔, 의존성 취약점 스캔을 수행한다 | §40 |
| OPS-007 | MUST | 운영 데이터를 개발환경으로 복사하지 않는다 | §34 |
| OPS-008 | MUST | 장애 분석은 Request ID / Job ID 기반으로 수행하며, 실제 음성·Transcript의 개발자 PC 복사를 기본 수단으로 삼지 않는다 | §35, §49 |

---

## 14. 설정 항목

Harness §63을 기준으로 하되 본 서비스에 필요한 항목을 추가한다. 전체 목록과 기본값은 `.env.example` 참조.

```env
APP_ENV=local|dev|test|prod
APP_VERSION / GIT_COMMIT

# STT (Harness §5.2)
STT_ENGINE / STT_MODEL_NAME / STT_MODEL_PATH / STT_DEVICE / STT_COMPUTE_TYPE
STT_LANGUAGE / STT_BEAM_SIZE / STT_VAD_ENABLED

# Resource (Harness §24)
STT_MAX_UPLOAD_MB / STT_MAX_AUDIO_MINUTES / STT_MAX_CONCURRENT_JOBS
STT_JOB_TIMEOUT_SECONDS / STT_MAX_RETRIES / STT_QUEUE_MAX_LENGTH

# Retention (Harness §21)
AUDIO_RETENTION_DAYS / TRANSCRIPT_RETENTION_DAYS / AUDIT_RETENTION_DAYS

# Infra
DATABASE_URL / REDIS_URL / STORAGE_ROOT / TEMP_DIR

# Auth
AUTH_PROVIDER=local|oidc|ldap
SESSION_SECRET / SESSION_MAX_AGE_SECONDS
LOGIN_MAX_FAILED_ATTEMPTS / LOGIN_LOCK_SECONDS

# Feature Flag (Harness §54)
ENABLE_LLM_CORRECTION / ENABLE_DIARIZATION / ENABLE_FFMPEG_PREPROCESS
ENABLE_AUDIO_DOWNLOAD / ENABLE_TRANSCRIPT_DOWNLOAD

LOG_LEVEL
```

설정 변경 감사 대상(Harness §37): `STT_MODEL`, `LANGUAGE`, `BEAM_SIZE`, `VAD`, `MAX_AUDIO_DURATION`, `MAX_UPLOAD_SIZE`, `WORKER_COUNT`, `RETENTION_POLICY`

---

## 15. 수용 기준 (Definition of Done)

Harness §60을 본 프로젝트의 완료 기준으로 그대로 채택한다.

- [ ] 요구사항이 구현됨
- [ ] 기존 아키텍처 규칙 준수
- [ ] 입력 Validation 구현
- [ ] 인증/권한 영향 검토
- [ ] 민감정보 로그 미포함
- [ ] 예외 처리 구현
- [ ] Audit 대상 여부 검토
- [ ] Unit Test 작성
- [ ] 필요한 Integration Test 작성
- [ ] 기존 테스트 PASS
- [ ] Security Checklist PASS
- [ ] Golden STT Regression 영향 확인
- [ ] Dependency 변경 검토
- [ ] Config 문서 갱신
- [ ] README 또는 관련 문서 갱신
- [ ] Rollback 가능
- [ ] 운영 관측성(로그/Metric) 확보

---

## 16. 제약사항 및 가정

### 16.1 현재 실행 환경 제약

| 항목 | 값 | 영향 |
|---|---|---|
| Python | 3.14.4 (유일) | `faster-whisper 1.2.1` / `ctranslate2 4.8.2` cp314 wheel 사용 |
| GPU | 없음 | `STT_DEVICE=cpu`, `STT_COMPUTE_TYPE=int8`. `GPU_RESOURCE_ERROR` 경로는 정의만 유지 |
| RAM | 총 7GB / 가용 약 2GB | `medium` int8(~1.5GB) + PG + Redis 동시 구동 시 스왑 가능. 개발 시 `small` 권장 |
| CPU | 16 core | Worker 동시성은 `STT_MAX_CONCURRENT_JOBS`로 제한 |
| ffmpeg | 미설치 | PyAV 기반 디코딩을 기본 경로로 사용. ffmpeg 경로는 `ENABLE_FFMPEG_PREPROCESS`로 분리 |
| Redis | 미설치 | `docker-compose.yml`로 제공 |

### 16.2 가정

- 서비스는 사내망(On-Premise)에서만 접근 가능하며 공개 인터넷에 노출하지 않는다.
- 모델 아티팩트는 승인된 경로로 사전 반입한다 (Harness §42).
- 조직 SSO 연동은 별도 승인 후 진행하며 본 버전에서는 인터페이스만 준비한다.

---

## 17. 추적 매트릭스 (Harness 조항 ↔ 요구사항)

| Harness | 요구사항 ID |
|---|---|
| §2.1 STT Core 분리 | §4 시스템 구성, §1.2 Out of Scope |
| §2.2 교체 가능 Engine | NFR-002, NFR-003, NFR-004, FR-A-002 |
| §3 Repository 구조 | §4 시스템 구성 |
| §4.3 명시적 실패 | SEC-006, SEC-007 |
| §4.5 Dependency | NFR-012 |
| §5.1 모델 초기화 | FR-J-005 |
| §5.2 설정 외부화 | NFR-001, §14 |
| §5.3 Provenance | FR-J-010, DAT-010 |
| §6 입력 음성 보안 | FR-U-001 ~ FR-U-009 |
| §7 외부 프로세스 | SEC-002 ~ SEC-004 |
| §8 개인정보 | SEC-020, SEC-021 |
| §9 Secret | SEC-022, SEC-023, FR-A-004, FR-A-007 |
| §10 인증/권한 | FR-A-001, SEC-010 ~ SEC-013, §3 역할 |
| §11 API Secure Coding | SEC-001, SEC-030 ~ SEC-035 |
| §12 SQL/DB | SEC-025, SEC-026 |
| §13 Transcript 보안 | FR-T-005, SEC-024 |
| §14~16 로그 | AUD-001 ~ AUD-003 |
| §15 금지 정보 | AUD-010 ~ AUD-013 |
| §17~18 Audit 이벤트/필드 | AUD-020 ~ AUD-023 |
| §19 Audit 무결성 | AUD-030 ~ AUD-033 |
| §20 재현성 | DAT-010, DAT-011 |
| §21 보관정책 | DAT-001 ~ DAT-006 |
| §22 임시파일 | FR-J-007, SEC-005 |
| §23 오류 분류 | SEC-032, §10.6 |
| §24 Resource 보호 | FR-J-004, FR-U-004, FR-U-005, SEC-031 |
| §25 Job 처리 | FR-J-001, FR-J-002, §11 |
| §26 Idempotency | FR-J-003 |
| §27~29 테스트 | TST-001 ~ TST-005, §12 |
| §30~31 성능/모니터링 | NFR-005, FR-M-002 |
| §32 보안 모니터링 | AUD-040, FR-A-008 |
| §33 Checklist | §7 전체 |
| §34~35 환경/Debug | NFR-006, NFR-007 |
| §36~37 변경관리 | OPS-003, FR-M-004 |
| §38~39 배포/Rollback | OPS-001, OPS-002, NFR-008 |
| §40~42 공급망/컨테이너/네트워크 | NFR-012, OPS-004 ~ OPS-006, NFR-011 |
| §43 다운로드 통제 | FR-T-008, AUD-020 |
| §44 응답 최소화 | SEC-033, FR-T-003, FR-H-003 |
| §45 파일 Hash | FR-U-009, AUD-013 |
| §46 최소수집 | §9.2 user |
| §47 관리자 화면 | FR-M-001 ~ FR-M-006 |
| §48 삭제 안전장치 | FR-M-005 |
| §49 Debug 대응 | OPS-008 |
| §50~51 LLM/Lineage | FR-T-001, FR-T-002, FR-T-006 |
| §52~53 코드 품질 | NFR-009, NFR-010 |
| §54~55 Flag/장애격리 | §14 Feature Flag, §1.2 |
| §56 Health | FR-H-001 ~ FR-H-003 |
| §57 테스트 데이터 | SEC-040, SEC-041 |
| §60 DoD | §15 |
| §63~65 설정/스키마 | §14, §9.2 |

---

## Change History

| Version | Date | Description |
|---|---|---|
| 1.0 | 2026-09-01 | 최초 요구사항 baseline |
