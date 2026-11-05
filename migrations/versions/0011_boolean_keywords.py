"""Store Boolean keyword trees while preserving existing literal conditions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_boolean_keywords"
down_revision: str | None = "0010_shared_keywords"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _quote_new_operators(value: str, terms: list[str]) -> str:
    if len(terms) == 1 and value.casefold() == terms[0]:
        if any(char in value for char in "!|()"):
            return '"' + value.replace('"', '""') + '"'
        return value
    # Previous canonical values already quote literal ampersands and escaped quotes.
    # Split only outside quotes, then quote any newly reserved characters.
    parts: list[str] = []
    buffer: list[str] = []
    quoted = False
    for char in value:
        if char == '"':
            quoted = not quoted
        if char == "&" and not quoted:
            parts.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
    parts.append("".join(buffer).strip())
    return " & ".join(
        '"' + part.replace('"', '""') + '"'
        if not part.startswith('"') and any(char in part for char in "!|()")
        else part
        for part in parts
    )


def upgrade() -> None:
    op.add_column("keywords", sa.Column("expression", postgresql.JSONB(), nullable=True))
    # Quoting and escaping old literals can increase their length. New input still
    # has the application limit of 500; extra storage keeps long legacy keys intact.
    op.alter_column("keywords", "value", type_=sa.String(1500))
    op.alter_column("keywords", "normalized_value", type_=sa.String(1500))
    op.drop_constraint("uq_keywords_owner_normalized_value", "keywords", type_="unique")
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, value, terms FROM keywords")).mappings().all()
    for row in rows:
        value = _quote_new_operators(row["value"], row["terms"])
        if value != row["value"]:
            connection.execute(
                sa.text(
                    "UPDATE keywords SET value = :value, normalized_value = :normalized WHERE id = :id"
                ),
                {"id": row["id"], "value": value, "normalized": value.casefold()},
            )
    _deduplicate_and_constrain()


def _deduplicate_and_constrain() -> None:
    op.execute("""
        DELETE FROM keywords WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY owner_id, normalized_value ORDER BY created_at, id
                ) AS position FROM keywords
            ) AS duplicates WHERE position > 1
        )
    """)
    op.create_unique_constraint(
        "uq_keywords_owner_normalized_value", "keywords", ["owner_id", "normalized_value"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_keywords_owner_normalized_value", "keywords", type_="unique")
    # Downgrading cannot retain Boolean semantics. Keep those queries as literals.
    op.execute(
        "UPDATE keywords SET terms = jsonb_build_array(normalized_value) WHERE expression IS NOT NULL"
    )
    op.drop_column("keywords", "expression")
    # Remove only the additional quoting that made a legacy value exceed 500.
    connection = op.get_bind()
    rows = (
        connection.execute(
            sa.text(
                "SELECT id, terms FROM keywords WHERE length(value) > 500 OR length(normalized_value) > 500"
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        terms = row["terms"]
        value = (
            terms[0]
            if len(terms) == 1
            else " & ".join(
                '"' + term.replace('"', '""') + '"' if any(c in term for c in ',&"') else term
                for term in terms
            )
        )
        connection.execute(
            sa.text("UPDATE keywords SET value = :value, normalized_value = :value WHERE id = :id"),
            {"id": row["id"], "value": value},
        )
    op.alter_column("keywords", "value", type_=sa.String(500))
    op.alter_column("keywords", "normalized_value", type_=sa.String(500))
    _deduplicate_and_constrain()
