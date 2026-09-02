# STT Harness Engineering Rules
> faster-whisper 기반 사내 STT 프로그램 개발·보안·운영 표준  
> Version: 1.0  
> Status: Baseline / Mandatory  
> Scope: AI Coding Agent, 개발자, 운영자, 배포 파이프라인

---

## 0. 문서 목적

이 문서는 `faster-whisper` 기반 STT(Speech-to-Text) 프로그램을 바이브 코딩/AI Coding Agent 방식으로 지속 개발할 때 적용하는 **프로젝트 Harness 규칙**이다.

이 문서는 단순 코딩 스타일 가이드가 아니다. 다음 목적을 동시에 달성하기 위한 프로젝트 최상위 운영 계약(Engineering Contract)이다.

1. AI Coding Agent가 임의로 아키텍처를 훼손하지 않도록 개발 경계를 정의한다.
2. 빠른 기능 개발과 장기 유지보수성을 동시에 확보한다.
3. 음성 및 STT 결과에 포함될 수 있는 개인정보·기밀정보를 보호한다.
4. Secure Coding 원칙을 기본값으로 적용한다.
5. 사용자 행위와 시스템 처리 이력을 추적·감사할 수 있도록 한다.
6. 모델·설정·코드 변경에 따른 STT 품질 변화를 재현할 수 있도록 한다.
7. 장애 발생 시 원인 분석과 복구가 가능하도록 한다.
8. 향후 화자분리, 요약, RAG, LLM 후처리 등 기능 확장을 고려하되 STT Core와 분리한다.

본 프로젝트에 참여하는 사람과 AI Coding Agent는 **작업 시작 전 반드시 본 문서를 읽고 준수해야 한다.**

---

# 1. 규칙의 강제 수준

본 문서에서는 다음 용어를 사용한다.

- **MUST**: 반드시 준수한다. 위반 상태에서는 Merge/Release 할 수 없다.
- **MUST NOT**: 절대 수행하지 않는다.
- **SHOULD**: 특별한 사유가 없는 한 준수한다. 미준수 시 사유를 기록한다.
- **SHOULD NOT**: 특별한 사유가 없는 한 수행하지 않는다.
- **MAY**: 프로젝트 상황에 따라 선택할 수 있다.

규칙 충돌 시 우선순위는 다음과 같다.

1. 정보보안 및 개인정보보호
2. 데이터 무결성 및 감사 가능성
3. 서비스 안정성
4. 기능 정확성
5. 성능
6. 개발 편의성

---

# 2. 기본 설계 원칙

## 2.1 STT Core와 업무 기능 분리

STT 엔진은 독립적인 Core Service로 유지해야 한다.

권장 구조:

```text
[Web / Client]
      |
      v
[API / Auth]
      |
      v
[Job Manager / Queue]
      |
      +-----------------------+
      |                       |
      v                       v
[Audio Preprocessor]    [Audit Logger]
      |
      v
[STT Core]
(faster-whisper)
      |
      v
[Transcript Normalizer]
      |
      v
[Result Store]
      |
      +------> 향후 LLM / Summary / RAG / Agent
```

다음 기능은 STT Core 내부에 직접 결합하지 않는다.

- LLM 요약
- RAG
- 업무분류
- 감성분석
- 상담평가
- 개인정보 마스킹
- 문서 생성
- 외부 API 연계

이들은 별도 모듈 또는 후처리 Pipeline으로 구현한다.

---

## 2.2 교체 가능한 STT Engine

`faster-whisper`는 구현체이며 애플리케이션의 절대적인 종속점이 되어서는 안 된다.

MUST:

- STT 호출부는 Adapter/Service Interface를 통해 격리한다.
- 모델명, device, compute_type, beam_size 등의 설정을 코드에 하드코딩하지 않는다.
- 향후 다른 STT 엔진으로 교체할 수 있는 인터페이스를 유지한다.

예:

```python
class STTEngine:
    def transcribe(self, audio_path, options):
        ...
```

구체 구현:

```text
STTEngine
 ├── FasterWhisperEngine
 ├── FutureCommercialSTTEngine
 └── MockSTTEngine
```

---

# 3. 권장 Repository 구조

```text
stt-service/
├─ app/
│  ├─ api/
│  │  ├─ routes/
│  │  ├─ schemas/
│  │  └─ dependencies/
│  │
│  ├─ core/
│  │  ├─ config.py
│  │  ├─ security.py
│  │  ├─ exceptions.py
│  │  └─ logging.py
│  │
│  ├─ stt/
│  │  ├─ base.py
│  │  ├─ faster_whisper_engine.py
│  │  ├─ preprocessing.py
│  │  ├─ normalization.py
│  │  └─ schemas.py
│  │
│  ├─ jobs/
│  │  ├─ service.py
│  │  ├─ queue.py
│  │  └─ worker.py
│  │
│  ├─ audit/
│  │  ├─ service.py
│  │  └─ schemas.py
│  │
│  ├─ storage/
│  │  ├─ audio.py
│  │  ├─ transcript.py
│  │  └─ repository.py
│  │
│  └─ main.py
│
├─ tests/
│  ├─ unit/
│  ├─ integration/
│  ├─ security/
│  ├─ regression/
│  └─ golden/
│
├─ scripts/
├─ docs/
├─ config/
├─ migrations/
├─ .env.example
├─ pyproject.toml
├─ README.md
├─ SECURITY.md
├─ CHANGELOG.md
└─ STT_HARNESS_RULES.md
```

AI Coding Agent는 특별한 사유 없이 위 구조를 크게 변경하지 않는다.

---

# 4. AI Coding Agent / 바이브 코딩 기본 규칙

## 4.1 작업 시작 규칙

AI Coding Agent는 코드 변경 전에 MUST:

1. `STT_HARNESS_RULES.md`를 읽는다.
2. `README.md`를 읽는다.
3. 기존 프로젝트 구조와 관련 코드를 확인한다.
4. 기존 구현을 재사용할 수 있는지 먼저 확인한다.
5. 변경 영향 범위를 파악한다.
6. 보안, 개인정보, 데이터 삭제가 관련되는지 확인한다.
7. 테스트 전략을 결정한다.

새 기능을 구현하기 전에 기존 기능을 중복 구현하지 않는다.

---

## 4.2 최소 변경 원칙

AI Coding Agent는 **요청한 문제를 해결하기 위한 최소 범위만 변경**한다.

MUST NOT:

- 요청하지 않은 대규모 Refactoring
- 관련 없는 파일의 포맷 변경
- 기존 API Breaking Change
- 임의 DB Schema 변경
- 임의 Dependency 교체
- 기존 보안 로직 제거
- 테스트를 통과시키기 위한 테스트 삭제
- 예외를 숨기기 위한 광범위한 `try/except`
- 검증 로직 우회
- 로그/감사 기능 비활성화

---

## 4.3 명시적 실패 원칙

오류를 숨기지 않는다.

금지:

```python
try:
    ...
except Exception:
    pass
```

금지:

```python
except Exception:
    return None
```

원칙:

```python
try:
    ...
except KnownException as exc:
    logger.error(...)
    raise ApplicationException(...) from exc
```

시스템 오류와 사용자 입력 오류는 반드시 구분한다.

---

## 4.4 기존 파일 삭제 금지

AI Coding Agent는 명시적인 요청 없이 다음을 삭제하지 않는다.

- 사용자 데이터
- 음성 데이터
- Transcript
- DB Table
- Migration
- 테스트
- 환경 설정
- 로그
- Audit Log
- Backup
- 기존 API

삭제가 필요한 변경은 변경 이유와 영향 범위를 명시한다.

---

## 4.5 Dependency 추가 규칙

새 라이브러리를 추가하기 전에 MUST 확인:

1. 표준 라이브러리 또는 기존 Dependency로 해결 가능한가?
2. 프로젝트 유지보수 상태가 적절한가?
3. 라이선스 사용에 문제가 없는가?
4. 알려진 취약점이 없는가?
5. 불필요하게 큰 Dependency가 아닌가?

Dependency 버전은 고정 또는 재현 가능한 방식으로 관리한다.

MUST NOT:

```text
pip install some-package
```

만 수행하고 Dependency Manifest를 갱신하지 않는 행위.

---

# 5. faster-whisper 구현 규칙

## 5.1 모델 초기화

모델은 요청마다 로딩하지 않는다.

MUST:

- Worker 또는 Process 단위 Singleton에 준하는 방식으로 재사용한다.
- 모델 초기화 실패를 명확히 기록한다.
- 모델 경로와 모델 버전을 기록한다.
- GPU/CPU Device를 설정 파일에서 관리한다.

금지:

```python
@app.post("/transcribe")
def transcribe(...):
    model = WhisperModel(...)
```

권장:

```text
Application Startup
      ↓
Load Model Once
      ↓
Ready
      ↓
Request → Existing Model
```

---

## 5.2 모델 설정 외부화

다음 값은 환경변수 또는 설정파일에서 관리한다.

예:

```text
STT_MODEL_NAME
STT_MODEL_PATH
STT_DEVICE
STT_COMPUTE_TYPE
STT_LANGUAGE
STT_BEAM_SIZE
STT_VAD_ENABLED
STT_MAX_AUDIO_DURATION
STT_MAX_UPLOAD_SIZE
STT_WORKER_COUNT
```

소스 코드에 운영환경별 값을 하드코딩하지 않는다.

---

## 5.3 모델 Provenance

모든 STT 결과는 최소한 다음 정보를 추적할 수 있어야 한다.

```text
engine
model_name
model_version
model_path_hash 또는 artifact_version
compute_type
device_type
language
beam_size
vad_enabled
application_version
```

STT 결과가 어떤 모델/설정에서 생성되었는지 재현 가능해야 한다.

---

# 6. 입력 음성 보안 규칙

음성파일은 **신뢰할 수 없는 외부 입력(Untrusted Input)** 으로 취급한다.

MUST:

- 허용된 확장자만 받는다.
- 확장자만 믿지 않고 MIME/파일 Signature를 함께 확인한다.
- 최대 파일 크기를 제한한다.
- 최대 재생시간을 제한한다.
- 파일명은 서버 저장 경로로 직접 사용하지 않는다.
- 서버가 생성한 UUID 기반 파일명을 사용한다.
- Path Traversal을 방지한다.
- 임시 저장 위치를 제한한다.
- 처리 완료 후 정책에 따라 안전하게 삭제한다.

금지 입력 예:

```text
../../system/file
..\..\secret
/script.sh
audio.mp3.exe
```

---

# 7. FFmpeg / 외부 프로세스 실행 규칙

오디오 변환 과정에서 Shell Injection을 방지한다.

MUST NOT:

```python
os.system(f"ffmpeg -i {user_input} ...")
```

MUST NOT:

```python
subprocess.run(command, shell=True)
```

사용자 입력을 포함하는 Shell String 생성은 금지한다.

권장:

```python
subprocess.run(
    [
        "ffmpeg",
        "-i", safe_input_path,
        ...
    ],
    shell=False,
    check=True,
    timeout=...
)
```

추가로 MUST:

- Timeout 설정
- CPU/Memory 제한 검토
- stderr 처리
- 반환 코드 검증
- 허용된 Binary만 실행

---

# 8. 개인정보 및 정보보안 준수

## 8.1 데이터 분류

음성과 STT Transcript는 기본적으로 **민감한 사내 업무정보가 포함될 수 있는 데이터**로 취급한다.

다음 정보가 포함될 수 있다.

- 성명
- 전화번호
- 주소
- 계좌번호
- 주민등록번호
- 고객정보
- 거래정보
- 계약정보
- 내부 회의정보
- 비공개 사업정보
- 기타 개인정보 및 기밀정보

따라서 별도 판단이 없으면 **Confidential 등급**으로 처리한다.

---

## 8.2 외부 전송 기본 금지

기본 운영 모드는 Local/On-Premise 처리로 한다.

MUST NOT:

- 음성을 외부 API로 임의 전송
- Transcript를 외부 LLM로 임의 전송
- 에러 분석을 목적으로 사용자 데이터를 외부 서비스에 업로드
- 개발 편의를 위해 실제 음성 데이터를 SaaS Debugging Tool에 업로드

외부 전송이 필요한 경우 별도의 보안 승인 및 명시적 설정이 필요하다.

---

# 9. Secret 관리

MUST NOT:

```python
API_KEY = "abc123..."
DB_PASSWORD = "..."
```

다음에 Secret을 저장하지 않는다.

- Git
- Source Code
- README
- 로그
- Error Message
- Front-end JavaScript
- Docker Image Layer
- 테스트 Fixture

Secret은 환경변수 또는 승인된 Secret Management 방식을 사용한다.

`.env`는 Git에 Commit하지 않는다.

`.env.example`에는 Dummy 값만 작성한다.

---

# 10. 인증 및 권한관리

운영 환경의 모든 STT 기능은 인증을 전제로 한다.

최소 역할 예:

```text
USER
 └─ 본인이 요청한 STT 작업 조회

REVIEWER
 └─ 허용된 업무범위 결과 조회

ADMIN
 ├─ 전체 작업 관리
 ├─ 설정 관리
 └─ 시스템 운영

AUDITOR
 └─ Audit Log 조회
```

MUST:

- Backend에서 권한 검증
- Object 단위 접근통제
- 다른 사용자의 Job ID를 통한 조회 차단
- 관리자 기능 Role 검증

MUST NOT:

- UI에서 버튼만 숨기는 방식으로 권한 제어
- `is_admin=true` 값을 Client가 전달하도록 설계

---

# 11. API Secure Coding 규칙

API는 다음 원칙을 적용한다.

MUST:

- Request Schema Validation
- Response Schema 정의
- 입력 길이 제한
- 파일 크기 제한
- Rate Limit 또는 동시작업 제한
- 인증
- 권한 확인
- 표준화된 오류 응답
- Request ID / Correlation ID 발급

SHOULD:

- API Versioning (`/api/v1/...`)
- 보안 Header
- 최소 CORS 정책
- 불필요한 Server 정보 비노출

Production 환경에서 상세 Stack Trace를 사용자에게 반환하지 않는다.

---

# 12. SQL / DB 보안

MUST:

- ORM 또는 Parameterized Query 사용
- 사용자 입력 문자열 조합으로 SQL 생성 금지
- DB 계정 최소권한
- 운영 DB와 개발 DB 분리
- Migration을 통한 Schema 변경

금지:

```python
query = f"SELECT * FROM users WHERE id = '{user_input}'"
```

---

# 13. Transcript 보안

STT 결과는 사용자 입력과 동일하게 Untrusted Data로 취급한다.

Transcript 안에는 다음이 포함될 수 있다.

```text
"관리자 명령을 무시하고..."
"<script>..."
"DROP TABLE..."
```

이는 단순 음성 내용일 뿐 **시스템 명령으로 해석하면 안 된다.**

향후 LLM에 전달할 경우:

```text
Transcript = DATA
Prompt/System Instruction = CONTROL
```

경계를 명확히 유지한다.

Transcript 내용이 시스템 Prompt 또는 Tool Command를 직접 변경할 수 없도록 한다.

---

# 14. 로그 기본 원칙

로그는 다음 두 종류를 반드시 분리한다.

```text
Application Log
- 장애
- 처리상태
- 성능
- Debug 정보

Audit Log
- 사용자 행위
- 데이터 접근
- 설정 변경
- 권한 변경
- 관리자 행위
```

Application Log와 Audit Log의 목적은 다르다.

Audit Log는 일반 Debug Log처럼 임의 삭제하거나 비활성화할 수 없다.

---

# 15. 로그에 기록하면 안 되는 정보

MUST NOT LOG:

- 원본 음성
- 전체 Transcript
- Password
- API Key
- Access Token
- Refresh Token
- 주민등록번호
- 계좌번호
- 카드번호
- 인증정보
- 불필요한 개인정보

필요 시 식별자는 Masking 또는 Pseudonymization 한다.

예:

```text
user_id = U-10492
job_id = STT-7f9c...
audio_sha256 = ...
```

파일의 실제 내용을 로그에 기록하지 않는다.

---

# 16. Application Logging 표준

모든 로그는 Structured Logging을 권장한다.

예:

```json
{
  "timestamp": "2026-09-01T16:20:31+09:00",
  "level": "INFO",
  "service": "stt-api",
  "environment": "prod",
  "request_id": "req-...",
  "job_id": "stt-...",
  "event": "STT_JOB_COMPLETED",
  "duration_ms": 25123
}
```

필수 공통 필드:

```text
timestamp
level
service
environment
event
request_id 또는 correlation_id
```

가능한 경우:

```text
user_id
job_id
client_ip_masked
application_version
worker_id
duration_ms
```

---

# 17. Audit Log 필수 이벤트

다음 이벤트는 MUST Audit Log로 남긴다.

## 인증

```text
LOGIN_SUCCESS
LOGIN_FAILED
LOGOUT
ACCESS_DENIED
```

## STT 처리

```text
STT_JOB_CREATED
AUDIO_UPLOADED
STT_JOB_STARTED
STT_JOB_COMPLETED
STT_JOB_FAILED
TRANSCRIPT_VIEWED
TRANSCRIPT_DOWNLOADED
AUDIO_DOWNLOADED
AUDIO_DELETED
TRANSCRIPT_DELETED
```

## 관리자

```text
USER_ROLE_CHANGED
SYSTEM_CONFIG_CHANGED
MODEL_CONFIG_CHANGED
MODEL_CHANGED
RETENTION_POLICY_CHANGED
AUDIT_LOG_EXPORTED
```

---

# 18. Audit Log 필수 필드

Audit Event는 최소 다음 정보를 포함한다.

```text
event_time
event_type
actor_id
actor_role
action
target_type
target_id
result
request_id
source_ip 또는 비식별화된 source 정보
application_version
```

STT 처리 이벤트에는 추가로:

```text
job_id
audio_sha256
model_name
model_version
stt_config_version
```

을 기록한다.

---

# 19. Audit Log 무결성

Audit Log는 임의 변조를 어렵게 해야 한다.

MUST:

- 일반 사용자에게 수정 권한을 주지 않는다.
- 애플리케이션 운영자도 원칙적으로 UPDATE/DELETE하지 않는다.
- Append-only 구조를 우선한다.
- 접근권한을 별도로 관리한다.
- Audit Log 조회/Export 행위 자체도 감사대상으로 기록한다.

SHOULD:

- 중앙 로그 시스템 전송
- 별도 저장소 사용
- 무결성 검증 Hash/Signature
- 장기 보관 정책 적용

---

# 20. STT 재현성 / 품질 Audit

STT 결과는 다음 입력 조합을 추적할 수 있어야 한다.

```text
Input Audio Hash
+
Application Version
+
Model Version
+
Model Configuration
+
Preprocessing Version
+
Normalization Version
=
Transcript Result
```

이를 위해 Job Metadata에 다음 정보를 저장한다.

```json
{
  "audio_sha256": "...",
  "engine": "faster-whisper",
  "model": "...",
  "model_version": "...",
  "language": "ko",
  "beam_size": 5,
  "vad_filter": true,
  "compute_type": "...",
  "preprocessor_version": "...",
  "application_version": "..."
}
```

---

# 21. 음성 원본과 Transcript 보관 정책

보관기간은 Configurable 해야 한다.

예:

```text
AUDIO_RETENTION_DAYS
TRANSCRIPT_RETENTION_DAYS
AUDIT_RETENTION_DAYS
```

MUST:

- 보관기간 만료 후 정책에 따라 삭제
- 삭제 성공/실패 기록
- 사용자 요청 삭제와 자동 삭제 구분
- Audit Log는 업무데이터 삭제와 별도로 관리

MUST NOT:

- 코드에 `30일` 등 특정 기간 하드코딩
- 임시파일 무기한 보관

---

# 22. 임시파일 처리

오디오 변환 과정의 임시파일은 지정된 Temp Directory만 사용한다.

MUST:

```text
Create
  ↓
Process
  ↓
Result Persist
  ↓
Cleanup
```

정상 처리뿐 아니라 예외 발생 시에도 Cleanup 해야 한다.

권장:

```python
try:
    ...
finally:
    cleanup_temp_files()
```

단, 삭제 실패는 무시하지 않고 로그로 남긴다.

---

# 23. 오류 처리 분류

오류는 최소 다음으로 분류한다.

```text
VALIDATION_ERROR
AUTHENTICATION_ERROR
AUTHORIZATION_ERROR
FILE_FORMAT_ERROR
FILE_TOO_LARGE
AUDIO_TOO_LONG
AUDIO_DECODE_ERROR
STT_MODEL_ERROR
GPU_RESOURCE_ERROR
QUEUE_ERROR
DATABASE_ERROR
STORAGE_ERROR
INTERNAL_ERROR
```

사용자에게는 내부 구현 정보를 노출하지 않는다.

예:

```json
{
  "code": "AUDIO_DECODE_ERROR",
  "message": "음성 파일을 처리할 수 없습니다.",
  "request_id": "req-..."
}
```

---

# 24. GPU / Resource 보호

음성파일은 시스템 자원을 대량 소비할 수 있으므로 Resource Exhaustion 공격을 고려한다.

MUST:

- 최대 업로드 파일 크기 제한
- 최대 재생시간 제한
- Worker 동시 실행 수 제한
- Queue 최대 길이 설정
- 작업 Timeout
- 실패 Job Retry 횟수 제한

SHOULD:

- GPU Memory Monitoring
- CPU Monitoring
- Disk 사용량 Monitoring
- Queue Depth Monitoring
- 처리속도 Monitoring

무제한 Parallel STT 실행은 금지한다.

---

# 25. Job 기반 처리

긴 음성은 동기 HTTP 요청 하나에서 끝까지 처리하지 않는 것을 원칙으로 한다.

권장 API:

```text
POST /api/v1/stt/jobs
        ↓
     job_id

GET /api/v1/stt/jobs/{job_id}
        ↓
QUEUED / PROCESSING / COMPLETED / FAILED

GET /api/v1/stt/jobs/{job_id}/transcript
```

Job 상태는 명확한 State Machine을 사용한다.

```text
CREATED
  ↓
QUEUED
  ↓
PROCESSING
  ├─→ COMPLETED
  └─→ FAILED
```

필요 시:

```text
CANCELLED
EXPIRED
```

---

# 26. Idempotency

동일 요청이 네트워크 재시도로 반복될 수 있다.

업로드/Job 생성 API에는 필요 시 Idempotency Key를 도입한다.

동일 Job이 중복 처리되어:

- GPU 자원을 이중 사용하거나
- Transcript가 중복 저장되거나
- Audit Event가 왜곡되는 것

을 방지한다.

---

# 27. 테스트 기본 원칙

기능 구현 시 테스트 없이 완료 처리하지 않는다.

최소 테스트 계층:

```text
Unit Test
Integration Test
API Test
Security Test
Regression Test
Golden Audio STT Test
```

---

# 28. Golden Dataset

STT 품질을 지속적으로 검증하기 위한 Golden Dataset을 별도로 유지한다.

Golden Dataset은 가능한 한 다음 유형을 포함한다.

- 일반 대화
- 회의
- 전화 음질
- 빠른 발화
- 작은 목소리
- 배경 소음
- 숫자
- 날짜
- 금액
- 영문 혼용
- 업무 전문용어
- 복수 화자
- 긴 음성

실제 개인정보가 포함된 데이터는 별도 승인 없이 테스트 Repository에 넣지 않는다.

가능하면 비식별/합성 데이터 사용을 우선한다.

---

# 29. STT Regression Gate

모델 또는 설정 변경 시 기존 Golden Dataset을 다시 평가한다.

비교 예:

```text
Before
WER                8.2%
금융용어 정확도      93.1%
숫자 정확도          96.0%

After
WER                7.9%
금융용어 정확도      94.0%
숫자 정확도          92.4%  ← Regression
```

전체 WER가 개선되더라도 특정 핵심 지표가 악화될 수 있다.

따라서 WER 하나만 Release 기준으로 사용하지 않는다.

---

# 30. 성능 측정

최소 다음 지표를 수집한다.

```text
audio_duration_seconds
processing_duration_seconds
real_time_factor
queue_wait_seconds
GPU memory
CPU usage
failure_rate
retry_rate
```

주요 파생지표:

```text
RTF = STT 처리시간 / 음성 재생시간
```

예:

```text
60분 음성
처리시간 6분
RTF = 0.1
```

---

# 31. Monitoring 항목

운영 Dashboard에서 SHOULD 확인 가능:

```text
STT 요청 건수
성공률
실패율
평균 처리시간
P95 처리시간
Queue 길이
평균 Queue 대기시간
GPU 사용률
GPU Memory
Disk 사용률
Model Load 상태
Worker 상태
```

---

# 32. Security Event Monitoring

다음 이벤트는 보안 모니터링 대상으로 취급한다.

```text
반복 로그인 실패
권한 없는 Transcript 접근
대량 다운로드
대량 삭제
지나치게 큰 파일 업로드 반복
비정상 API 호출
관리자 권한 변경
모델 설정 변경
Audit Log Export
```

필요 시 Alert를 발생시킨다.

---

# 33. Secure Coding 기본 Checklist

모든 변경은 최소한 다음을 확인한다.

## Input

- [ ] 입력값 Schema Validation
- [ ] 파일 크기 제한
- [ ] 파일 타입 검증
- [ ] Path Traversal 방지
- [ ] Command Injection 방지

## Authentication / Authorization

- [ ] 인증 적용
- [ ] Backend 권한검사
- [ ] Object 단위 접근통제
- [ ] 관리자 API 보호

## Data

- [ ] Sensitive Data 로그 미기록
- [ ] Secret 하드코딩 없음
- [ ] 필요 시 저장 데이터 암호화
- [ ] 데이터 보관기간 적용

## API

- [ ] Error 정보 최소화
- [ ] Rate/Concurrency 제한
- [ ] CORS 최소화
- [ ] Request ID 지원

## DB

- [ ] Parameterized Query
- [ ] 최소권한 DB 계정
- [ ] Migration 관리

## Process

- [ ] `shell=True` 미사용
- [ ] Timeout 설정
- [ ] 임시파일 정리
- [ ] Resource Limit

---

# 34. 운영환경 분리

환경은 최소 다음으로 분리한다.

```text
local
dev
test
prod
```

Production 데이터는 개발환경으로 복사하지 않는다.

환경별 설정은 코드가 아니라 Config로 관리한다.

---

# 35. Production Debug 금지

Production에서 다음을 금지한다.

```text
DEBUG=True
상세 Stack Trace 외부노출
SQL Debug Logging
전체 Request Body Logging
Transcript Logging
Secret Logging
```

장애 분석은 Request ID / Job ID 기반으로 수행한다.

---

# 36. 모델 변경 관리

다음은 단순 설정변경이 아니라 **Release 영향 변경**으로 취급한다.

- Whisper Model 변경
- Quantization 변경
- compute_type 변경
- beam_size 변경
- VAD 설정 변경
- 음성 전처리 변경
- Normalization 변경

변경 시 MUST:

1. 변경 이유 기록
2. Golden Dataset Regression Test
3. 성능 비교
4. Release Note 작성
5. Version 증가
6. Rollback 방법 확보

---

# 37. Configuration 변경 감사

운영 중 다음 설정 변경은 Audit 대상이다.

```text
STT_MODEL
LANGUAGE
BEAM_SIZE
VAD
MAX_AUDIO_DURATION
MAX_UPLOAD_SIZE
WORKER_COUNT
RETENTION_POLICY
```

변경 시 기록:

```text
who
when
previous_value
new_value
reason
```

Secret 값 자체는 Audit Log에 기록하지 않는다.

---

# 38. Deployment 규칙

Release 전 MUST:

```text
Unit Tests PASS
Integration Tests PASS
Security Tests PASS
Golden Regression PASS
Dependency Scan PASS
Configuration Validation PASS
Migration Validation PASS
```

배포는 Version으로 식별 가능해야 한다.

예:

```text
application_version = 1.4.2
git_commit = abcd1234
build_id = ...
```

---

# 39. Rollback

모든 Production 배포는 Rollback 가능해야 한다.

MUST:

- 이전 Application Version 확인 가능
- 이전 설정 복구 가능
- DB Migration Rollback 영향 검토
- 이전 모델 Artifact 유지 또는 재배포 가능

모델 변경과 코드 변경을 가능하면 독립적으로 Rollback 가능하게 한다.

---

# 40. Dependency / Supply Chain 보안

MUST:

- Dependency Manifest 관리
- 버전 관리
- 불필요한 Package 제거
- 알려진 취약점 점검
- 출처가 불명확한 Package 금지

SHOULD:

- SBOM 생성
- Container Image Scan
- Dependency Vulnerability Scan
- Hash 기반 Artifact 검증

---

# 41. Container 보안

Docker를 사용하는 경우:

MUST:

- Root 실행을 기본값으로 하지 않는다.
- 최소 Base Image 사용
- Secret을 Image에 포함하지 않는다.
- 불필요한 Port를 열지 않는다.
- Volume 권한 최소화
- Health Check 제공

GPU Container에도 동일 원칙을 적용한다.

---

# 42. Network 기본 정책

STT Service는 기본적으로 **외부 Internet 연결이 없어도 동작**할 수 있도록 설계하는 것을 권장한다.

MUST:

- 불필요한 Outbound Network Call 금지
- 외부 연결 대상 Allowlist 관리
- 내부 DB와 API도 최소권한 적용

모델 다운로드는 운영 중 동적으로 수행하기보다 승인된 Artifact를 사전 반입하는 방식을 우선한다.

---

# 43. 데이터 다운로드 통제

Transcript 또는 음성 다운로드 기능은 단순 조회와 별개의 권한으로 관리할 수 있어야 한다.

Audit 대상:

```text
TRANSCRIPT_DOWNLOADED
AUDIO_DOWNLOADED
BULK_EXPORT_STARTED
BULK_EXPORT_COMPLETED
```

대량 Export는 추가 권한 또는 승인 Workflow를 고려한다.

---

# 44. API Response 최소화

API는 요청한 정보만 반환한다.

MUST NOT:

- 내부 파일 경로 노출
- DB Primary Key 불필요 노출
- 서버 Hostname 노출
- Stack Trace 노출
- GPU 상세정보 불필요 노출
- Secret/Configuration 노출

---

# 45. 파일 Hash

업로드된 음성은 SHA-256 등 안전한 Hash를 계산해 식별 가능하게 한다.

용도:

```text
중복 탐지
Audit
재현성
무결성 확인
```

Hash 값은 파일 내용 자체가 아니므로 로그에 사용할 수 있지만 내부 정책에 따라 접근통제한다.

---

# 46. 개인정보 최소수집

STT 처리 목적에 필요하지 않은 사용자 정보는 저장하지 않는다.

예:

STT 요청에 필요하지 않다면 다음을 저장하지 않는다.

```text
사용자 주민번호
개인 전화번호
개인 주소
불필요한 조직정보
```

User ID 등 감사에 필요한 최소 식별자만 사용한다.

---

# 47. 관리자 화면

관리자 화면은 일반 사용자 화면과 명확히 권한을 분리한다.

관리 가능 항목 예:

```text
Job 상태
Worker 상태
Model Version
Queue 상태
시스템 설정
Retention Policy
사용자 권한
Audit 조회
```

MUST:

- 모든 관리자 변경 Audit
- 재인증 또는 강한 인증 검토
- 대량 삭제/설정 변경 Confirmation

---

# 48. 삭제 안전장치

대량 삭제 또는 운영 데이터 삭제 기능은 별도의 Safety Guard를 둔다.

예:

```text
1. 권한확인
2. 대상 건수 표시
3. 재확인
4. 실행
5. Audit 기록
```

AI Coding Agent가 테스트 목적으로 Production 데이터를 삭제하는 로직을 작성해서는 안 된다.

---

# 49. 개인정보/기밀정보가 포함된 Debug 대응

운영 장애를 해결하기 위해 실제 음성/Transcript가 필요한 경우에도 이를 개발자의 개인 PC로 복사하는 것을 기본 방법으로 사용하지 않는다.

우선순위:

```text
Metadata
→ Application Log
→ Error Code
→ Job Metadata
→ 비식별 Sample
→ 승인된 제한적 원본 접근
```

원본 접근은 최소화하고 필요 시 감사기록을 남긴다.

---

# 50. 향후 LLM 후처리 연계 규칙

LLM 연계는 STT와 분리한다.

```text
Audio
 ↓
STT
 ↓
Raw Transcript
 ↓
Normalization
 ↓
[Optional LLM Processor]
 ↓
Summary / Structured Data
```

MUST:

- Raw Transcript 보존 여부를 정책으로 결정
- LLM Output과 STT Raw Output 구분
- LLM 수정 내역 추적 가능
- LLM 결과를 원본 STT 결과로 덮어쓰지 않음

예:

```text
raw_transcript
normalized_transcript
llm_corrected_transcript
```

을 분리할 수 있다.

---

# 51. 데이터 Lineage

가능한 경우 다음 계보를 추적한다.

```text
Audio
  ↓
Raw Transcript
  ↓
Normalized Transcript
  ↓
LLM Corrected Transcript
  ↓
Summary
```

각 단계에:

```text
created_at
processor
processor_version
input_version
```

을 기록한다.

---

# 52. 소스코드 품질 규칙

MUST:

- 함수는 하나의 책임 중심으로 작성
- 명확한 변수명
- Type Hint 사용 권장
- Public Interface에 Docstring
- 중복코드 최소화
- Magic Number Config화
- Circular Dependency 방지

AI가 생성한 코드라고 해서 품질 기준을 낮추지 않는다.

---

# 53. 주석 규칙

주석은 코드가 무엇을 하는지 반복하지 말고 **왜 그렇게 구현했는지** 설명한다.

Bad:

```python
# 파일을 연다
file = open(...)
```

Good:

```python
# 사용자 파일명을 그대로 사용하면 path traversal 위험이 있으므로
# 서버가 생성한 UUID 경로만 사용한다.
```

---

# 54. Feature Flag

위험도가 높거나 점진 배포가 필요한 신규 기능은 Feature Flag 사용을 고려한다.

예:

```text
ENABLE_NEW_VAD
ENABLE_LLM_CORRECTION
ENABLE_DIARIZATION
```

기능 실패 시 전체 STT를 중단시키지 않고 안전하게 비활성화 가능해야 한다.

---

# 55. 장애 격리

후처리 기능의 장애가 STT 원본 생성 자체를 가능한 한 방해하지 않도록 설계한다.

예:

```text
STT 성공
  ↓
LLM Summary 실패
```

인 경우:

```text
STT_RESULT = COMPLETED
SUMMARY = FAILED
```

로 개별 상태를 관리할 수 있어야 한다.

---

# 56. Health Check

운영 서비스는 Health Endpoint를 제공한다.

예:

```text
/live
/ready
```

구분:

```text
Liveness  = Process가 살아 있는가?
Readiness = 실제 STT 요청을 받을 준비가 되었는가?
```

Readiness에는 모델 Load 상태를 고려한다.

---

# 57. 개인정보가 포함된 테스트 방지

MUST NOT:

- 실제 고객 음성을 Git에 Commit
- 실제 고객 Transcript를 Fixture로 Commit
- 운영 DB Dump를 Test Data로 사용

테스트 데이터는 합성 또는 비식별 데이터를 기본으로 한다.

---

# 58. Pull Request / 변경 단위 규칙

한 변경은 하나의 목적을 갖는다.

PR 또는 변경 설명에는 최소 다음을 기록한다.

```text
목적
변경사항
보안영향
데이터영향
테스트결과
Rollback 방법
```

AI Coding Agent도 작업 종료 시 동일 내용을 요약한다.

---

# 59. 변경 위험도 분류

변경은 다음으로 분류할 수 있다.

## LOW

```text
UI 문구
내부 Refactoring
테스트 추가
```

## MEDIUM

```text
API 변경
STT 옵션 변경
Queue 변경
Logging 변경
```

## HIGH

```text
인증/권한
개인정보 처리
Audit Log
DB Migration
모델 변경
데이터 삭제
외부 API 연결
Secret 관리
```

HIGH 변경은 추가 검증 없이 자동 배포하지 않는다.

---

# 60. Definition of Done

기능은 다음 조건을 만족해야 완료로 간주한다.

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

# 61. AI Coding Agent 금지 행동

AI Coding Agent는 MUST NOT:

1. 테스트 실패를 숨긴다.
2. 테스트 자체를 삭제하여 통과시킨다.
3. 인증 로직을 임시로 제거한다.
4. 보안 검증을 주석처리한다.
5. Production Secret을 코드에 넣는다.
6. 사용자의 실제 음성/Transcript를 로그에 남긴다.
7. Production DB를 임의 초기화한다.
8. DB Migration 없이 Schema를 변경한다.
9. `shell=True`로 사용자 입력을 실행한다.
10. 임의 외부 API로 데이터를 전송한다.
11. 출처가 불분명한 Dependency를 설치한다.
12. 전체 프로젝트를 이유 없이 재작성한다.
13. 요청과 관계없는 파일을 대량 변경한다.
14. 장애를 숨기기 위해 `except Exception: pass`를 사용한다.
15. 보안 또는 Audit 기능을 성능상의 이유만으로 제거한다.
16. 원본 STT 결과를 후처리 결과로 덮어쓴다.
17. 명시적 승인 없이 데이터 삭제 코드를 실행한다.
18. Debug 목적으로 개인정보를 노출한다.

---

# 62. AI Coding Agent 작업 종료 보고

작업 완료 시 Agent는 최소 다음 형태로 보고해야 한다.

```text
## 변경 요약
- ...

## 변경 파일
- ...

## 테스트
- Unit:
- Integration:
- Regression:

## 보안 영향
- ...

## 데이터 영향
- ...

## Audit/Logging 영향
- ...

## 알려진 제한사항
- ...

## Rollback
- ...
```

---

# 63. 권장 Configuration 예시

```env
APP_ENV=dev

STT_ENGINE=faster-whisper
STT_MODEL_NAME=...
STT_MODEL_PATH=/models/...
STT_DEVICE=cuda
STT_COMPUTE_TYPE=...
STT_LANGUAGE=ko
STT_BEAM_SIZE=5
STT_VAD_ENABLED=true

STT_MAX_UPLOAD_MB=500
STT_MAX_AUDIO_MINUTES=180
STT_MAX_CONCURRENT_JOBS=2
STT_JOB_TIMEOUT_SECONDS=3600

AUDIO_RETENTION_DAYS=...
TRANSCRIPT_RETENTION_DAYS=...
AUDIT_RETENTION_DAYS=...

LOG_LEVEL=INFO
```

실제 값은 운영 정책과 인프라 용량을 기준으로 결정한다.

---

# 64. 최소 Audit Schema 예시

```sql
audit_event
-----------
id
event_time
event_type
actor_id
actor_role
action
target_type
target_id
result
request_id
job_id
application_version
model_version
metadata_json
```

`metadata_json`에는 개인정보와 Secret을 저장하지 않는다.

---

# 65. 최소 STT Job Metadata 예시

```json
{
  "job_id": "stt-...",
  "created_by": "U-...",
  "created_at": "...",
  "status": "COMPLETED",

  "audio_sha256": "...",
  "audio_duration_seconds": 1234,

  "engine": "faster-whisper",
  "model_name": "...",
  "model_version": "...",
  "language": "ko",
  "beam_size": 5,
  "vad_enabled": true,
  "compute_type": "...",

  "application_version": "...",

  "started_at": "...",
  "completed_at": "...",
  "processing_duration_seconds": 91.2
}
```

---

# 66. 운영 핵심 원칙 요약

본 프로젝트의 핵심 원칙은 다음 10개이다.

1. **STT 엔진과 업무서비스를 분리한다.**
2. **모든 음성과 Transcript를 민감정보로 간주한다.**
3. **기본적으로 외부 전송하지 않는다.**
4. **사용자 입력은 항상 신뢰하지 않는다.**
5. **모든 데이터 접근은 인증·권한검사를 거친다.**
6. **민감정보를 로그에 기록하지 않는다.**
7. **중요 행위는 Audit Log로 추적한다.**
8. **STT 결과는 모델·설정·코드 버전까지 재현 가능해야 한다.**
9. **AI Coding Agent는 최소 변경·테스트 우선 원칙을 따른다.**
10. **빠른 개발보다 보안·무결성·감사 가능성을 우선한다.**

---

# 67. Secure Development Reference

본 Harness는 다음과 같은 일반적인 Secure Software Development 원칙을 참고해 운용한다.

- NIST Secure Software Development Framework (SSDF)
- OWASP Application Security Verification Standard (ASVS)
- OWASP Logging Guidance
- 조직 내부 정보보안 정책
- 조직 내부 개인정보보호 정책
- 조직 내부 개발·변경·배포 관리 기준

외부 표준과 내부 정책이 충돌하는 경우 **내부 정책 중 더 엄격한 기준을 우선 적용**한다.

---

# 68. 최종 원칙

> **AI Coding Agent가 코드를 빠르게 생성하는 것은 품질·보안·감사 책임을 제거하지 않는다.**

이 프로젝트에서 생성되는 모든 코드와 설정은 사람이 직접 작성한 코드와 동일한 품질 기준을 적용한다.

특히 STT 시스템은 음성이라는 원천 데이터를 통해 개인정보, 고객정보, 내부 회의, 거래정보 및 기타 기밀정보를 처리할 가능성이 있으므로 **Security by Default, Privacy by Default, Audit by Default**를 기본 설계 원칙으로 한다.

---

## Change History

| Version | Date | Description |
|---|---|---|
| 1.0 | 2026-09-01 | Initial Harness Engineering Baseline |

