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

### 대안: 호스팅 Whisper (`STT_ENGINE=groq-whisper`)

모델을 반입하는 대신 Groq 등의 전사 API 를 부를 수 있다. 로컬 `tiny` 보다 정확도가
크게 높고 반입 절차도 없지만, **음성 원본 자체가 사내 경계를 벗어난다.**

LLM 분석과는 위험의 크기가 다르다는 점이 중요하다. 분석은 이미 전사된 텍스트만
내보내지만, 이쪽은 녹취 파일을 그대로 업로드한다. 그래서 승인 플래그를 따로 둔다 —
`ALLOW_EXTERNAL_LLM` 을 켰다고 해서 이쪽이 함께 열리지 않는다.

```bash
STT_ENGINE=groq-whisper
STT_MODEL_NAME=whisper-large-v3      # 또는 whisper-large-v3-turbo
STT_API_BASE_URL=https://api.groq.com/openai/v1
STT_API_KEY=gsk_...
STT_API_TIMEOUT_SECONDS=600
STT_API_MAX_UPLOAD_MB=25             # Groq 무료 25 / 유료 100, OpenAI 25
ALLOW_EXTERNAL_STT=true              # 없으면 기동을 거부한다
```

엔드포인트 규약이 OpenAI `/audio/transcriptions` 와 같으므로, 주소와 모델 이름만 바꾸면
OpenAI(`https://api.openai.com/v1`, `whisper-1`)에도 그대로 붙는다.

기동 시점에 막는 것들:

| 상황 | 결과 |
|---|---|
| `ALLOW_EXTERNAL_STT=false` | 기동 거부 — 음성이 나가는 결정을 설정 실수로 하지 않는다 |
| `STT_API_KEY` 비어 있음 | 기동 거부 — 첫 전사에서 401 로 드러나는 것보다 낫다 |
| `STT_API_BASE_URL` 이 외부 `http://` | 기동 거부 — 평문 구간에 음성을 흘리지 않는다 |

로컬 엔진과 달라지는 것들:

- **아티팩트 해시가 없다.** 가중치가 손에 없으므로 `model_artifact_hash` 는 `remote`
  로 기록된다. 어떤 엔드포인트에서 나온 결과인지는 `model_version` 에 호스트로 남는다.
  없는 근거를 지어내지 않는 편이 낫다 (Harness §20).
- **크기 한도가 하나 더 있다.** `STT_MAX_UPLOAD_MB` 를 통과한 파일도 API 한도를 넘을 수
  있다. 넘으면 호출하지 않고 `FILE_TOO_LARGE` 로 즉시 거절하며, 재시도해도 결과가 같은
  실패이므로 자동 재시도 대상에서 빠진다 (Harness §24).
- **진행률이 없다.** 한 번의 요청으로 끝나므로 중간 보고가 없고 시작·끝만 기록된다.

API 가 음성을 거부하면(400/413/415/422) `AUDIO_DECODE_ERROR` 로 분류해 재시도하지 않고,
그 밖의 실패(5xx·타임아웃·연결 불가)는 `STT_MODEL_ERROR` 로 두어 재시도 경로를 남긴다.
오류에는 상태코드만 남는다 — 응답 본문에 API 키가 반사될 수 있다 (Harness §9 / §15).

**403 이 인증 실패가 아닐 수 있다.** Groq 앞단의 Cloudflare 는 User-Agent 가 없거나
라이브러리 기본값(`Python-urllib/x.y`)인 요청을 차단하고 `error code: 1010` 을 담은 403 을
돌려준다. 키 문제와 구분되지 않아 헤매기 쉬운 지점이라, 엔진이 `stt-service/<버전>` 을
UA 로 밝히고 나간다. 키가 실제로 유효한지는 아래로 확인한다.

```bash
docker compose exec worker python -c "
import json, urllib.request
from app.core.config import Settings
s = Settings()
req = urllib.request.Request(
    s.stt_api_base_url.rstrip('/') + '/models',
    headers={'Authorization': 'Bearer ' + s.stt_api_key.get_secret_value(),
             'User-Agent': f'{s.app_name}/{s.app_version}'})
with urllib.request.urlopen(req, timeout=20) as r:
    print([m['id'] for m in json.loads(r.read())['data'] if 'whisper' in m['id']])
"
```

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
| `LLM_PROVIDER` | `ollama` / `openai-compatible` / `openrouter` / `mock` |
| `LLM_BASE_URL` | 엔드포인트 주소. openai-compatible 은 보통 `/v1` 까지 |
| `LLM_MODEL_NAME` | 모델 이름 |
| `LLM_API_KEY` | 인증이 필요한 경우만. Bearer 로 전달되며 로그·응답에 남지 않는다 |
| `LLM_JSON_MODE` | `json_schema`(권장) / `json_object`(구형 엔드포인트) |
| `LLM_TIMEOUT_SECONDS` | 호출 상한 |
| `LLM_MAX_TRANSCRIPT_CHARS` | 프롬프트에 실을 녹취 길이 상한 |
| `LLM_MAX_OUTPUT_TOKENS` | 출력 토큰 상한. 추론 모델용 (`openrouter`) |
| `LLM_REASONING_EFFORT` | 추론 강도. 빈 값이면 모델 기본값 (`openrouter`) |
| `LLM_APP_NAME` / `LLM_SITE_URL` | OpenRouter 순위표 노출용 선택 헤더. 비우면 보내지 않는다 |
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

### OpenRouter 경유 (GLM 등 추론 모델)

`openrouter` Provider 는 OpenAI 호환 경로를 그대로 쓰되, 추론 모델에서만 생기는 세 가지를
함께 다룬다. 그래서 `openai-compatible` 로도 붙일 수는 있지만 권장하지 않는다.

1. **추론 토큰이 출력 한도를 잠식한다.** `LLM_MAX_OUTPUT_TOKENS`(기본 32768)를 함께
   보낸다. 한도에서 잘리면 무엇을 올려야 하는지 오류 메시지가 알려준다.
2. **응답에 코드펜스나 설명이 섞여 나오는 모델이 있다.** JSON 구간만 걷어내고 넘긴다.
   몇 글자를 걷어냈는지만 로그에 남기며, 본문은 남기지 않는다 (Harness §15).
3. **본문이 비는 경우의 원인이 다르다.** 토큰 한도 소진과 추론 토큰만 소비한 경우를
   구분해 알린다.

```bash
# 녹취가 사내를 벗어난다. ALLOW_EXTERNAL_LLM 승인 절차를 먼저 확인한다 (SEC-021).
ENABLE_LLM_ANALYSIS=true
LLM_PROVIDER=openrouter
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL_NAME=z-ai/glm-5.2
LLM_API_KEY=sk-or-v1-...
LLM_JSON_MODE=json_schema
LLM_TIMEOUT_SECONDS=600
ALLOW_EXTERNAL_LLM=true
```

`LLM_JSON_MODE=json_schema` 를 쓴다. `json_object` 로 두면 GLM 이 `summary` 를 배열이
아니라 문자열로 주거나 분류 항목을 통째로 빼먹고, 분석기가 그 값들을 버려서 결과가 반쯤
빈 채로 저장된다. 모델을 바꿀 때는 이 모드가 그 모델에서도 동작하는지 먼저 확인한다.

`ENABLE_LLM_ANALYSIS=true` 이고 Provider 가 `openrouter` 인데 `LLM_API_KEY` 가 비어 있으면
기동을 거부한다. 키 없이 떠 있다가 첫 분석에서 401 로 드러나는 것보다 낫다 (Harness §4.3).

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

## 4.3 상담 품질 평가 (QA)

전사가 끝난 상담을 **상담 원칙**과 **컴플라이언스 기준**으로 채점한다. 분석과 같은 LLM
설정을 쓰지만 호출은 별개다 — 기준이 바뀌어 재평가할 때 요약까지 다시 뽑을 이유가 없고,
둘 중 하나가 실패해도 다른 하나는 남아야 하기 때문이다.

```bash
ENABLE_LLM_ANALYSIS=true    # QA 의 전제. 없으면 기동을 거부한다
ENABLE_QA=true
QA_AUTO_RUN=true            # false 면 상담 상세의 버튼으로만 평가한다
```

**켜면 상담당 LLM 호출이 2회가 된다.** 비용과 처리 시간이 대략 두 배가 되므로, 전수
평가가 필요 없으면 `QA_AUTO_RUN=false` 로 두고 표본만 평가하는 편이 낫다.

### 점수는 모델이 아니라 서버가 계산한다

LLM 은 항목 점수를 잘 매기면서도 합계를 틀리는 일이 흔하다. 총점이 화면과 집계에 그대로
쓰이므로 다음 두 값은 코드가 다시 계산한다 (Harness §4.3).

| 값 | 계산 방식 |
|---|---|
| 상담 점수 | 항목별 점수의 합계. 배점을 넘는 점수는 배점으로 자른다 |
| 컴플라이언스 점수 | 100에서 시작해 심각 30 · 주의 15 · 경미 5씩 감점 (최저 0) |
| 등급 | 90↑ 우수 / 80↑ 양호 / 70↑ 보통 / 그 미만 미흡 |

모델이 준 값과 어긋나면 결과의 `warnings` 에 `overall_score_mismatch` 또는
`compliance_score_mismatch` 가 남는다. 어느 쪽이 옳은지는 알 수 없으므로 계산값을 쓰되
어긋났다는 사실을 지우지 않는다.

**채점 항목이 하나도 오지 않으면 0점으로 저장하지 않는다.** 평가 실패가 최악의 점수로
둔갑하면 상담원에게 부당하다. 그럴 때는 등급을 `판단불가` 로 두고 경고를 남긴다.

**위반은 근거 발화가 있을 때만 기록된다.** 녹취에서 인용할 발화가 없는 위반은 버려지고
`violation_without_evidence_dropped` 경고가 남는다 — 근거 없는 감점은 상담원이 다툴
여지를 주지 않는다.

### 평가 기준 관리 (FR-M-004)

기본 기준 두 벌이 코드에 실려 있다.

| 프로필 | 내용 |
|---|---|
| `finance` (기본) | 대출·금리·수수료 안내의 정확성, 불완전판매 방지 항목 포함 |
| `general` | 업종 무관한 표준 응대 항목만 |

관리 화면 > **QA 기준** 에서 바꾼다. 세 가지 방법이 모두 같은 곳으로 들어간다.

1. **기본값 불러오기** — 프로필을 골라 편집기를 채운다
2. **직접 편집** — 편집기에서 고친다
3. **파일 불러오기** — `.md` / `.txt` 파일로 편집기를 채운다 (브라우저 안에서만 읽으며
   서버로 업로드하지 않는다)

셋 다 편집기를 채울 뿐이고, **저장해야 반영된다.** 저장은 프롬프트와 같은 런타임 설정
경로를 지나므로 변경 사유가 함께 기록되고 이력이 남는다 (Harness §37).

기본값은 코드에 있고 DB 는 덮어쓰기만 한다. `기본값 복원` 을 누르면 DB 값이 지워지고
코드 기본값으로 돌아간다.

> 실려 있는 두 벌은 **출발점이지 정답이 아니다.** 실제 기준은 각 센터의 품질관리 규정에서
> 온다. 금융 프로필의 항목들은 금융소비자보호의 취지를 상담 현장 언어로 옮긴 것이며
> 법령 조문이 아니다 — 규정 준수 판단의 근거로 쓰지 않는다.

### 기준이 바뀌면 점수도 바뀐다

평가 결과에는 그때 쓰인 기준 본문의 해시(`rubric_version`, `compliance_version`)가 함께
저장된다. "지난달 82점, 이번달 74점"을 비교하기 전에 이 값이 같은지 확인해야 한다.
다르면 잣대가 바뀐 것이지 품질이 떨어진 것이 아닐 수 있다 (Harness §20).

### 화면과 권한

| 화면 | 내용 |
|---|---|
| QA 개요 (`/qa`) | 평균 점수, 등급 분포, 위반 심각도 집계 |
| 상담 점수 (`/qa/scores`) | 상담별 점수와 등급 목록 |
| 컴플라이언스 (`/qa/compliance`) | 위반이 확인된 상담만 |
| 상담 상세 > QA 평가 | 항목별 점수, 위반과 근거 발화, 잘한 점 / 개선할 점 |

조회 범위는 Job 과 같다 — USER 는 본인 상담만, ADMIN 은 전부 본다 (SEC-011).
AUDITOR 는 업무 데이터를 보지 못하므로 QA 결과도 볼 수 없다 (직무분리, Harness §46).

**QA 결과 조회는 감사에 남는다** (`QA_RESULT_VIEWED`). 상담원 평가 자료이므로 누가 언제
누구의 점수를 열어 봤는지가 남아야 한다 (Harness §17). 평가 실행은 `QA_EVALUATED`,
실패는 `QA_EVALUATION_FAILED` 로 기록된다.

### 한계

평가는 AI 가 만든 참고 자료다. 인사 평가나 징계의 근거로 그대로 쓰기 전에 사람이 근거
발화를 확인해야 한다. 특히 다음 경우는 점수를 그대로 믿을 수 없다.

- 녹취가 길어 잘린 경우 (`transcript_truncated`) — 앞부분만 평가된 점수다
- 전사 품질이 낮은 경우 — 잘못 인식된 발화가 위반으로 잡힐 수 있다
- 상담 유형이 기준과 맞지 않는 경우 — 예: 단순 안내 통화를 상품 설명 기준으로 채점

---

## 4.4 실시간 전사 (준실시간)

브라우저 마이크 입력을 WebSocket 으로 받아 조각 단위로 전사한다.

> **진짜 스트리밍 ASR 이 아니다.** 일정 길이(기본 6초)로 잘라 기존 배치 엔진에 넣는
> 방식이라 조각 경계에서 문맥이 끊기고, 문장이 잘린 채 화면에 뜬다. 이름 때문에 품질을
> 오해하지 않도록 화면에도 같은 안내를 띄운다.

이렇게 만든 이유는 엔진 교체 가능성(NFR-002) 때문이다. 스트리밍 전용 경로를 따로 두면
`STTEngine` 밖에 두 번째 전사 구현이 생기고, 그때부터 두 경로의 품질·설정·Provenance 가
갈라진다.

```bash
ENABLE_REALTIME_STT=true
REALTIME_SEGMENT_SECONDS=6        # 조각 길이
REALTIME_MAX_SESSION_SECONDS=1800 # 한 세션 최대 길이
REALTIME_MAX_SESSIONS=4           # 동시 세션 수 (API 프로세스당)
```

### 비용에 직접 영향을 준다

외부 STT API 를 쓰는 경우 **조각 하나가 곧 API 호출 한 번**이다. `REALTIME_SEGMENT_SECONDS=6`
이면 10분 통화에 100회를 부른다. 짧게 잡을수록 화면에 빨리 뜨지만 호출도 그만큼 늘어난다.
로컬 `faster-whisper` 로 돌리면 호출 비용은 없고 대신 CPU 를 쓴다.

### 마이크 권한은 기능과 함께 열린다

`ENABLE_REALTIME_STT=false` 이면 응답 헤더가 `Permissions-Policy: microphone=()` 이라
화면 코드가 실수로 `getUserMedia` 를 불러도 브라우저가 막는다. 켜면 `microphone=(self)` 로
바뀌어 이 출처에만 열린다. **기능 플래그가 꺼졌는데 권한만 열려 있는 상태를 만들지 않는다**
(Harness §11 / §54).

### WebSocket 의 방어는 HTTP 와 다르다

| 항목 | HTTP 라우트 | 실시간 WebSocket |
|---|---|---|
| 인증 | 세션 쿠키 (의존성) | 세션 쿠키 (라우트가 직접 확인) |
| CSRF | `X-CSRF-Token` 헤더 | **`Origin` 헤더 검증** |
| Rate Limit | 분당 호출 수 | 동시 세션 수 |

핵심은 두 번째 줄이다. **브라우저는 WebSocket 핸드셰이크를 CORS 로 막지 않는다.** 다른
사이트의 스크립트가 사용자의 쿠키로 연결을 열 수 있으므로, `Origin` 이 이 서비스 자신인지
직접 확인한다. `Origin` 이 없는 연결(브라우저가 아닌 클라이언트)도 거절한다 — 판별이
애매하면 막는 쪽으로 튼다.

종료 코드로 원인을 구분한다. 화면이 안내를 다르게 하기 위해서다.

| 코드 | 의미 |
|---|---|
| 4401 | 세션 없음·만료 |
| 4403 | Origin 거절 |
| 4404 | 기능 꺼짐 |
| 4408 | 세션 최대 길이 도달 |
| 4429 | 동시 사용 한도 |
| 4400 / 4500 | 입력 오류 / 내부 오류 |

### 음성은 남지 않는다

조각은 임시 WAV 로 잠깐 내려갔다가 **전사 직후 지워진다. 전사가 실패해도 지워진다.**
실시간 화면은 보관 대상이 아니므로 참조 없는 Confidential 파일을 만들지 않는다
(Harness §21 / §22). 전사 결과도 DB 에 저장되지 않으며, 필요하면 화면에서 텍스트로
내려받는다.

**따라서 실시간 전사는 AI 분석·QA 평가 대상이 아니다.** 요약과 품질 평가는 업로드한
상담에만 적용된다. 기록이 필요한 상담은 녹취 업로드 경로를 쓴다.

### 브라우저가 보내는 것은 원시 PCM 이다

`MediaRecorder` 를 쓰지 않는다. 그것이 만드는 webm/opus 는 첫 조각에만 컨테이너 헤더가
있어서 중간 조각만 떼어 보내면 디코딩되지 않는다. AudioWorklet(`/static/pcm-worklet.js`)
으로 16bit PCM 을 뽑아 보내면 서버가 어느 지점에서든 온전한 WAV 를 만들 수 있다.

WebSocket 연결은 CSP `connect-src 'self'` 로 허용된다 — CSP 에서 `'self'` 는 같은 호스트의
`ws:`/`wss:` 를 포함한다. 외부 CDN 이나 별도 신호 서버가 없으므로 폐쇄망에서도 동작한다.

### 한계

- 조각 경계에서 문맥이 끊긴다. 화자 분리도 없다 (본 버전 범위 밖).
- 짧은 조각은 배경 잡음에 취약하다. 무음에 가까운 조각은 빈 결과가 나온다.
- 동시 세션은 프로세스 단위로 센다. API 를 여러 벌 띄우면 실제 상한은
  프로세스 수 × `REALTIME_MAX_SESSIONS` 다.

---

## 4.5 금융권 용어사전

등록한 용어가 **두 곳에서 함께** 쓰인다. 사전을 두 벌로 나누면 "전사에는 등록했는데
분석이 못 알아듣는" 상태가 생기고, 그때 어느 쪽을 고쳐야 하는지 알기 어려워진다.

| 쓰이는 곳 | 무엇이 넘어가는가 |
|---|---|
| 전사 (업로드·실시간 공통) | 용어 이름만 이어 붙인 어휘 힌트 (Whisper `initial_prompt`) |
| AI 분석 / QA 평가 | 용어 + 뜻 + 오인식 표기 |

관리는 **용어사전** 메뉴에서 한다. 읽기는 로그인한 사용자면 누구나 할 수 있고(상담원이
통화 중에 뜻을 확인해야 한다), 추가·수정·삭제는 관리자만 할 수 있으며 전부 감사에
남는다 — 용어를 바꾸면 전사와 분석 결과가 함께 달라지기 때문이다.

기본 금융 용어 26개가 코드에 실려 있고 `기본 용어 불러오기` 로 채운다. **이미 등록된
용어는 건드리지 않는다** — 운영자가 고쳐 놓은 뜻을 기본값이 되돌리면 안 된다.

### 힌트는 전부 실리지 않는다

Whisper 의 `initial_prompt` 는 약 224 토큰까지만 반영된다. 넘겨도 오류가 나지 않고
**앞쪽만 쓰인 채 나머지는 조용히 버려진다.** 그러면 "등록했는데 왜 안 되지"가 된다.

그래서 힌트를 400자에서 자르고, `priority` 가 높은 용어부터 채운다. 용어사전 화면의
**전사 힌트** 패널이 엔진에 실제로 넘어가는 문자열과 "몇 개가 빠졌는지"를 그대로
보여준다. 빠진 용어가 있으면 중요한 것의 우선순위를 올린다.

뜻은 이 상한과 무관하다. 분석·QA 프롬프트는 문맥이 넉넉해서 상위 40개 용어의 뜻이
그대로 들어간다.

### 오인식 표기(aliases)는 전사 결과를 고치지 않는다

`aliases` 에 적은 표기는 **분석·QA 프롬프트에만** 들어가, 모델이 "중도 상환 수수료"와
"중도상환수수료"를 같은 용어로 읽게 한다. 전사 텍스트를 치환하지는 않는다 —
`app/stt/normalization.py` 는 "어휘는 바꾸지 않는다"를 계약으로 두고 있고, 그것을 깨려면
`NORMALIZER_VERSION` 을 올리고 Golden Regression 을 다시 돌려야 한다 (Harness §36).
사전 기반 치환이 필요하면 별도 결정으로 다뤄야 한다.

또한 힌트에는 별칭을 넣지 않는다. 힌트는 "이런 말이 나올 것"을 알리는 자리이지 잘못된
표기를 학습시키는 자리가 아니며, 오인식 표기를 넣으면 그쪽으로 끌려간다.

---

## 4.6 인식 신뢰도와 실제 정확도

목록의 이 값은 **정답률이 아니다.** Whisper 가 스스로 매긴 확신도이며, 실제로 얼마나
맞았는지와는 다른 척도다.

### 계산 방식

```
신뢰도 = exp(avg_logprob) × (1 − no_speech_prob)
```

`avg_logprob` 은 토큰당 평균 로그확률이므로 `exp` 를 취하면 "토큰 하나를 맞힐 평균
확률"이 된다. 여기에 무음 확률만큼 깎는다. Job 단위 값은 세그먼트를 **길이로 가중**해
평균한다 — 0.5초짜리 감탄사와 20초짜리 설명이 같은 무게를 가지면 전체 인상이 왜곡된다.

### 0.76 은 낮은 값이 아니다

**완벽하게 전사된 문장도 0.82 안팎에서 천장을 친다.** 실측 결과다.

```
avg_logprob   exp()   no_speech    최종   텍스트
     -0.202   0.817       0.002   0.815   네. 고객센터 김민수입니다. 무엇을 도와드릴까요?   ← 정답과 일치
     -0.202   0.817       0.002   0.815   안녕하세요. 지난주에 주문한 물건이 아직 안 와서…  ← 정답과 일치
     -0.304   0.738       0.727   0.202   감사합니다.                                    ← 무음 구간 환각
```

이 지표에서 1.0 은 "모든 토큰을 확률 1로 예측" 이라는 뜻이라 현실에서 나오지 않는다.
그래서 **0.76 을 "76점" 으로 읽으면 멀쩡한 결과를 불량으로 오해**하게 된다.

| 구간 | 표시 | 뜻 |
|---|---|---|
| 0.75 이상 | 양호 | 정상 범위 |
| 0.65~0.75 | 보통 | 잡음·발화 겹침이 있을 수 있음 |
| 0.65 미만 | 확인 | 무음 환각·저음질 의심 |

### 실제 정확도는 정답 대본과 비교해서 잰다

`scripts/measure_accuracy.py` 가 음성과 같은 이름의 `.txt` 대본을 읽어 CER/WER 을 낸다.

```bash
PYTHONPATH=. .venv/bin/python scripts/measure_accuracy.py samples/sample_call_full.mp3
```

실측 결과 (whisper-large-v3, 2026-09-08):

| 파일 | 문자 정확도 | 모델 신뢰도 |
|---|---|---|
| `sample_call_full.mp3` (79어절) | **97.4%** | 0.80 |
| `sample_call_short.mp3` (21어절) | 84.8% | 0.76 |

짧은 파일의 오류 두 건은 모두 설명이 된다.

* `에이 일이삼사오` → `A12345` — **오히려 정규화가 더 정확하다.** 대본을 기준으로 하니
  오류로 세어졌을 뿐이다. 이것을 정답으로 치면 정확도는 96% 가 된다.
* 끝부분 `감사합니다` — 무음 구간 환각이다. 신뢰도가 0.20 으로 떨어져 **지표가 이 구간을
  실제로 잡아냈다.**

즉 **실제 정확도는 97% 대이고, 화면의 0.76~0.80 은 그것과 다른 척도의 정상값**이다.
한국어는 조사·띄어쓰기 변동이 커서 WER 이 과하게 나오므로 CER 을 주 지표로 본다.

### 정확도를 높이는 방법

효과가 큰 순서다.

1. **LLM 후처리 보정** (`ENABLE_LLM_CORRECTION`) — 이미 켜져 있다. 용어사전을 근거로
   오인식을 되돌린다. 실측에서 `알피` → `IRP`, `체육석구장` → `퇴사한 부장` 을 고쳤다.
2. **용어사전 우선순위** — 계속 틀리는 용어를 용어사전 화면에서 우선순위 상향한다.
   전사 힌트는 800바이트(약 40~50개)까지만 실린다 (4.10 참고).
3. **원본 음질** — 가장 근본적인 지렛대다. 8kHz 전화망 압축 음성은 어떤 모델을 써도
   한계가 있다. 녹취 설정에서 16kHz 이상, 모노, 무손실 또는 고비트레이트로 저장한다.
4. **무음 구간 정리** — 통화 끝의 긴 무음이 환각(`감사합니다`, `자막제공자` 등)을 만든다.
   녹취 단계에서 잘라 내면 사라진다.

### 무음 구간 환각 걸러내기

Whisper 계열은 통화 끝의 긴 무음에서 없는 말을 만들어 낸다 — `감사합니다`,
`자막제공자`, `시청해주셔서 감사합니다` 같은 문구다. 이 구간은 **신뢰도가 눈에 띄게
낮게 나오므로**(실측: 정상 발화 0.82 / 환각 0.20) 그 차이로 걸러낼 수 있다.

```bash
STT_MIN_SEGMENT_CONFIDENCE=0.3    # 0 이면 끄기(기본)
```

정규화 단계에서 적용된다. **RAW Transcript 는 그대로 남으므로** 걸러진 내용은 화면의
`원본` 에서 언제든 확인할 수 있다 (Harness §50).

**발화를 지우는 동작이라 기본은 꺼짐이다.** 임계값을 높이면 진짜 발화까지 사라진다.
0.3~0.4 를 권하며, 0.6 이상은 정상 발화가 0.6~0.8 대에 몰려 있어 위험하다.

안전장치 두 가지가 있다.

| 상황 | 동작 |
|---|---|
| 신뢰도를 주지 않는 엔진(Mock 등) | 지우지 않는다 — 모른다고 버리면 전사가 통째로 사라진다 |
| 모든 구간이 임계값 미만 | **아무것도 지우지 않는다** — 빈 Transcript 는 "발화가 없었다" 로 보여 문제를 감춘다 |

구간이 빠지면 상담 상세의 계보 안내에 "구간 N개가 정규화에서 제외됨" 이 표시된다.
조용히 지우면 "왜 이 말이 없지" 가 되기 때문이다 (Harness §4.3).

> 이 값은 `stt_config_version` 에 포함된다. 바꾸면 같은 음성에서 다른 세그먼트 수가
> 나오므로, 이전 결과와 비교할 때 이 버전이 같은지 확인해야 한다 (§20).
>
> 규칙이 바뀌었으므로 `NORMALIZER_VERSION` 을 1.1.0 으로 올렸다 (§36).

### 효과가 없는 설정 (주의)

`STT_BEAM_SIZE` 와 `STT_VAD_ENABLED` 는 **외부 API 엔진(`groq-whisper`, `external`)에서
아무 일도 하지 않는다.** 이 파라미터는 로컬 `faster-whisper` 전용이며, API 는 받지
않는다. 다만 Provenance 에는 설정값 그대로 기록되므로, 외부 엔진 결과의 이 두 값은
"요청한 설정" 이지 "적용된 설정" 이 아니다 (Harness §20 — 값을 읽을 때 이 점을 감안한다).

외부 엔진에서 실제로 결과를 바꾸는 것은 모델(`STT_MODEL_NAME`), 언어(`STT_LANGUAGE`),
용어 힌트 셋뿐이다.

---

## 4.6.1 상담 삭제 (FR-T-009)

상담 목록 화면에서 여러 건을 골라 지운다. **관리자에게만 보이는 경로**이며 세 단계를
거친다 — 되돌릴 수 없는 동작이기 때문이다 (Harness §48).

1. 목록 우측 상단 `녹취 삭제` → 목록 옆에 체크박스가 나타난다
2. 하나 이상 고르면 `선택 삭제` 가 켜진다
3. 팝업이 **무엇이 사라지는지 파일명으로 나열**한 뒤 확인을 받는다

### 지워지는 것과 순서

음성 원본 → 변환 결과 → Job 행 순서다. 분석과 QA 는 FK 의 `ON DELETE CASCADE` 가 함께
거둔다.

**행을 먼저 지우면 파일이 고아가 된다.** 참조 없는 Confidential 파일은 보관정책 삭제
대상에도 잡히지 않아 영원히 남는다 (Harness §21 / §22). 그래서 순서를 지킨다.

처리 중(`QUEUED` / `PROCESSING`)인 상담은 거절한다. 워커가 사라진 행을 붙들고 실패하기
때문이며, 먼저 취소해야 한다.

### 부분 실패

한 건이 실패해도 나머지는 지운다. 전부 되돌리면 "무엇이 왜 안 지워졌는지" 알 수 없고
다시 눌러도 같은 결과가 나온다 (Harness §4.3). 실패한 건과 사유가 화면에 뜬다.

한 번에 100건까지만 지운다 — 목록에서 전체 선택한 뒤 누르는 사고를 한 번에 크게 만들지
않기 위해서다 (§24).

### 감사

삭제는 **`JOB_DELETED`** 이벤트로 남는다. 취소(`STT_JOB_CANCELLED`)와 구분한 이유는,
감사자가 로그만 보고 "멈춘 것"과 "지운 것"을 혼동하면 안 되기 때문이다 (§17).

| 남는 것 | 남지 않는 것 |
|---|---|
| 누가(actor), 언제, 어느 Job, 파일명, 음성 해시, 삭제된 전사 수 | Transcript 본문 (§64) |

감사 로그 화면에서 `JOB_DELETED` 로 검색하면 삭제 이력만 볼 수 있다.

> 버튼을 감춘 것은 편의이지 권한 제어가 아니다 (Harness §10). 실제 차단은 API 가 하며,
> `JOB_DELETE_OWN` / `JOB_DELETE_ANY` 권한으로 판단한다.

---

## 4.7 라이브 캡션 재생

전사 결과를 한 줄씩 드러내는 **표시 효과**다.

| 화면 | 기본 동작 |
|---|---|
| 상담 상세 (`/jobs/{id}`) | 결과가 처음 뜰 때 한 번 자동 재생 |
| 상담 목록 (`/consultations`) | **자동 재생하지 않는다.** `캡션 재생` 버튼으로만 |

목록 화면에서 자동으로 돌리지 않는 이유는, 그 화면의 목적이 "내용을 확인하는 것"이기
때문이다. 여러 상담을 옮겨 다니며 볼 때 매번 캡션이 흐르면 방해가 된다. 목록 화면은
상세정보를 먼저 보여주고 스크립트는 그 아래에 정적으로 붙인다.

**스트리밍이 아니다.** 업로드 경로는 파일 전체를 한 번에 전사하므로 중간 결과가
존재하지 않는다 — 특히 Groq 같은 외부 API 는 응답이 한 번에 온다. 진짜 실시간 자막은
실시간 전사 화면(`/realtime`)의 몫이며, 둘을 이름으로 헷갈리지 않도록 코드 주석과
화면 안내에 같은 말을 적어 두었다.

조각이 120개를 넘으면 재생만 몇 분이 걸리므로 효과 없이 바로 보여준다. 브라우저의
`prefers-reduced-motion` 설정이 켜져 있으면 효과를 쓰지 않는다.

---

## 4.9 Transcript 후처리 (문맥 보정 + 화자분리)

전사 결과를 LLM 에 보내 문장을 다듬고 발화자를 나눈다.

```bash
ENABLE_LLM_ANALYSIS=true      # 전제. 같은 LLM 설정을 쓴다
ENABLE_LLM_CORRECTION=true    # 문맥·용어사전에 맞춰 문장 보정
ENABLE_DIARIZATION=true       # 발화자를 상담원/고객으로 구분
```

둘은 **한 번의 LLM 호출**에서 함께 처리된다. 같은 문장을 두 번 읽히면 비용이 두 배가
되고, 보정된 문장과 화자 판단이 서로 다른 입력을 보게 된다.

### 원본을 덮어쓰지 않는다

결과는 `LLM_CORRECTED` 라는 **세 번째 Transcript 종류**로 저장된다.

| 종류 | 내용 |
|---|---|
| `RAW` | 엔진이 낸 그대로 |
| `NORMALIZED` | 표기만 정리 (어휘는 바꾸지 않음) |
| `LLM_CORRECTED` | 문맥 보정 + 화자 라벨 |

보정이 잘못돼도 앞의 둘은 온전하고, 화면의 종류 선택으로 나란히 비교할 수 있다
(Harness §50). 계보(`input_transcript_id`)와 사용된 모델·프롬프트 버전이 함께 저장되어
"이 문장이 왜 이렇게 바뀌었는가"에 답할 수 있다 (§20 / §51).

### 화자분리는 음향 기반이 아니다

> **정확한 화자 식별이 아니다.** 말투와 내용으로 **추정**하는 것이며, 음향 특성(성문)을
> 쓰지 않는다. 두 사람의 말투가 비슷하거나 발화가 겹치면 틀린다.

제대로 된 음향 화자분리는 별도 모델(pyannote 등)이 필요하고 폐쇄망 반입·라이선스 문제가
따로 있다. 지금 구조는 그 모델 없이 2인 상담에서 쓸 만한 수준을 목표로 한다. 근거가
부족한 문장에는 라벨을 붙이지 않는다 — 억지로 반씩 나누지 않는다.

### 문장이 사라지지 않게 하는 장치

LLM 은 목록을 다루면서 조용히 몇 줄을 빠뜨리거나 합치는 일이 흔하다. 그것이 그대로
저장되면 **녹취에서 발화가 사라지는데, 전사 결과에서 이보다 나쁜 실패는 없다.**

| 장치 | 하는 일 |
|---|---|
| index 대조 | 순서가 아니라 index 로 원본과 맞춰 붙인다 |
| 누락 보존 | 결과가 돌아오지 않은 문장은 **원문을 그대로 남긴다** |
| 시각 고정 | start/end 는 절대 바꾸지 않는다 (모델은 시각을 모른다) |
| 길이 제한 | 원문의 3배를 넘는 "보정"은 지어낸 것으로 보고 원문을 쓴다 |
| 분할 전송 | 40문장씩 나눠 보낸다. 한 번에 다 보내면 출력이 잘린다 |

### 비용

상담당 LLM 호출이 한 번 더 늘고, 긴 녹취는 40문장마다 한 번씩 호출된다. 30분 통화에
200문장이면 5회다. 분석·QA 까지 켜면 상담당 총 7~8회가 된다.

---

## 4.10 용어사전 규모와 쓰임

기본 사전에 **1028개** 용어가 실려 있다 (직위·직책 62개 포함). `기본 용어 불러오기` 로
채운다.

### 1000개를 다 쓰는 곳은 따로 있다

| 쓰이는 곳 | 실리는 양 | 비고 |
|---|---|---|
| 전사 힌트 (Whisper `initial_prompt`) | **약 40개** | 크기 상한 800바이트 |
| 분석 · QA 프롬프트 | 상위 40개 (뜻 포함) | |
| 후처리 보정 프롬프트 | 상위 200개 (뜻 + 오인식 표기) | **여기가 사전의 주 무대다** |

전사 힌트는 Whisper 의 224 토큰 제약 때문에 1000개를 실을 수 없다. 그래서 **사전을
크게 만드는 실익은 후처리 보정 쪽에 있다** — 거기서는 상위 200개가 뜻과 오인식 표기까지
함께 들어가 "중도 상환 수수료" 를 "중도상환수수료" 로 되돌린다.

### 힌트 상한은 글자 수가 아니라 바이트다

엔드포인트는 프롬프트를 **UTF-8 바이트**로 잰다 (Groq 는 896바이트). **한글은 글자당
3바이트**라 글자 수로 재면 실제 크기의 3분의 1로 착각하게 된다.

실제로 그렇게 실패한 적이 있다. 사전을 1000개로 늘린 뒤 힌트가 393자(=935바이트)가
되었는데, 상한을 400"자"로 잡아 두어 그대로 나갔고 엔드포인트가 `invalid_prompt` 로
400 을 돌려주어 **전사 자체가 실패**했다. 음성은 멀쩡했다.

지금은 `HINT_MAX_BYTES=800` 으로 바이트를 세며, 용어사전 화면이 현재 크기와 상한을
바이트로 보여준다.

> 이 실패는 오류 분류도 함께 어렵게 만들었다. 400 을 무조건 "파일이 원인" 으로 보고
> `AUDIO_DECODE_ERROR` 로 분류했기 때문에, 로그만 보면 음성 파일을 의심하게 된다.
> 지금은 응답의 `invalid_prompt` 표식을 확인해 **설정 문제로 분류하고 무엇을 줄여야
> 하는지 오류에 담는다** (Harness §4.3 / §23). 본문 전체를 남기지는 않는다 — API 키가
> 반사될 수 있다 (§15).

### 우선순위

분류별 기본값 위에, 오인식이 잦고 업무상 중요한 **핵심 용어 30개**를 최우선으로 둔다.
분류 우선순위만 쓰면 같은 분류 안에서 이름순으로 잘려 하필 중요한 용어가 빠진다 —
실제로 "중도상환수수료" 가 그랬다.

운영 중 특정 용어가 계속 틀리면 관리 화면에서 그 용어의 우선순위만 올리면 된다.

---

## 4.11 JSON 연동 (외부 시스템 통합)

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
