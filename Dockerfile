# STT 서비스 이미지 (Harness §38 / §40 / §42).
#
# 원칙:
#   * 모델 아티팩트를 이미지에 굽지 않는다. 빌드가 네트워크에 의존하면 폐쇄망 재현이
#     불가능해지고, 이미지가 수 GB 로 커진다. 모델은 볼륨으로 마운트한다 (§42).
#   * 런타임은 비특권 사용자로 돈다. 애플리케이션이 자기 코드를 덮어쓸 수 없다.
#   * 빌드 단계와 런타임 단계를 나눠 컴파일 도구가 최종 이미지에 남지 않게 한다 (§40).

# --- 빌드 단계 ---------------------------------------------------------------
FROM python:3.14-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 의존성만 먼저 복사한다. 소스가 바뀌어도 이 레이어는 재사용된다.
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# --- 런타임 단계 -------------------------------------------------------------
FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# 비특권 사용자. 홈 디렉터리는 모델 캐시 경로 기본값이 필요할 때를 위해 만들어 둔다.
RUN groupadd --system --gid 1001 stt \
    && useradd --system --uid 1001 --gid stt --create-home --shell /usr/sbin/nologin stt

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=root:root app ./app
COPY --chown=root:root migrations ./migrations
COPY --chown=root:root scripts ./scripts
COPY --chown=root:root alembic.ini pyproject.toml ./

# 데이터 디렉터리만 쓰기 가능하다. 소스는 root 소유 · 읽기 전용이므로
# 애플리케이션이 자기 코드를 수정할 수 없다.
RUN mkdir -p /data/audio /data/transcripts /data/tmp /models \
    && chown -R stt:stt /data

USER stt

ENV PYTHONPATH=/app \
    STORAGE_ROOT=/data \
    TEMP_DIR=/data/tmp

EXPOSE 8000

# 기본 명령은 API. 워커는 compose 에서 command 를 덮어쓴다.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
