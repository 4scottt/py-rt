"""initial: every table of plan §12

Revision ID: 0001
Revises:
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "custom_fields",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_table(
        "queues",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("subject_tag", sa.String(length=120), nullable=True),
        sa.Column("correspond_address", sa.String(length=120), nullable=False),
        sa.Column("comment_address", sa.String(length=120), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("last_updated", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_table(
        "rights",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("principal_kind", sa.String(length=8), nullable=False),
        sa.Column("principal_id", sa.Integer(), nullable=False),
        sa.Column("right_name", sa.String(length=25), nullable=False),
        sa.Column("object_kind", sa.String(length=8), nullable=False),
        sa.Column("object_id", sa.Integer(), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("creator_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "principal_kind",
            "principal_id",
            "right_name",
            "object_kind",
            "object_id",
            name="uq_rights_grant",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_rights_lookup", "rights", ["right_name", "object_kind", "object_id"], unique=False
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=100), nullable=True),
        sa.Column("email", sa.String(length=120), nullable=True),
        sa.Column("real_name", sa.String(length=120), nullable=False),
        sa.Column("privileged", sa.Boolean(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("last_updated", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_users_email", "users", ["email"], unique=False)
    op.create_table(
        "group_members",
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("group_id", "user_id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_group_members_user_id", "group_members", ["user_id"], unique=False)
    op.create_table(
        "queue_custom_fields",
        sa.Column("queue_id", sa.Integer(), nullable=False),
        sa.Column("custom_field_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["custom_field_id"],
            ["custom_fields.id"],
        ),
        sa.ForeignKeyConstraint(
            ["queue_id"],
            ["queues.id"],
        ),
        sa.PrimaryKeyConstraint("queue_id", "custom_field_id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("queue_id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("started", sa.DateTime(), nullable=True),
        sa.Column("resolved", sa.DateTime(), nullable=True),
        sa.Column("last_updated", sa.DateTime(), nullable=False),
        sa.Column("creator_id", sa.Integer(), nullable=False),
        sa.Column("last_updated_by", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["queue_id"],
            ["queues.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_tickets_owner_status", "tickets", ["owner_id", "status"], unique=False)
    op.create_index("ix_tickets_queue_status", "tickets", ["queue_id", "status"], unique=False)
    op.create_index("ix_tickets_status_created", "tickets", ["status", "created"], unique=False)
    op.create_table(
        "ticket_custom_field_values",
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("custom_field_id", sa.Integer(), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(
            ["custom_field_id"],
            ["custom_fields.id"],
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
        ),
        sa.PrimaryKeyConstraint("ticket_id", "custom_field_id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_ticket_custom_field_values_field_value",
        "ticket_custom_field_values",
        ["custom_field_id", "value"],
        unique=False,
    )
    op.create_table(
        "ticket_watchers",
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("ticket_id", "user_id", "role"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_ticket_watchers_user_id", "ticket_watchers", ["user_id"], unique=False)
    op.create_table(
        "transactions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=True),
        sa.Column("old_value", sa.String(length=255), nullable=True),
        sa.Column("new_value", sa.String(length=255), nullable=True),
        sa.Column("creator_id", sa.Integer(), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_transactions_ticket_id", "transactions", ["ticket_id", "id"], unique=False)
    op.create_table(
        "messages",
        sa.Column("transaction_id", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(length=80), nullable=False),
        sa.Column("body", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("headers", sa.Text(), nullable=True),
        sa.Column("message_id", sa.String(length=160), nullable=True),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
        ),
        sa.PrimaryKeyConstraint("transaction_id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_messages_message_id", "messages", ["message_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_messages_message_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_transactions_ticket_id", table_name="transactions")
    op.drop_table("transactions")
    op.drop_index("ix_ticket_watchers_user_id", table_name="ticket_watchers")
    op.drop_table("ticket_watchers")
    op.drop_index(
        "ix_ticket_custom_field_values_field_value", table_name="ticket_custom_field_values"
    )
    op.drop_table("ticket_custom_field_values")
    op.drop_index("ix_tickets_status_created", table_name="tickets")
    op.drop_index("ix_tickets_queue_status", table_name="tickets")
    op.drop_index("ix_tickets_owner_status", table_name="tickets")
    op.drop_table("tickets")
    op.drop_table("queue_custom_fields")
    op.drop_index("ix_group_members_user_id", table_name="group_members")
    op.drop_table("group_members")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    op.drop_index("ix_rights_lookup", table_name="rights")
    op.drop_table("rights")
    op.drop_table("queues")
    op.drop_table("groups")
    op.drop_table("custom_fields")
