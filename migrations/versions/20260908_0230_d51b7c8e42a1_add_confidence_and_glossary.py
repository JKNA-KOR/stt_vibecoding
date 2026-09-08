"""add transcription confidence and glossary

두 가지를 더한다.

  1. `stt_job.transcription_confidence` — 모델이 스스로 매긴 평균 확신도(0~1).
     **측정된 정확도가 아니다.** 낮으면 사람이 확인해 볼 구간이라는 신호일 뿐이다
     (Harness §20). 확신도를 주지 않는 엔진에서는 NULL 이며, 이 마이그레이션 이전에
     만들어진 Job 도 NULL 이다 — 0 으로 채우면 "확신도 0"으로 오해된다.

  2. `glossary_term` — 금융권 용어사전. 전사 힌트와 분석·QA 프롬프트에 함께 쓰인다.
     용어는 업무 데이터가 아니라 설정에 가까우므로 보관정책 대상이 아니다.

Revision ID: d51b7c8e42a1
Revises: c3f8a1d5b09e
Create Date: 2026-09-08 02:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'd51b7c8e42a1'
down_revision: str | None = 'c3f8a1d5b09e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), 'postgresql'
)


def upgrade() -> None:
    with op.batch_alter_table('stt_job', schema=None) as batch_op:
        batch_op.add_column(sa.Column('transcription_confidence', sa.Float(), nullable=True))

    op.create_table(
        'glossary_term',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('term', sa.String(length=120), nullable=False),
        # 오인식되기 쉬운 표기들. 전사 결과를 이 표기에서 표준 용어로 되돌리는 데 쓴다.
        sa.Column('aliases', _JSON, nullable=True),
        sa.Column('category', sa.String(length=40), nullable=False),
        sa.Column('definition', sa.Text(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        # 힌트에 실을 우선순위. Whisper 프롬프트는 길이 상한이 있어 전부는 못 넣는다.
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_by', sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        # 같은 용어를 두 번 등록하면 힌트에 중복으로 실린다.
        sa.UniqueConstraint('term', name='uq_glossary_term'),
    )
    with op.batch_alter_table('glossary_term', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_glossary_term_is_active'), ['is_active'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_glossary_term_category'), ['category'], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('glossary_term', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_glossary_term_category'))
        batch_op.drop_index(batch_op.f('ix_glossary_term_is_active'))
    op.drop_table('glossary_term')

    with op.batch_alter_table('stt_job', schema=None) as batch_op:
        batch_op.drop_column('transcription_confidence')
