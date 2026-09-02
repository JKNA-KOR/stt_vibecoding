#!/bin/bash
# 런타임 전용 DB 계정 생성 (SEC-026, Harness §12).
#
# PostgreSQL 이미지의 초기화 훅은 데이터 디렉터리가 비어 있는 첫 기동에만 실행된다.
# 여기서는 계정만 만들고, 테이블 단위 권한은 마이그레이션 이후에 부여한다
# (scripts/grant_least_privilege.sql) — 이 시점에는 아직 테이블이 없기 때문이다.
#
# POSTGRES_USER 는 스키마 소유자이자 마이그레이션 실행 계정이다.
# STT_APP_DB_USER 는 애플리케이션 런타임 계정이며 DDL 권한을 갖지 않는다.

set -euo pipefail

if [[ -z "${STT_APP_DB_USER:-}" || -z "${STT_APP_DB_PASSWORD:-}" ]]; then
    echo "STT_APP_DB_USER / STT_APP_DB_PASSWORD 가 설정되지 않았다" >&2
    exit 1
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v app_user="$STT_APP_DB_USER" -v app_password="'$STT_APP_DB_PASSWORD'" \
     -v db_name="$POSTGRES_DB" <<'SQL'
CREATE ROLE :"app_user" LOGIN PASSWORD :app_password;

-- 접속과 스키마 사용만 먼저 연다. 테이블 권한은 마이그레이션 후에 준다.
GRANT CONNECT ON DATABASE :"db_name" TO :"app_user";
GRANT USAGE ON SCHEMA public TO :"app_user";

-- 런타임 계정은 새 객체를 만들 수 없다.
REVOKE CREATE ON SCHEMA public FROM :"app_user";
REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
SQL

echo "런타임 계정 '$STT_APP_DB_USER' 생성 완료"
