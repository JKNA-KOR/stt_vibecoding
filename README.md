# STT Service

녹취 음성 파일을 텍스트로 변환·저장·관리하는 사내 웹 서비스.
faster-whisper 기반이며, STT Core 는 교체 가능한 인터페이스 뒤에 있다.

## 문서

| 문서 | 내용 |
|---|---|
| [`STT_HARNESS_RULES.md`](STT_HARNESS_RULES.md) | 상위 계약. 충돌 시 이 문서가 우선한다 |
| [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) | 요구사항 정의서 (FR / NFR / SEC / AUD) |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | 구축·운영·진단, 메모리 산정, **알려진 제약** |

## 빠른 시작 (docker compose)

```bash
cp .env.example .env
# SESSION_SECRET, POSTGRES_PASSWORD, STT_APP_DB_PASSWORD, BOOTSTRAP_ADMIN_* 를 채운다

docker compose up -d db redis
docker compose run --rm migrate
docker compose exec -T db psql -v ON_ERROR_STOP=1 \
  -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v app_user="$STT_APP_DB_USER" \
  -f /opt/grant_least_privilege.sql
docker compose up -d api worker
docker compose exec api python -m scripts.bootstrap_admin
```

`http://127.0.0.1:8000` 에서 로그인한다. 자세한 절차와 주의사항은
[`docs/OPERATIONS.md`](docs/OPERATIONS.md) 를 본다.

**모델 없이 전 경로를 써 보려면** `.env` 에 `STT_ENGINE=mock` 을 둔다. 실제 음성을
전사하지는 않지만 업로드부터 다운로드까지 동작한다. 운영 환경에서 선택하면 기동이 거부된다.

## 로컬 개발 (도커 없이)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

export APP_ENV=local QUEUE_BACKEND=inline STT_ENGINE=mock BCRYPT_ROUNDS=6
export SESSION_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
export DATABASE_URL="sqlite+pysqlite:///$PWD/data/dev.db"

alembic upgrade head
BOOTSTRAP_ADMIN_USERNAME=admin BOOTSTRAP_ADMIN_PASSWORD='Local-Dev-Pass-1!' \
  python -m scripts.bootstrap_admin
uvicorn app.main:create_app --factory --reload
```

`QUEUE_BACKEND=inline` 은 브로커 없이 업로드 요청 스레드에서 바로 전사한다.
테스트·로컬 전용이며 운영에서 선택하면 기동이 거부된다.

## 테스트용 샘플 녹취

```bash
pip install edge-tts
python -m scripts.make_sample_recording --out-dir samples   # 들을 수 있는 상담 통화 (인터넷 필요)
python -m scripts.make_sample_audio --out-dir samples --set all   # 신호만 + 거부 케이스 (오프라인)
```

실제 고객 녹취를 테스트에 쓰지 않는다 (SEC-040). 대본은 지어낸 것이며 산출물은
커밋하지 않는다. 자세한 내용은 [`docs/OPERATIONS.md`](docs/OPERATIONS.md) §4.1.

## 구조

```
app/
  core/      설정·오류·로깅·보안 원시 기능
  stt/       STTEngine 인터페이스와 구현체, 전처리, 정규화, 출력 형식
  jobs/      큐 추상화, Job 서비스, 워커 파이프라인
  storage/   ORM 모델, 저장소(음성/Transcript), 질의
  auth/      인증 Provider, 세션, 권한
  audit/     Append-only 감사 로그
  api/       FastAPI 라우트·스키마·의존성
  web/       Jinja2 템플릿 + Vanilla JS (빌드 체인 없음)
migrations/  Alembic. audit_event append-only 트리거 포함
```

## 검사

```bash
ruff check .        # 린트 (보안·타입·예외 처리 규칙 포함)
pytest              # 단위·통합·보안·마이그레이션 테스트
```

테스트는 외부 인프라 없이 돈다 — Mock 엔진 + Inline 큐 + SQLite 이며, 스키마는
`create_all` 이 아니라 실제 마이그레이션으로 만든다.
