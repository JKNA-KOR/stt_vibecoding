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

이 저장소 compose 스택 실측 (2026-09-02):

| 프로세스 | Mock 엔진 | `tiny` 적재 후 |
|---|---|---|
| `api` | 188 MB | 119 MB |
| `worker` | 170 MB | **527 MB** |
| `db` | 49 MB | 47 MB |
| `redis` | 7 MB | 16 MB |
| **합계** | **약 415 MB** | **약 709 MB** |

`tiny`(가중치 72 MB)를 적재한 워커가 527 MB 다. 즉 **모델 가중치의 3~4배**가 실사용
메모리로 잡힌다 — CTranslate2 의 런타임 버퍼와 디코딩 버퍼가 함께 잡히기 때문이다.

이 비율을 그대로 적용한 다른 모델의 추정치:

| 모델 | 가중치 | 워커 1개 추정 | 근거 |
|---|---|---|---|
| `tiny` | 72 MB | **527 MB** | 실측 |
| `base` | ~145 MB | ~700 MB | tiny 실측에서 외삽 |
| `small` | ~490 MB | ~1.3 GB | 〃 |
| `medium` | ~1.5 GB | ~2.7 GB | 〃 |
| `large-v3` | ~3.1 GB | ~4.6 GB | 〃 |

> `tiny` 만 실측이고 나머지는 외삽이다. 긴 음성일수록 디코딩 버퍼가 커지므로,
> 배포 전 **실제 사용할 모델과 대표 길이 음성으로 측정해 이 표를 갱신할 것.**

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

## 4.1 테스트용 샘플 음성

실제 고객 녹취를 테스트에 쓰지 않는다 (SEC-040). 필요한 샘플은 합성해서 만든다.

스크립트가 둘이다. 용도가 다르다.

### 사람이 들을 수 있는 녹취 — `make_sample_recording.py`

```bash
pip install edge-tts
python -m scripts.make_sample_recording --out-dir samples
```

지어낸 상담 통화 대본을 TTS 로 읽혀 두 화자가 오가는 음성을 만든다. 재생하면 실제
통화처럼 들리고, STT 모델에 넣으면 의미 있는 텍스트가 나온다. 대본 원문(`.txt`)도 함께
저장되므로 전사 결과와 눈으로 비교할 수 있다.

| 산출물 | 길이 |
|---|---|
| `sample_call_short.wav` / `.mp3` / `.txt` | 30초 |
| `sample_call_full.wav` / `.mp3` / `.txt` | 98초 |

**이 스크립트는 인터넷을 쓴다.** Microsoft Edge TTS 에 대본 텍스트를 보낸다 — 보내는
것은 지어낸 문장뿐이고 고객 데이터가 아니지만, 폐쇄망(Harness §42)에서는 동작하지
않는다. 망 분리 환경에서는 인터넷이 되는 곳에서 미리 만들어 반입한다. 생성된 음성은
정적 자산이므로 서비스 런타임은 이 스크립트에도 인터넷에도 의존하지 않는다.

대본에는 실존 인물·번호를 넣지 않는다 (Harness §46).

### 신호만 필요할 때 — `make_sample_audio.py`

```bash
python -m scripts.make_sample_audio --out-dir samples --set all
```

인터넷 없이 동작하며, 정상 파일 5개(WAV/MP3/FLAC/M4A/OGG)와 **거부되어야 하는 파일
3개**(확장자 위장, 빈 파일, 허용되지 않는 확장자)를 만든다. 정상 경로만 확인하면
검증이 실제로 걸리는지 알 수 없으므로 거부 케이스가 함께 있다.

이쪽 산출물에는 사람의 발화가 없다. 업로드 검증과 Mock 엔진 확인 전용이다.

두 스크립트 모두 결정론적이라 같은 입력에 같은 파일이 나온다. 그래서 산출물은
커밋하지 않는다 (`/samples/` 는 .gitignore 대상).

---

## 4.2 LLM 분석 (선택 기능)

전사가 끝난 뒤 요약·키워드·분류·상담의견을 만든다. 기본값은 **꺼짐**이다.

### 원칙: 녹취는 사내를 벗어나지 않는다

분석은 **로컬에서 도는 LLM** 을 전제로 한다 (SEC-021). `LLM_BASE_URL` 이 외부 주소로
보이면 기동이 거부되며, 외부 전송이 필요하면 `ALLOW_EXTERNAL_LLM=true` 로 명시 승인해야
한다. 그 승인 전에 개인정보 영향평가와 위탁 계약이 선행되어야 한다 (Harness §8.2).

프롬프트와 녹취는 **분리된 메시지로** 전달된다. 녹취 안에 지시문처럼 보이는 문장이
있어도 데이터로만 다뤄진다 (Harness §13, SEC-024).

### 접속 설정

**접속 정보는 전부 `.env` 에서 온다.** 새 LLM 을 붙이는 데 코드 변경도 배포도 필요 없다.

| 키 | 설명 |
|---|---|
| `ENABLE_LLM_ANALYSIS` | 기능 on/off |
| `LLM_PROVIDER` | `ollama` / `openai-compatible` / `mock` |
| `LLM_BASE_URL` | 엔드포인트 주소. openai-compatible 은 보통 `/v1` 까지 |
| `LLM_MODEL_NAME` | 모델 이름 |
| `LLM_API_KEY` | 인증이 필요한 경우만. Bearer 로 전달되며 로그·응답에 남지 않는다 |
| `LLM_JSON_MODE` | `json_schema`(권장) / `json_object`(구형 엔드포인트) |
| `LLM_TIMEOUT_SECONDS` | 호출 상한 |
| `LLM_MAX_TRANSCRIPT_CHARS` | 프롬프트에 실을 녹취 길이 상한 |
| `ALLOW_EXTERNAL_LLM` | 외부 전송 명시 승인 |

`openai-compatible` 하나로 OpenAI 형식 `/chat/completions` 를 말하는 모든 엔드포인트를
붙인다 — OpenAI, Groq, vLLM, LM Studio, Ollama 의 `/v1` 등.

```bash
# 사내 vLLM
LLM_PROVIDER=openai-compatible
LLM_BASE_URL=http://llm.internal:8000/v1
LLM_MODEL_NAME=Qwen2.5-14B-Instruct
LLM_API_KEY=사내발급키
```

전체 예시는 `.env.example` 의 LLM 절에 있다.

### 외부 주소 판별

`ALLOW_EXTERNAL_LLM=false`(기본) 이면 외부로 보이는 주소에 기동을 거부한다.

- **사내로 보는 것**: 루프백, 사설 대역(10/8, 172.16/12, 192.168/16), 점 없는
  호스트명(`ollama`, `llm-server`), 사내 접미사(`.internal` `.local` `.corp`
  `.lan` `.intranet` `.svc` `.cluster.local`)
- **외부로 보는 것**: 그 외 전부

판별이 애매하면 외부로 본다 — 안전한 쪽으로 틀린다. 실제 경계 통제는 네트워크 정책이
하며, 이 검사는 `api.openai.com` 을 실수로 설정하는 것을 막는 용도다.

외부 엔드포인트는 **https 여야 한다.** 평문 http 로는 녹취를 보내지 않는다.

### LLM 을 어디에 둘 것인가

| 방법 | 명령 | 주의 |
|---|---|---|
| 이미 있는 사내 LLM | `LLM_BASE_URL` 만 지정 | 가장 단순하다 |
| 컨테이너로 함께 | `docker compose --profile llm up -d ollama` | 모델을 새로 받는다. 4B 기준 3GB+ 메모리 |
| 호스트 Ollama 사용 | `LLM_BASE_URL` 을 그 주소로 | 127.0.0.1 에만 바인딩되어 있으면 컨테이너에서 닿지 않는다 |

호스트 Ollama 가 snap 이면 노출 설정이 필요하다.

```bash
sudo snap set ollama host=0.0.0.0   # 그 뒤 재시작
```

### 동작

- 전사 완료 시 자동으로 분석 태스크가 큐에 들어간다.
- 화면에서 "다시 분석"으로 재실행할 수 있다. 결과는 기존 것을 **대체**한다.
- 분석이 실패해도 Transcript 는 그대로다. Job 은 `COMPLETED` 로 남고 `analysis_status`
  만 `FAILED` 가 된다.
- 전사와 같은 큐를 쓴다. 분석이 몰리면 전사가 밀리므로, 문제가 되면 큐를 분리하고
  워커를 따로 띄운다.

### 프롬프트 관리 (FR-M-004)

관리 화면에서 프롬프트를 고칠 수 있고, **재배포 없이 다음 분석부터 반영**된다.
기본값은 코드에 있고 DB 는 덮어쓰기만 하므로, DB 가 비어 있어도 서비스는 뜬다.

변경에는 **사유가 필수**이며 `config_change` 에 기록된다. 프롬프트 전문은 이력에
남기지 않고 길이와 해시만 남긴다 — 전문을 남기면 이력 테이블이 감당하지 못한다.

프롬프트를 바꾸면 결과가 달라진다. `prompt_version` 이 분석 결과와 함께 저장되므로
"이 분석이 어떤 프롬프트로 나왔는가"에 답할 수 있다 (Harness §36).

### 한계

모델 크기에 따라 분류 정확도가 크게 다르다. `gemma3:4b` 실측에서 요약과 키워드는
쓸 만했지만 **접촉분류가 틀렸다**(정상 종료된 통화를 "고객거절"로 분류). 분류 결과를
업무 판단에 그대로 쓰기 전에 대표 표본으로 검증할 것.

---

## 4.3 JSON 연동 (외부 시스템 통합)

multipart 를 쓰기 어려운 클라이언트(레거시 ESB, 일부 RPA)를 위한 입출력 경로다.

### 입력 — `POST /api/v1/jobs/json`

```json
{
  "filename": "call_20260902.wav",
  "content_type": "audio/wav",
  "audio_base64": "UklGRi...",
  "idempotency_key": "CRM-12345",
  "language": "ko"
}
```

응답은 접수 결과다. 전사는 비동기이므로 `status` 는 `QUEUED` 로 시작한다.

```json
{
  "job_id": "stt-...", "status": "QUEUED", "analysis_status": "NONE",
  "original_filename": "call_20260902.wav",
  "audio_duration_seconds": 30.32, "audio_size_bytes": 970284,
  "audio_sha256": "36af160e..."
}
```

`audio_sha256` 로 보낸 파일이 그대로 도착했는지 확인할 수 있다. `idempotency_key` 를
주면 같은 키로 두 번 보내도 Job 이 하나만 생긴다.

**검증은 multipart 와 같은 코드를 지난다.** 확장자 allowlist, 파일 시그니처, 크기,
재생시간 — 입구가 둘이어도 규칙이 갈라지면 약한 쪽이 우회로가 된다 (Harness §6).

크기 상한이 두 겹이다.
- `JSON_UPLOAD_MAX_MB` — **디코딩된 원본** 기준. 디코딩 도중 넘기면 즉시 중단한다.
- 요청 본문 자체 — base64 여유분(약 1.4배)까지. `Content-Length` 로 **읽기 전에**
  거절한다. JSON 은 파싱 시점에 전체가 메모리에 올라오므로 라우트에서는 이미 늦다.
  크기를 알 수 없는 chunked 요청은 411 로 거절한다.

기본값은 **꺼짐**이다 (`ENABLE_JSON_UPLOAD=false`).

### 출력 — `GET /api/v1/jobs/{id}/export`

Job · Transcript · 분석을 한 문서로 내보낸다.

```json
{
  "schema_version": "1.0",
  "exported_at": "2026-09-02T...",
  "job": { "id": "...", "engine": "faster-whisper", "model_name": "tiny",
           "stt_config_version": "...", "audio_sha256": "...", ... },
  "transcript": { "kind": "NORMALIZED", "text": "...", "segments": [...] },
  "analysis": { "summary": [...], "keywords": [...], ... }
}
```

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `include_transcript` | `JSON_EXPORT_INCLUDE_TRANSCRIPT` | Transcript 포함 |
| `include_segments` | `JSON_EXPORT_INCLUDE_SEGMENTS` | 세그먼트 포함 (빼도 `text` 는 남는다) |
| `include_analysis` | `JSON_EXPORT_INCLUDE_ANALYSIS` | 분석 포함 |
| `kind` | `NORMALIZED` | `RAW` / `NORMALIZED` |

**요청하지 않았거나 아직 만들어지지 않은 구성요소는 키 자체가 빠진다.** `null` 로
채우지 않는 이유는, 소비하는 쪽이 "요청하지 않음"과 "값이 없음"을 구분할 수 있어야
하기 때문이다.

`job` 에는 재현에 필요한 정보가 모두 담긴다 — 엔진·모델·설정 버전·전처리/정규화 버전·
음성 해시 (Harness §20). 저장 경로는 담기지 않는다 (§44).

`schema_version` 은 형식이 바뀌면 올린다. 소비하는 쪽이 변화를 감지할 수 있게 한다.

### 권한과 감사

내보내기는 Transcript 본문을 포함하므로 **다운로드 권한**을 요구하고 감사에도
다운로드(`TRANSCRIPT_DOWNLOADED`, `action=export_json`)로 기록된다 — 화면 조회와
파일 반출은 다른 행위다 (Harness §43). AUDITOR 는 내보낼 수 없다.

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

5. **`/ready` 의 `model_loaded` 는 API 프로세스 기준이다.** 모델은 워커가 적재하므로
   API 에서는 항상 `false` 다. 워커 상태는 `/api/v1/admin/status` 의 `workers_online`
   (Celery ping)으로 본다.

6. **대량 삭제(FR-M-005)가 아직 없다.** 사용자 역할 변경(FR-M-003)과 런타임 설정
   변경(FR-M-004)은 구현되었다.

7. **LLM 분석 분류의 정확도는 모델에 크게 의존한다.** §4.2 의 한계를 참고해 업무
   판단에 쓰기 전에 검증할 것.

8. **`docker-compose.yml` 은 개발용이다.** 운영에서는 DB·Redis 를 관리형 또는 별도
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
| 실제 모델 전사 (`tiny`, ko) | 98초 통화 → 21구간, RTF 0.048 | 2026-09-02 |
| 워커 메모리 (`tiny` 적재) | 527 MB 실측 | 2026-09-02 |
| LLM 분석 (Ollama gemma3:4b) | 98초 통화 → 요약·키워드·분류 생성 | 2026-09-02 |
| 런타임 프롬프트 변경 | 저장·복원·이력 기록 확인 | 2026-09-02 |
| JSON 업로드 / 내보내기 | 실제 스택에서 전 경로 확인 | 2026-09-02 |
| 사용자 역할 변경 | 감사 기록·자기강등 차단 확인 | 2026-09-02 |
| **`small` 이상 모델** | **미검증** — `tiny` 만 확인 | — |
| **컨테이너에서 LLM 접근** | **미검증** — 호스트 Ollama 가 127.0.0.1 바인딩 | — |
| **부하·동시성** | **미검증** | — |
