"""The tables of plan §12, one model each.

Designed from the journeys' queries, not copied from Request Tracker's
schema. Names keep RT's meaning in lower-case spelling; ids are integers so
an importer from an as-is database stays possible.

All timestamps are naive UTC datetimes: the database stores UTC, ``TZ`` is a
display setting only.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# --- the fixed vocabulary (plan §8, §10) -----------------------------------

NO_SUBJECT: Final = "[no subject]"

STATUSES: Final[tuple[str, ...]] = (
    "new",
    "open",
    "stalled",
    "resolved",
    "rejected",
    "deleted",
)
ACTIVE_STATUSES: Final[tuple[str, ...]] = ("new", "open", "stalled")
INACTIVE_STATUSES: Final[tuple[str, ...]] = ("resolved", "rejected", "deleted")


class GroupKind(StrEnum):
    """``groups.kind``: what a group row stands for."""

    USER_DEFINED = "user_defined"
    SYSTEM = "system"
    ROLE = "role"


class PrincipalKind(StrEnum):
    """``rights.principal_kind``: a system group or a role is a ``group``."""

    USER = "user"
    GROUP = "group"


class ObjectKind(StrEnum):
    """``rights.object_kind``: a grant is global (``system``) or on a queue."""

    SYSTEM = "system"
    QUEUE = "queue"


class WatcherRole(StrEnum):
    """``ticket_watchers.role``: the roles' membership on a ticket."""

    REQUESTOR = "requestor"
    CC = "cc"
    ADMINCC = "admincc"


class CustomFieldKind(StrEnum):
    """``custom_fields.kind``: the one type the journeys use, "Enter one value"."""

    FREEFORM_SINGLE = "freeform_single"


class TransactionType(StrEnum):
    """``transactions.type``: every write to a ticket is one of these."""

    CREATE = "Create"
    CORRESPOND = "Correspond"
    COMMENT = "Comment"
    STATUS = "Status"
    SET = "Set"
    CUSTOM_FIELD = "CustomField"


# The three system groups and the four role groups, by name. Membership in
# them is never a row: the system groups are membership by rule and the roles
# come from `tickets.owner_id` and `ticket_watchers`.
SYSTEM_GROUP_EVERYONE: Final = "Everyone"
SYSTEM_GROUP_PRIVILEGED: Final = "Privileged"
SYSTEM_GROUP_UNPRIVILEGED: Final = "Unprivileged"
SYSTEM_GROUPS: Final[tuple[str, ...]] = (
    SYSTEM_GROUP_EVERYONE,
    SYSTEM_GROUP_PRIVILEGED,
    SYSTEM_GROUP_UNPRIVILEGED,
)
ROLE_GROUPS: Final[tuple[str, ...]] = ("Owner", "Requestor", "Cc", "AdminCc")

# The right names of plan §8's vocabulary table, kept verbatim: they are facts
# about the product the goal text names.
RIGHTS: Final[tuple[str, ...]] = (
    "SuperUser",
    "ShowTicket",
    "CreateTicket",
    "ReplyToTicket",
    "CommentOnTicket",
    "ModifyTicket",
    "OwnTicket",
    "TakeTicket",
    "StealTicket",
    "SeeQueue",
    "AdminQueue",
    "AdminUsers",
    "AdminGroup",
    "AdminGroupMembership",
    "AdminCustomField",
    "SeeCustomField",
    "ModifyCustomField",
    "ShowConfigTab",
    "AdminOwnPersonalGroups",
)
GLOBAL_ONLY_RIGHTS: Final[frozenset[str]] = frozenset(
    {
        "SuperUser",
        "AdminUsers",
        "AdminGroup",
        "AdminCustomField",
        "ShowConfigTab",
    }
)

# The seeded queue and the unowned owner.
DEFAULT_QUEUE_NAME: Final = "General"
NOBODY_USER_NAME: Final = "Nobody"
ROOT_USER_NAME: Final = "root"
ROOT_USER_EMAIL: Final = "root@localhost"


def utcnow() -> dt.datetime:
    """Now, as a naive UTC datetime (what every timestamp column stores)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)


class Base(DeclarativeBase):
    """Declarative base; every table carries ``_TABLE_KW`` (InnoDB/utf8mb4)."""


_TABLE_KW: Final[dict[str, str]] = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_email", "email"), _TABLE_KW)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(100), nullable=True)
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    real_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    privileged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_updated: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (_TABLE_KW,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=GroupKind.USER_DEFINED)
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (Index("ix_group_members_user_id", "user_id"), _TABLE_KW)

    group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("groups.id"), primary_key=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), primary_key=True, nullable=False
    )


class Queue(Base):
    __tablename__ = "queues"
    __table_args__ = (_TABLE_KW,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    subject_tag: Mapped[str | None] = mapped_column(String(120), nullable=True)
    correspond_address: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    comment_address: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_updated: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class Right(Base):
    """A grant: a principal holds a right globally or on one queue."""

    __tablename__ = "rights"
    __table_args__ = (
        UniqueConstraint(
            "principal_kind",
            "principal_id",
            "right_name",
            "object_kind",
            "object_id",
            name="uq_rights_grant",
        ),
        Index("ix_rights_lookup", "right_name", "object_kind", "object_id"),
        _TABLE_KW,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    principal_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    principal_id: Mapped[int] = mapped_column(Integer, nullable=False)
    right_name: Mapped[str] = mapped_column(String(25), nullable=False)
    object_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    object_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    creator_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        Index("ix_tickets_queue_status", "queue_id", "status"),
        Index("ix_tickets_owner_status", "owner_id", "status"),
        Index("ix_tickets_status_created", "status", "created"),
        _TABLE_KW,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    queue_id: Mapped[int] = mapped_column(Integer, ForeignKey("queues.id"), nullable=False)
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False, default=NO_SUBJECT)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="new")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    started: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    resolved: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_updated: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    creator_id: Mapped[int] = mapped_column(Integer, nullable=False)
    last_updated_by: Mapped[int] = mapped_column(Integer, nullable=False)


class TicketWatcher(Base):
    """The roles' membership on a ticket (Requestor, Cc, AdminCc)."""

    __tablename__ = "ticket_watchers"
    __table_args__ = (Index("ix_ticket_watchers_user_id", "user_id"), _TABLE_KW)

    ticket_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tickets.id"), primary_key=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), primary_key=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), primary_key=True, nullable=False)


class Transaction(Base):
    """The audit trail: every write to a ticket is one row here."""

    __tablename__ = "transactions"
    __table_args__ = (Index("ix_transactions_ticket_id", "ticket_id", "id"), _TABLE_KW)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickets.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    field: Mapped[str | None] = mapped_column(String(64), nullable=True)
    old_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    creator_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class Message(Base):
    """A transaction's body, when it has one."""

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_message_id", "message_id"), _TABLE_KW)

    transaction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("transactions.id"), primary_key=True, nullable=False
    )
    content_type: Mapped[str] = mapped_column(String(80), nullable=False, default="text/plain")
    body: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    headers: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(160), nullable=True)


class CustomField(Base):
    __tablename__ = "custom_fields"
    __table_args__ = (_TABLE_KW,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CustomFieldKind.FREEFORM_SINGLE
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class QueueCustomField(Base):
    """Which custom fields a queue's tickets carry ("Applies to")."""

    __tablename__ = "queue_custom_fields"
    __table_args__ = (_TABLE_KW,)

    queue_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("queues.id"), primary_key=True, nullable=False
    )
    custom_field_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("custom_fields.id"), primary_key=True, nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class TicketCustomFieldValue(Base):
    """One freeform value per field per ticket."""

    __tablename__ = "ticket_custom_field_values"
    __table_args__ = (
        Index("ix_ticket_custom_field_values_field_value", "custom_field_id", "value"),
        _TABLE_KW,
    )

    ticket_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tickets.id"), primary_key=True, nullable=False
    )
    custom_field_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("custom_fields.id"), primary_key=True, nullable=False
    )
    value: Mapped[str] = mapped_column(String(255), nullable=False)


#: Every table of plan §12 but ``alembic_version``, for the migration and the
#: tests' truncation.
TABLE_NAMES: Final[tuple[str, ...]] = (
    "users",
    "groups",
    "group_members",
    "queues",
    "rights",
    "tickets",
    "ticket_watchers",
    "transactions",
    "messages",
    "custom_fields",
    "queue_custom_fields",
    "ticket_custom_field_values",
)
