# STT Service — 운영 문서 (Operations)

> Version: 1.0 · Date: 2026-09-02
> 상위 계약: [`STT_HARNESS_RULES.md`](../STT_HARNESS_RULES.md) · [`REQUIREMENTS.md`](REQUIREMENTS.md)

이 문서는 서비스를 세우고, 돌리고, 문제를 진단하는 사람을 위한 것이다. 설계 근거는
요구사항 정의서에 있고 여기서는 반복하지 않는다.

---

## 1. 구성

프로세스는 넷이다. 역할이 다르므로 자원 요구도 다르다.

| 프로세스 | 역할 | 특징 |
|---|---|---|
| `api` | HTTP 요청 처리, 화면 제공 | STT 모델을 적재하지 않는다 |
| `worker` | 큐에서 Job 을 꺼내 전사 | 모델을 적재한다. 메모리 대부분을 여기서 쓴다 |
| `db` | PostgreSQL. Job 메타데이터와 감사 로그 | 단일 진실 |
| `redis` | Celery 브로커 | 유실되어도 Job 상태는 DB 에 남는다 |

**API 는 워커 코드를 임포트하지 않는다.** Celery 태스크를 이름(`app.jobs.worker.run_stt_job`)
으로만 호출하기 때문이다. 덕분에 API 프로세스에 모델 라이브러리가 적재되지 않는다.

음성과 Transcript 는 파일 저장소(`STORAGE_ROOT`)에 있고 DB 에는 상대경로만 있다.
**API 와 워커가 같은 저장소를 공유해야 한다** — compose 에서는 `sttdata` 볼륨이,
여러 호스트로 나눌 때는 공유 파일시스템이 그 역할을 한다.

---

## 2. 최초 구축

### 2.1 사전 준비

```bash
cp .env.example .env
```

`.env` 에서 최소한 다음을 채운다.

| 키 | 채우는 법 |
|---|---|
| `SESSION_SECRET` | `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `POSTGRES_PASSWORD` | 스키마 소유자 비밀번호 |
| `STT_APP_DB_PASSWORD` | 런타임 계정 비밀번호 (소유자와 **다르게**) |
| `BOOTSTRAP_ADMIN_USERNAME` / `_PASSWORD` | 초기 관리자. 생성 후 **반드시 비운다** |

`.env` 는 커밋 대상이 아니다 (SEC-023). prod 환경에 `BOOTSTRAP_ADMIN_PASSWORD` 가
남아 있으면 애플리케이션이 기동을 거부한다.

### 2.2 기동 순서

```bash
docker compose up -d db redis          # 첫 기동 시 런타임 DB 계정이 생성된다
docker compose run --rm migrate        # 스키마 소유자 계정으로 마이그레이션
docker compose exec -T db \
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
       -v app_user="$STT_APP_DB_USER" -f /opt/grant_least_privilege.sql
docker compose up -d api worker
docker compose exec api python -m scripts.bootstrap_admin
```

**권한 부여는 마이그레이션 뒤에 온다.** DB 초기화 훅이 도는 시점에는 아직 테이블이
없어 테이블 단위 권한을 줄 수 없다.

초기 관리자를 만든 뒤 `.env` 의 `BOOTSTRAP_ADMIN_*` 를 비우고 `api` 를 재시작한다.

### 2.3 확인

```bash
curl -s localhost:8000/ready
# {"status":"ready","database":true,"queue":true,"model_loaded":false}
```

`model_loaded: false` 는 정상이다. 모델은 워커가 첫 Job 에서 적재하며, API 프로세스는
적재하지 않는다.

---

## 3. 메모리 산정

`STT_ENGINE=mock` 으로 측정한 실측값(2026-09-02, 이 저장소 compose 스택):

| 프로세스 | 측정값 |
|---|---|
| `api` | 188 MB |
| `worker` (모델 미적재) | 170 MB |
| `db` | 49 MB |
| `redis` | 7 MB |
| **합계 (모델 제외)** | **약 415 MB** |

여기에 워커가 적재하는 **모델이 더해진다**. faster-whisper `int8` 기준 대략:

| 모델 | 가중치 | 워커 1개 실사용(추정) |
|---|---|---|
| `tiny` | ~40 MB | ~250 MB |
| `base` | ~75 MB | ~300 MB |
| `small` | ~250 MB | ~500 MB |
| `medium` | ~1.5 GB | ~1.9 GB |
| `large-v3` | ~3.1 GB | ~3.6 GB |

> 가중치 크기는 아티팩트 실측이지만 **실사용 추정치는 이 저장소에서 측정하지 않았다.**
> 디코딩 버퍼가 음성 길이에 비례해 늘기 때문에, 실제 배포 전 대표 길이의 음성으로
> 측정해 이 표를 갱신할 것.

**동시 실행 수만큼 곱해진다.** `STT_MAX_CONCURRENT_JOBS=2` 에 `medium` 이면 워커
프로세스만 약 3.8 GB 다.

### 이 개발 머신의 제약

총 7.8 GB / 가용 약 3.2 GB 다. `medium` + 동시 2 + 전체 스택이면 가용 메모리를
넘어 스왑이 발생하고 RTF 가 급격히 나빠진다. 개발 중에는 다음 중 하나를 택한다.

```bash
STT_MODEL_NAME=small        # 모델을 낮춘다
STT_MAX_CONCURRENT_JOBS=1   # 동시 실행을 줄인다
STT_ENGINE=mock             # 모델 없이 전 경로를 검증한다
```

세 값 모두 설정이므로 코드 변경 없이 바꿀 수 있다.

---

## 4. 모델 반입 (폐쇄망)

이미지에 모델을 굽지 않는다. 빌드가 네트워크에 의존하면 폐쇄망 재현이 불가능해진다.

```bash
# 인터넷 되는 곳에서 내려받아
huggingface-cli download Systran/faster-whisper-medium --local-dir ./models/medium

# 운영 호스트로 옮긴 뒤
STT_MODEL_DIR=./models
STT_MODEL_PATH=/models/medium
STT_ALLOW_MODEL_DOWNLOAD=false
```

`STT_ALLOW_MODEL_DOWNLOAD=false` 인데 아티팩트가 없으면 워커가 **조용히 넘어가지 않고**
`STT_MODEL_ERROR` 로 실패한다.

모델 가중치의 SHA-256 은 Job 메타데이터(`model_artifact_hash`)에 기록된다. 같은
이름의 모델이라도 아티팩트가 바뀌면 값이 달라지므로 결과 차이의 원인을 추적할 수 있다.

---

## 5. 일상 운영

### 5.1 상태 확인

| 경로 | 용도 |
|---|---|
| `GET /live` | 프로세스 생존. 컨테이너 헬스체크용 |
| `GET /ready` | DB·큐 연결. 준비 안 되면 503 |
| `GET /api/v1/admin/status` | Job 분포, 큐 길이, 모델 상태, 한도 (ADMIN) |
| `GET /api/v1/admin/audit/integrity` | 감사 해시 체인 검증 (ADMIN/AUDITOR) |

`/live` 를 컨테이너 헬스체크에 쓰고 `/ready` 는 쓰지 않는다. `/ready` 로 재시작을
판단하면 DB 가 잠깐 흔들릴 때 멀쩡한 프로세스가 죽는다.

### 5.2 로그

구조화 JSON 이 stdout 으로 나간다. 상관관계는 `request_id` 와 `job_id` 로 잡는다.

```bash
docker compose logs api | jq 'select(.job_id=="stt-...")'
docker compose logs -f worker | jq 'select(.level=="ERROR")'
```

민감 필드는 출력 직전에 자동 마스킹된다. **Transcript 본문과 비밀번호는 어떤 로그에도
남지 않는다** — 개별 호출부의 주의력이 아니라 포매터가 보장한다.

### 5.3 보관정책

`JobService.purge_expired_audio()` 가 만료된 음성을 지운다. **주기 실행 경로는 아직
없다** (§8 참조). 당장은 수동으로 돌린다.

```bash
docker compose exec api python -c "
from app.core.config import get_settings
from app.storage.database import init_engine, session_scope
from app.storage.audio import AudioStore
from app.storage.transcript import TranscriptStore
from app.jobs.queue import create_queue
from app.jobs.service import JobService
s = get_settings(); init_engine(s)
with session_scope() as session:
    n = JobService(session, settings=s, audio_store=AudioStore(s),
                   transcript_store=TranscriptStore(s), queue=create_queue(s)).purge_expired_audio()
    print(f'{n}건 삭제')
"
```

사용자 삭제(`AUDIO_DELETED`)와 보관정책 삭제(`RETENTION_AUDIO_PURGED`)는 감사에서
구분된다.

### 5.4 백업

| 대상 | 방법 | 주의 |
|---|---|---|
| DB | `pg_dump` | 감사 로그 포함. Confidential 로 취급 |
| 음성·Transcript | `sttdata` 볼륨 | Confidential. 암호화 저장 권장 |
| `.env` | 별도 Secret 관리 | 백업본에 평문으로 두지 않는다 |

**운영 DB 덤프를 테스트 데이터로 쓰지 않는다** (SEC-040).

---

## 6. Release 영향 변경 (Harness §36)

다음을 바꾸면 STT 결과가 달라질 수 있다. 변경 시 **버전 증가 + Golden 회귀 재실행 +
성능 비교 + Release Note + Rollback 방법**을 남긴다.

| 대상 | 위치 |
|---|---|
| 모델 / `compute_type` / `beam_size` / VAD | 설정 (`stt_config_version` 에 반영) |
| 전처리 규칙 | `app/stt/preprocessing.py` → `PREPROCESSOR_VERSION` |
| 정규화 규칙 | `app/stt/normalization.py` → `NORMALIZER_VERSION` |
| 디코딩 파라미터 | `app/stt/faster_whisper_engine.py` → `ENGINE_DECODE_VERSION` |
| 감사 해시 필드/표현 | `app/audit/service.py` → 재계산 마이그레이션 필요 |

---

## 7. 문제 진단

| 증상 | 확인 |
|---|---|
| Job 이 `QUEUED` 에서 멈춤 | 워커 생존 (`docker compose ps worker`), 큐 연결 (`/ready`), 태스크 이름 일치 |
| `QUEUE_ERROR` 로 업로드 거부 | 대기 Job 수가 `STT_QUEUE_MAX_LENGTH` 초과. 처리량 또는 상한 조정 |
| `STT_MODEL_ERROR` | 아티팩트 경로(`STT_MODEL_PATH`) 확인. `STT_ALLOW_MODEL_DOWNLOAD` 확인 |
| `AUDIO_DECODE_ERROR` | 입력 파일 문제. 재시도해도 같으므로 자동 재시도 대상이 아니다 |
| 기동 즉시 종료 | 설정 검증 실패다. 로그의 `ConfigurationError` 메시지가 원인을 명시한다 |
| RTF 급등 | 스왑 여부 확인 (§3). 동시 실행 수를 줄인다 |

오류 응답의 `request_id` 로 로그를 찾는다. 응답에는 내부 원인이 담기지 않는다 —
전부 로그에 있다.

---

## 8. 알려진 제약

정직하게 적어 둔다. 운영 판단에 직접 영향을 준다.

1. **Rate Limit 이 프로세스 메모리 기반이다.** API 프로세스를 여러 개 띄우면 실효
   한도가 프로세스 수만큼 커진다. 정확한 한도가 필요하면 Redis 기반 구현으로 교체한다
   (`app/api/dependencies/ratelimit.py`, 인터페이스는 그대로).

2. **감사 해시 체인은 동시 삽입에 취약하다.** 두 트랜잭션이 같은 직전 해시를 읽으면
   체인이 갈라져 `audit/integrity` 가 위반으로 보고한다. 순차 기록에서는 검증되며
   (API·워커 두 프로세스 환경에서 확인), 고부하에서는 직렬화 또는 체인 재설계가 필요하다.

3. **감사 로그 보관기간 삭제 경로가 없다.** `AUDIT_RETENTION_DAYS` 설정은 있지만
   append-only 트리거와 권한이 삭제를 막는다. 의도된 것이며, 삭제가 필요하면 DBA 가
   트리거를 일시 해제하고 그 사실을 별도로 기록해야 한다.

4. **보관정책 자동 실행 스케줄러가 없다.** §5.3 의 수동 실행으로 대체한다.

5. **관리 기능 일부 미구현.** 사용자 역할 변경(FR-M-003), 런타임 설정 변경(FR-M-004),
   대량 삭제(FR-M-005)는 화면과 API 가 아직 없다.

6. **`docker-compose.yml` 은 개발용이다.** 운영에서는 DB·Redis 를 관리형 또는 별도
   호스트에 두고, 애플리케이션 서비스 정의만 참고한다.

---

## 9. 검증 이력

| 항목 | 결과 | 일자 |
|---|---|---|
| 전체 테스트 | 193개 통과 (SQLite + Mock 엔진) | 2026-09-02 |
| PostgreSQL 마이그레이션 | 성공 (PG 18) | 2026-09-02 |
| `audit_event` UPDATE/DELETE 차단 | 트리거·권한 양쪽에서 거부 확인 | 2026-09-02 |
| 런타임 계정 최소권한 | DDL·audit 수정 거부 확인 | 2026-09-02 |
| Celery 비동기 경로 | QUEUED → PROCESSING → COMPLETED | 2026-09-02 |
| 감사 해시 체인 (PG, 2프로세스) | 8건 검증 통과 | 2026-09-02 |
| **실제 모델(faster-whisper) 전사** | **미검증** — Mock 엔진으로만 확인 | — |
| **부하·동시성** | **미검증** | — |
