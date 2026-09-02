-- 런타임 계정 최소권한 부여 (SEC-026, Harness §12 / §19).
--
-- 마이그레이션(alembic upgrade head) 이후, 스키마 소유자 계정으로 실행한다.
--
--   psql "$MIGRATION_DATABASE_URL" -v app_user=stt_app -f scripts/grant_least_privilege.sql
--
-- 핵심은 audit_event 다. 애플리케이션 계정에 INSERT 만 주고 UPDATE/DELETE 를 주지 않는다.
-- DB 트리거(초기 마이그레이션)와 함께 이중 차단이 된다 — 트리거는 실수를 막고,
-- 권한은 애플리케이션이 침해되었을 때를 막는다.

\set ON_ERROR_STOP on

-- 업무 테이블: 일반적인 CRUD.
GRANT SELECT, INSERT, UPDATE, DELETE ON
    app_user, stt_job, stt_transcript, idempotency_record, runtime_config
TO :"app_user";

-- 설정 변경 이력도 추가 전용이다 (Harness §37).
GRANT SELECT, INSERT ON config_change TO :"app_user";

-- Append-only: INSERT 와 SELECT 만. UPDATE/DELETE 는 주지 않는다 (Harness §19).
GRANT SELECT, INSERT ON audit_event TO :"app_user";
REVOKE UPDATE, DELETE, TRUNCATE ON audit_event FROM :"app_user";

-- 시퀀스 사용 권한이 없으면 INSERT 자체가 실패한다.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app_user";

-- 마이그레이션 이력은 읽기만 한다. 런타임이 버전을 고쳐 쓸 이유가 없다.
GRANT SELECT ON alembic_version TO :"app_user";

-- 앞으로 마이그레이션이 만들 테이블에 대해 자동으로 같은 정책이 적용되지는 않는다.
-- 새 테이블을 추가하는 마이그레이션은 이 파일도 함께 갱신해야 한다.
