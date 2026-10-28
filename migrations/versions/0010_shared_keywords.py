"""Share one keyword list across each owner's tracked groups."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_shared_keywords"
down_revision: str | None = "0009_catalog_search"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("keywords", sa.Column("owner_id", postgresql.UUID(as_uuid=True)))
    op.execute("""
        UPDATE keywords AS k SET owner_id = g.owner_id
        FROM tracked_groups AS g WHERE k.group_id = g.id
    """)
    # Removed chats must not reintroduce conditions the user stopped tracking.
    op.execute("""
        DELETE FROM keywords USING tracked_groups AS g
        WHERE keywords.group_id = g.id AND g.status = 'removed'
    """)
    op.drop_constraint("uq_keywords_group_normalized_value", "keywords", type_="unique")
    # Legacy literal "a & b" must stay distinct from the newer AND rule a & b.
    op.execute("""
        UPDATE keywords SET value = '"' || replace(value, '"', '""') || '"',
            normalized_value = '"' || replace(normalized_value, '"', '""') || '"'
        WHERE jsonb_array_length(terms) = 1 AND normalized_value = terms->>0
          AND (strpos(normalized_value, '&') > 0 OR strpos(normalized_value, ',') > 0
               OR strpos(normalized_value, '"') > 0)
          AND length('"' || replace(normalized_value, '"', '""') || '"') <= 500
          AND length('"' || replace(value, '"', '""') || '"') <= 500
    """)
    op.execute("""
        DELETE FROM keywords WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY owner_id, normalized_value ORDER BY created_at, id
                ) AS position FROM keywords
            ) AS duplicates WHERE position > 1
        )
    """)
    op.alter_column("keywords", "owner_id", nullable=False)
    op.create_foreign_key(
        "fk_keywords_owner", "keywords", "owners", ["owner_id"], ["id"], ondelete="CASCADE"
    )
    op.drop_column("keywords", "group_id")
    op.create_unique_constraint(
        "uq_keywords_owner_normalized_value", "keywords", ["owner_id", "normalized_value"]
    )
    # A chat paused after deleting its last condition can now use the shared list.
    op.execute("""
        UPDATE tracked_groups AS g SET
            status = CASE WHEN g.status = 'paused' THEN 'active'::group_status ELSE g.status END,
            configuration_version = configuration_version + 1
        WHERE g.status != 'removed'
          AND EXISTS (SELECT 1 FROM keywords AS k WHERE k.owner_id = g.owner_id)
    """)


def downgrade() -> None:
    op.drop_constraint("uq_keywords_owner_normalized_value", "keywords", type_="unique")
    op.add_column("keywords", sa.Column("group_id", postgresql.UUID(as_uuid=True)))
    # Expand the shared list back into one independent copy per chat.
    op.execute("""
        INSERT INTO keywords (id, owner_id, group_id, value, normalized_value, terms, created_at)
        SELECT md5(k.id::text || g.id::text)::uuid, k.owner_id, g.id,
               k.value, k.normalized_value, k.terms, k.created_at
        FROM keywords AS k JOIN tracked_groups AS g ON g.owner_id = k.owner_id
        WHERE k.group_id IS NULL AND g.status != 'removed'
    """)
    op.execute("DELETE FROM keywords WHERE group_id IS NULL")
    op.alter_column("keywords", "group_id", nullable=False)
    op.create_foreign_key(
        "keywords_group_id_fkey",
        "keywords",
        "tracked_groups",
        ["group_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_keywords_group_normalized_value", "keywords", ["group_id", "normalized_value"]
    )
    op.drop_column("keywords", "owner_id")
