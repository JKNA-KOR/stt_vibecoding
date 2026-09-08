"""add qa evaluation

상담 품질 평가(QA) 결과 테이블과 Job 의 QA 상태 컬럼을 추가한다 (Harness §50 / §51).

분석과 마찬가지로 Transcript 를 바꾸지 않고 파생 결과만 따로 둔다. 평가 실패가 전사나
분석 결과를 무효로 만들지 않도록 Job 상태와 분리된 `qa_status` 를 둔다.

점수 두 개(`overall_score`, `compliance_score`)와 심각 위반 여부에 인덱스를 건다.
목록 화면이 이 값들로 정렬하고 거르기 때문이다 — JSON 안에 묻어 두면 매번 전체를
읽어야 한다.

기존 Job 은 평가를 돌린 적이 없으므로 'NONE' 으로 채워진다.

Revision ID: c3f8a1d5b09e
Revises: 4ee391a2fbe7
Create Date: 2026-09-08 01:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'c3f8a1d5b09e'
down_revision: str | None = '4ee391a2fbe7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 모델의 JsonType 과 같은 표현. PostgreSQL 에서는 JSONB 를 쓴다.
_JSON = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), 'postgresql'
)


def upgrade() -> None:
    op.create_table(
        'stt_qa_evaluation',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=False),
        sa.Column('transcript_id', sa.String(length=64), nullable=True),
        sa.Column('overall_score', sa.Float(), nullable=False),
        sa.Column('grade', sa.String(length=10), nullable=False),
        sa.Column('compliance_score', sa.Float(), nullable=False),
        sa.Column('has_critical_violation', sa.Boolean(), nullable=False),
        sa.Column('violation_count', sa.Integer(), nullable=False),
        sa.Column('score_items', _JSON, nullable=True),
        sa.Column('violations', _JSON, nullable=True),
        sa.Column('strengths', _JSON, nullable=True),
        sa.Column('improvements', _JSON, nullable=True),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('model_name', sa.String(length=128), nullable=False),
        sa.Column('prompt_version', sa.String(length=32), nullable=False),
        sa.Column('schema_version', sa.String(length=32), nullable=False),
        sa.Column('rubric_version', sa.String(length=32), nullable=False),
        sa.Column('compliance_version', sa.String(length=32), nullable=False),
        sa.Column('duration_seconds', sa.Float(), nullable=True),
        sa.Column('transcript_truncated', sa.Boolean(), nullable=False),
        sa.Column('warnings', _JSON, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['job_id'], ['stt_job.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['transcript_id'], ['stt_transcript.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('job_id', name='uq_qa_job'),
    )
    with op.batch_alter_table('stt_qa_evaluation', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_stt_qa_evaluation_job_id'), ['job_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_stt_qa_evaluation_created_at'), ['created_at'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_stt_qa_evaluation_expires_at'), ['expires_at'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_stt_qa_evaluation_overall_score'), ['overall_score'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_stt_qa_evaluation_compliance_score'),
            ['compliance_score'],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_stt_qa_evaluation_has_critical_violation'),
            ['has_critical_violation'],
            unique=False,
        )

    with op.batch_alter_table('stt_job', schema=None) as batch_op:
        # 기존 행에도 값이 필요하므로 server_default 로 채운 뒤 제약을 남긴다.
        batch_op.add_column(
            sa.Column(
                'qa_status', sa.String(length=20), nullable=False, server_default='NONE'
            )
        )
        batch_op.add_column(sa.Column('qa_error_code', sa.String(length=64), nullable=True))

    # 애플리케이션이 항상 값을 넣으므로 DB 기본값은 남겨 두지 않는다. 기본값이 남아
    # 있으면 코드가 컬럼을 빠뜨려도 조용히 통과한다 (Harness §4.3).
    with op.batch_alter_table('stt_job', schema=None) as batch_op:
        batch_op.alter_column('qa_status', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('stt_job', schema=None) as batch_op:
        batch_op.drop_column('qa_error_code')
        batch_op.drop_column('qa_status')

    with op.batch_alter_table('stt_qa_evaluation', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_stt_qa_evaluation_has_critical_violation'))
        batch_op.drop_index(batch_op.f('ix_stt_qa_evaluation_compliance_score'))
        batch_op.drop_index(batch_op.f('ix_stt_qa_evaluation_overall_score'))
        batch_op.drop_index(batch_op.f('ix_stt_qa_evaluation_expires_at'))
        batch_op.drop_index(batch_op.f('ix_stt_qa_evaluation_created_at'))
        batch_op.drop_index(batch_op.f('ix_stt_qa_evaluation_job_id'))
    op.drop_table('stt_qa_evaluation')
