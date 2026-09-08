# 전문 송수신 규격서

STT 서비스와 외부 시스템이 주고받는 전문(message) 형식을 정의한다. 연동처에 그대로
전달할 수 있는 문서다.

```
  녹취서버  ──── ① 수신 ────>  STT 서비스  ──── ② 송신 ────>  타 솔루션 (CRM 등)
                                    │
                                    └──── ③ 엔진 호출 ────>  타사 STT 솔루션
```

| # | 방향 | 누가 구현하는가 | 규격 위치 |
|---|---|---|---|
| ① | 녹취서버 → STT | 녹취서버(또는 배치) | [1절](#1-수신-전문-녹취서버--stt) |
| ② | STT → 타 솔루션 | 받는 쪽 | [2절](#2-송신-전문-stt--타-솔루션) |
| ③ | STT → 타사 엔진 | 타사 STT 벤더 | [3절](#3-엔진-연동-전문-stt--타사-stt-엔진) |

---

## 0. 공통 규칙

### 0.1 문자셋과 시각

- 인코딩은 **UTF-8** 고정이다.
- 모든 시각은 **ISO 8601 + 타임존**이다: `2026-09-08T14:03:11+09:00`.
  타임존이 없는 값은 UTC 로 해석한다.

### 0.2 인증

`Authorization: Bearer <token>` 헤더를 쓴다. 토큰은 양측이 사전 교환하며 **전문 본문에는
어떤 경우에도 담지 않는다.**

### 0.3 버전

전문에는 `message_version` 이 있다 (현재 `1.0`).

- **필드 추가는 버전을 올리지 않는다.** 받는 쪽은 **모르는 필드를 무시**해야 한다.
- 필드가 빠지거나 의미가 바뀌면 올린다.

이 규칙 덕분에 우리가 필드를 더해도 연동처가 깨지지 않는다. 반대로 받는 쪽이 엄격한
스키마 검증으로 모르는 필드를 거부하면 다음 배포에서 연동이 끊긴다.

### 0.4 오류 응답

HTTP 상태코드로 판단한다. 본문 형식은 강제하지 않는다.

| 상태 | 의미 | 보내는 쪽 동작 |
|---|---|---|
| 2xx | 정상 수신 | 완료 |
| 400, 401, 403, 404, 409, 413, 415, 422 | 다시 보내도 같음 | **재시도하지 않음** |
| 5xx, 타임아웃, 연결 실패 | 일시적 | 재시도 |

이 구분이 중요하다. 형식이 틀린 전문을 계속 재시도하면 상대 로그만 더럽히고 우리 큐도
오래 잡힌다.

### 0.5 멱등성

**같은 전문이 두 번 도착할 수 있다.** 재시도, 네트워크 단절 후 재발송, 운영자의 수동
재실행 — 전부 정상적인 경로다.

받는 쪽은 아래 값으로 중복을 걸러야 한다.

| 방향 | 멱등 키 |
|---|---|
| ① 수신 | 녹취 식별자 (`id`) — STT 서비스가 처리 |
| ② 송신 | `message_id` (헤더 `X-Message-Id` 와 같은 값) |

### 0.6 다루지 않는 것

전문에는 **내부 구조가 나가지 않는다.** 저장 경로, 프롬프트 본문, API 키, 모델
아티팩트 경로는 어떤 전문에도 포함되지 않는다.

---

## 1. 수신 전문 (녹취서버 → STT)

`RECORDING_SOURCE` 로 두 방식 중 하나를 고른다. 수집된 녹취는 **화면 업로드와 같은
검증**을 지난다 — 확장자 allowlist, 파일 시그니처, 크기, 재생시간. 입구가 늘어도 규칙이
갈라지면 약한 쪽이 우회로가 되기 때문이다.

수집은 자동으로 돌지 않는다. **연동 인터페이스** 화면의 `지금 수집` 버튼이나
`POST /api/v1/admin/integration/ingest` 를 운영 스케줄러(cron 등)가 부른다. 앱 안에
스케줄러를 두지 않은 이유는, API 를 여러 벌 띄우면 같은 건을 여러 번 가져가기 때문이다.

### 1.1 folder 방식 (`RECORDING_SOURCE=folder`)

폐쇄망에서 가장 흔하고, **녹취서버 쪽 개발이 필요 없다.**

녹취서버는 지정된 폴더에 음성 파일을 떨어뜨린다.

```
${RECORDING_INBOX_DIR}/
├── 20260908_140311_A1024_0511.wav      ← 음성 (필수)
└── 20260908_140311_A1024_0511.json     ← 메타데이터 (선택)
```

사이드카 JSON (선택):

```json
{
  "id": "REC-20260908-000511",
  "recorded_at": "2026-09-08T14:03:11+09:00",
  "agent_id": "A1024",
  "customer_ref": "C-88123"
}
```

| 필드 | 필수 | 설명 |
|---|---|---|
| `id` | 아니오 | 녹취 식별자. **없으면 파일명을 쓴다** |
| `recorded_at` | 아니오 | 통화 시각. 없으면 파일 수정 시각을 쓰고 그 사실을 기록한다 |
| `agent_id` | 아니오 | 상담원 식별자 |
| `customer_ref` | 아니오 | 고객 참조값 |

**처리 후 파일은 지우지 않고 옮긴다.**

```
${RECORDING_PROCESSED_DIR}/   ← 접수 성공
${RECORDING_FAILED_DIR}/      ← 검증 실패 등
```

수집이 잘못되었을 때 원본이 남아 있어야 다시 넣을 수 있다. 같은 이름이 이미 있으면
덮어쓰지 않고 뒤에 번호를 붙인다.

> **주의**: 녹취서버가 파일을 쓰는 도중에 수집이 집어 갈 수 있다. 임시 확장자로 쓴 뒤
> `rename` 하는 방식(원자적 이동)을 권장한다. `.wav.tmp` 는 허용 확장자가 아니므로
> 수집 대상에서 자동으로 빠진다.

### 1.2 http 방식 (`RECORDING_SOURCE=http`)

녹취서버가 REST API 를 제공하는 경우다.

**목록 조회**

```http
GET {RECORDING_API_BASE_URL}/recordings?limit=20
Authorization: Bearer <RECORDING_API_KEY>
Accept: application/json
```

응답:

```json
{
  "items": [
    {
      "id": "REC-20260908-000511",
      "filename": "20260908_140311_A1024.wav",
      "download_url": "/recordings/REC-20260908-000511/audio",
      "recorded_at": "2026-09-08T14:03:11+09:00"
    }
  ]
}
```

| 필드 | 필수 | 설명 |
|---|---|---|
| `id` | **예** | 멱등 키. 같은 값이면 다시 접수하지 않는다 |
| `filename` | **예** | 확장자로 형식을 판별한다 |
| `download_url` | **예** | 절대 또는 상대 주소 |
| `recorded_at` | 아니오 | 통화 시각 |

**필드 이름이 다르면 설정으로 맞춘다.** 이미 운영 중인 서버를 우리 규격에 맞춰 고치라고
할 수 없는 경우가 많기 때문이다.

```bash
RECORDING_FIELD_ID=callId
RECORDING_FIELD_FILENAME=fileName
RECORDING_FIELD_DOWNLOAD_URL=fileUrl
RECORDING_FIELD_RECORDED_AT=callStartTime
RECORDING_LIST_ITEMS_KEY=data
```

**음성 다운로드**

```http
GET {download_url}
Authorization: Bearer <RECORDING_API_KEY>
```

응답은 음성 바이너리다. 상대경로면 `RECORDING_API_BASE_URL` 기준으로 해석하며,
**다른 호스트를 가리키는 절대주소는 거절한다** — 녹취서버 응답이 조작되면 우리 서버가
임의 주소로 요청을 보내는 통로(SSRF)가 되기 때문이다.

### 1.3 이미 있는 또 하나의 입구

`ENABLE_JSON_UPLOAD=true` 이면 외부 시스템이 직접 밀어 넣을 수도 있다
(`POST /api/v1/jobs/json`, base64). 자세한 내용은 `docs/OPERATIONS.md` 4.8절에 있다.
녹취서버가 **밀어 넣는 쪽**이면 이 경로가, **우리가 가져오는 쪽**이면 1.1/1.2 가 맞다.

---

## 2. 송신 전문 (STT → 타 솔루션)

전사가 끝나면 결과를 지정된 주소로 보낸다.

```http
POST {OUTBOUND_URL}
Authorization: Bearer <OUTBOUND_API_KEY>
Content-Type: application/json; charset=utf-8
X-Message-Id: 3f9a1c2e8b7d4a06b1e5c9d2f8a37e40
```

```json
{
  "message_type": "STT_RESULT",
  "message_version": "1.0",
  "message_id": "3f9a1c2e8b7d4a06b1e5c9d2f8a37e40",
  "sent_at": "2026-09-08T14:09:02+09:00",
  "attempt": 0,

  "job": {
    "job_id": "stt-a4d18f10469b4ac0b3254d0ae1b0e4e9",
    "original_filename": "20260908_140311_A1024.wav",
    "status": "COMPLETED",
    "audio_sha256": "9f2c...",
    "audio_duration_seconds": 302.4,
    "created_at": "2026-09-08T14:05:00+09:00",
    "completed_at": "2026-09-08T14:08:57+09:00",
    "engine": "groq-whisper",
    "model_name": "whisper-large-v3",
    "detected_language": "Korean",
    "transcription_confidence": 0.8042,
    "source_reference": "ingest:REC-20260908-000511"
  },

  "transcript": {
    "kind": "NORMALIZED",
    "language": "ko",
    "segment_count": 42,
    "char_count": 1830,
    "text": "네, 고객센터 김민수입니다...",
    "segments": [
      { "index": 0, "start": 0.0, "end": 6.98, "text": "네, 고객센터 김민수입니다.", "confidence": 0.829 }
    ]
  },

  "analysis": {
    "summary": ["고객이 대출 한도 확인을 위해 전화했다."],
    "keywords": ["대출 한도", "신용등급"],
    "action_items": ["신용등급 확인 후 한도 안내"],
    "customer_reaction": "관심많음",
    "contact_classification": "처리완료",
    "sentiment": "중립",
    "opinion": "절차 안내는 명확했다.",
    "model_name": "z-ai/glm-5.2",
    "prompt_version": "1.0.0"
  },

  "qa": {
    "overall_score": 82.0,
    "grade": "양호",
    "compliance_score": 85.0,
    "violation_count": 3,
    "has_critical_violation": false,
    "score_items": [
      { "category": "응대 기본", "score": 13.0, "max_score": 15.0, "comment": "..." }
    ],
    "violations": [
      { "rule": "확인 없이 추측으로 안내", "severity": "주의", "evidence": "아마 그렇게...", "comment": "..." }
    ],
    "strengths": ["처리 절차를 명확히 안내했다."],
    "improvements": ["확정되지 않은 조건은 심사 절차와 함께 안내한다."],
    "summary": "...",
    "rubric_version": "0aea4e6fce2598cc",
    "compliance_version": "f8803b2c62fd3783"
  }
}
```

### 2.1 구성요소는 선택적으로 빠진다

`transcript` / `analysis` / `qa` 는 설정(`OUTBOUND_INCLUDE_*`)으로 끌 수 있고, 아직
만들어지지 않았으면 **키 자체가 빠진다.**

`null` 로 채우지 않는 이유는, 받는 쪽이 **"보내지 않음"과 "값이 없음"을 구분**할 수
있어야 하기 때문이다. 받는 쪽은 키의 존재 여부로 판단해야 한다.

### 2.2 값을 읽을 때 주의할 것

| 필드 | 주의 |
|---|---|
| `transcription_confidence` | **모델이 스스로 매긴 확신도이지 측정된 정확도가 아니다.** 없으면 `null` — 0 으로 해석하면 안 된다 |
| `qa.*_version` | 평가 기준의 해시. **값이 다르면 다른 잣대로 매긴 점수**라 서로 비교할 수 없다 |
| `qa.violations[].evidence` | 녹취 원문의 인용이다. 원문과 같은 등급으로 다뤄야 한다 |
| `source_reference` | 수집 경로로 들어온 건에만 있다. `ingest:` 접두사 뒤가 녹취서버의 `id` 다 |

### 2.3 응답과 재시도

2xx 면 성공이다. 본문은 보지 않는다.

실패하면 `OUTBOUND_MAX_ATTEMPTS`(기본 3) 까지 재시도하며, **`message_id` 는 그대로
유지된다.** 0.4절의 영구 실패 상태코드는 재시도하지 않는다.

**송신 실패는 전사 실패가 아니다.** 상대 시스템이 멈춰도 우리 Job 은 `COMPLETED` 로
남고 결과는 화면에서 그대로 볼 수 있다. 송신 성공·실패는 감사 로그(`send_stt_result`)에
남으므로 무엇이 안 갔는지 사후에 확인할 수 있다.

### 2.4 결과를 가져가는 방식도 있다

밀어 주는 것이 부담이면 받는 쪽이 조회할 수도 있다:
`GET /api/v1/jobs/{id}/export` (`ENABLE_JSON_EXPORT`). 형식은 다르지만 담기는 값은
같다.

---

## 3. 엔진 연동 전문 (STT → 타사 STT 엔진)

`STT_ENGINE=external` 로 두면 화면·분석·QA 는 그대로 쓰면서 **전사만 타사 솔루션에
맡긴다.** 벤더는 아래 엔드포인트 하나만 구현하면 된다.

> **벤더 API 에 우리가 맞추지 않는 이유**: 연동처마다 응답 매핑을 열어 두면 "이 필드가
> 저기서는 무슨 뜻인가"를 아무도 모르게 되고, 잘못 매핑된 값이 조용히 전사 결과로
> 저장된다. 규격이 하나면 문서 하나로 설명되고, 맞지 않으면 연동 시점에 드러난다.

### 3.1 요청

```http
POST {STT_API_BASE_URL}/transcribe
Authorization: Bearer <STT_API_KEY>
Content-Type: multipart/form-data; boundary=...
```

| 파트 | 필수 | 설명 |
|---|---|---|
| `file` | **예** | 음성 파일. 파일명은 항상 `audio.<확장자>` 로 보낸다 |
| `model` | **예** | `STT_MODEL_NAME` 값 |
| `language` | 아니오 | ISO-639-1 (예: `ko`). 없으면 자동 감지 |
| `vocabulary` | 아니오 | 용어사전 힌트. **무시해도 동작해야 한다** |

원본 파일명을 보내지 않는 이유는, 사용자가 올린 값이 그대로 헤더에 실리지 않게 하기
위해서다.

파일 크기 상한은 `STT_API_MAX_UPLOAD_MB`(기본 25MB) 다. 넘는 파일은 **호출하지 않고**
거절한다.

### 3.2 응답

```json
{
  "segments": [
    { "start": 0.0, "end": 6.98, "text": "네, 고객센터 김민수입니다.", "confidence": 0.83 }
  ],
  "language": "ko",
  "duration": 302.4
}
```

| 필드 | 필수 | 설명 |
|---|---|---|
| `segments[].start` / `end` | **예** | 초 단위 |
| `segments[].text` | **예** | 발화 내용. 빈 문자열이면 버린다 |
| `segments[].confidence` | 아니오 | 0~1. **모르면 보내지 말 것** — 0 을 보내면 "확신도 0"이 된다 |
| `language` | 아니오 | 감지된 언어 |
| `duration` | 아니오 | 없으면 마지막 세그먼트의 `end` 를 쓴다 |

**규격을 벗어난 응답은 조용히 넘기지 않는다.** 어긋난 채로 저장되면 나중에 "전사가 왜
이런가"를 추적할 수 없기 때문에, 전사를 실패로 처리하고 원인을 로그에 남긴다.

### 3.3 오류

| 상태 | STT 서비스의 처리 |
|---|---|
| 401, 403 | 인증 실패로 기록. 재시도함 |
| 400, 413, 415, 422 | **파일이 원인**으로 보고 재시도하지 않음 |
| 5xx, 타임아웃 | 시스템 오류로 보고 재시도함 |

### 3.4 연동 확인

```bash
# 설정 후 기동하면 연동 화면에서 엔진이 external 로 보인다
STT_ENGINE=external
STT_API_BASE_URL=https://stt.vendor.example/api/v1
STT_API_KEY=...
STT_MODEL_NAME=vendor-model-v2
ALLOW_EXTERNAL_STT=true     # 음성이 외부로 나간다. 없으면 기동을 거부한다
```

---

## 4. 보안 요구사항

연동처에 함께 전달할 항목이다.

1. **전송 구간은 HTTPS 여야 한다.** 사내 주소가 아닌 평문 `http://` 는 설정 검증이
   기동 시점에 거부한다. 녹취 음성과 전사 본문이 그대로 노출되기 때문이다.
2. **음성·전사·QA 결과는 Confidential 이다.** 받는 쪽도 같은 등급으로 보관·파기해야
   한다. 특히 `qa.violations[].evidence` 는 녹취 원문의 인용이다.
3. **토큰은 헤더로만 오간다.** 쿼리스트링에 담지 않는다 — 프록시 접근 로그에 남는다.
4. **음성이 사내를 벗어나는 경로는 명시 승인이 필요하다.** `ALLOW_EXTERNAL_STT`(전사),
   `ALLOW_EXTERNAL_LLM`(분석·QA), `ALLOW_EXTERNAL_OUTBOUND`(결과 송신) 셋은 각각
   별개다 — 위험의 크기가 다르므로 하나를 켰다고 나머지가 함께 열리지 않는다.
5. **개인정보 영향평가와 위탁 계약이 선행되어야 한다.** 위 승인 플래그를 켜기 전에
   확인한다.

---

## 5. 변경 이력

| 버전 | 날짜 | 내용 |
|---|---|---|
| 1.0 | 2026-09-08 | 최초 작성. 수신(folder/http) · 송신 · 엔진 연동 |

규격을 바꿀 때는 `app/integration/messages.py` 를 먼저 고친다. **그 파일이 규격의 단일
출처이며**, 이 문서는 그것을 설명한다. 둘이 갈라지면 문서가 거짓말을 하게 된다.
