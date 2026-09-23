"""A fixture dataset, loaded through the services (FP O09).

``pyrt seed --fixture <path|->`` reads one dataset (schema 1: queues, users,
groups, custom fields, rights, and tickets with their steps; every reference
is by name and the tickets' own numbers are the only ids) and writes it
through the service functions the pages call, so each row is one the app
itself could have written. Three things differ from a person clicking
through the pages, on purpose:

* **the dates are the file's**: after each write the ticket, and every
  transaction the write made, is stamped with the time the file gives;
* **the notification hooks are silenced** for the load and put back after
  it, so a few hundred tickets mail nobody;
* **ticket ids are asserted**: the file numbers its tickets 1..N and the
  database must agree, which is why only a side with an empty tickets table
  (and none of the file's names) is loaded.

It prints one line, the checksum, which a loader on another side of a
comparison prints identically for the same file::

    fixture v1 seed=… dataset=<sha256 prefix> queues=… users=… groups=… members=…
    rights=… custom_fields=… applied=… tickets=… create=… correspond=… comment=…
    status=… custom_field=… last_ticket=…

(one line on the wire). Every number on it is verified in the database
first (:func:`verify`), not echoed from the file; ``--check`` runs that
verification alone and writes nothing.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pyrt.config import Settings
from pyrt.customfields import service as customfields
from pyrt.db.models import (
    GLOBAL_ONLY_RIGHTS,
    RIGHTS,
    ROOT_USER_NAME,
    CustomField,
    GroupKind,
    GroupMember,
    PrincipalKind,
    Queue,
    QueueCustomField,
    Right,
    Ticket,
    TicketCustomFieldValue,
    TicketWatcher,
    Transaction,
    TransactionType,
    User,
    WatcherRole,
)
from pyrt.db.seed import find_group, find_queue, find_user
from pyrt.groups import service as groups
from pyrt.queues import service as queues
from pyrt.rights import service as rights
from pyrt.tickets import hooks, update
from pyrt.tickets import service as tickets
from pyrt.users import service as users

#: The one schema this loader reads; the line's ``v1`` is this number.
SCHEMA: Final = 1

#: The step types, each written the way its page writes it.
CORRESPOND: Final = "correspond"
COMMENT: Final = "comment"
STATUS: Final = "status"
CUSTOM_FIELD: Final = "custom_field"
STEP_TYPES: Final[tuple[str, ...]] = (CORRESPOND, COMMENT, STATUS, CUSTOM_FIELD)

#: The statuses a step may name: plan §8's vocabulary without ``deleted``
#: (a dataset describes tickets; it does not remove them).
STEP_STATUSES: Final[tuple[str, ...]] = ("new", "open", "stalled", "resolved", "rejected")

#: Every time in the file: UTC, to the second.
DATE_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"
_DATE_SHAPE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

#: The columns' sizes (plan §12), so a value too long for its column is the
#: file's error before anything is written, not a database error half-way.
NAME_MAX: Final = 200
DESCRIPTION_MAX: Final = 255
EMAIL_MAX: Final = 120
REAL_NAME_MAX: Final = 120
SUBJECT_TAG_MAX: Final = 120
SUBJECT_MAX: Final = 200
VALUE_MAX: Final = customfields.VALUE_MAX

#: The four hook lists a load silences, by the names ``hooks.fire`` uses.
_HOOKS: Final[tuple[str, ...]] = (
    hooks.CREATED,
    hooks.CORRESPONDED,
    hooks.COMMENTED,
    hooks.STATUS_CHANGED,
)


# --- the failures -------------------------------------------------------------


class FixtureError(Exception):
    """Why the dataset was not loaded or not found: one line on standard error."""

    #: The command's exit status for this kind of failure.
    exit_code: int = 1

    def report(self) -> str:
        """The line standard error carries."""
        return f"fixture: {self}"


class SchemaError(FixtureError):
    """The file is not a dataset this loader reads; nothing was touched."""

    exit_code = 2


class Refused(FixtureError):
    """The side is not empty enough to load into; nothing was written."""

    def report(self) -> str:
        return f"fixture: refusing to load: {self}; reset the side first"


class PartlyLoaded(FixtureError):
    """The load stopped after it had written: the side holds part of the dataset."""

    def report(self) -> str:
        return f"fixture: {self}; the side is partly loaded: reset the side and load again"


class Absent(FixtureError):
    """The database does not hold what the dataset describes."""

    def report(self) -> str:
        return f"fixture absent: {self}"


def summary(exc: BaseException) -> str:
    """An unexpected exception as one short line (its type and first line)."""
    lines = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {lines[0] if lines else ''}"[:300].rstrip(": ")


# --- the dataset ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueueSpec:
    """A queue: one the side already has (``existing``), or one to create."""

    name: str
    existing: bool = False
    description: str = ""
    subject_tag: str | None = None


@dataclass(frozen=True, slots=True)
class UserSpec:
    name: str
    email: str
    real_name: str
    privileged: bool


@dataclass(frozen=True, slots=True)
class GroupSpec:
    name: str
    description: str
    members: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """A custom field of the one kind there is ("Enter one value")."""

    name: str
    description: str
    applies_to: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GrantSpec:
    """The rights one principal holds on one object (a queue, or globally)."""

    principal: str
    kind: str
    queue: str | None
    rights: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Step:
    """One thing that happened to a ticket after it was created."""

    type: str
    at: dt.datetime
    actor: str
    content: str = ""
    status: str = ""
    field: str = ""
    value: str = ""


@dataclass(frozen=True, slots=True)
class TicketSpec:
    id: int
    queue: str
    subject: str
    content: str
    requestor: str
    owner: str | None
    creator: str
    created: dt.datetime
    steps: tuple[Step, ...]

    @property
    def final_status(self) -> str:
        """The status the last ``status`` step names; a ticket is born ``new``."""
        for step in reversed(self.steps):
            if step.type == STATUS:
                return step.status
        return "new"

    def final_values(self) -> dict[str, str]:
        """Each custom field's value after the last step that set it."""
        return {step.field: step.value for step in self.steps if step.type == CUSTOM_FIELD}


@dataclass(frozen=True, slots=True)
class Dataset:
    schema: int
    seed: int
    clock: dt.datetime
    window_start: dt.datetime
    queues: tuple[QueueSpec, ...]
    users: tuple[UserSpec, ...]
    groups: tuple[GroupSpec, ...]
    custom_fields: tuple[FieldSpec, ...]
    rights: tuple[GrantSpec, ...]
    tickets: tuple[TicketSpec, ...]

    def steps_of(self, kind: str) -> int:
        """How many steps of that type the file holds."""
        return sum(1 for ticket in self.tickets for step in ticket.steps if step.type == kind)


@dataclass(frozen=True, slots=True)
class Counts:
    """What :func:`verify` found, in the order the line prints it."""

    queues: int
    users: int
    groups: int
    members: int
    rights: int
    custom_fields: int
    applied: int
    tickets: int
    create: int
    correspond: int
    comment: int
    status: int
    custom_field: int
    last_ticket: int


def digest(raw: bytes) -> str:
    """The sha256 of the file's exact bytes, in hex (the line prints twelve)."""
    return hashlib.sha256(raw).hexdigest()


def line(dataset: Dataset, counts: Counts, sha: str) -> str:
    """The checksum line: the same text on every side for the same file."""
    numbers = (
        ("queues", counts.queues),
        ("users", counts.users),
        ("groups", counts.groups),
        ("members", counts.members),
        ("rights", counts.rights),
        ("custom_fields", counts.custom_fields),
        ("applied", counts.applied),
        ("tickets", counts.tickets),
        ("create", counts.create),
        ("correspond", counts.correspond),
        ("comment", counts.comment),
        ("status", counts.status),
        ("custom_field", counts.custom_field),
        ("last_ticket", counts.last_ticket),
    )
    head = [f"fixture v{dataset.schema}", f"seed={dataset.seed}", f"dataset={sha[:12]}"]
    return " ".join(head + [f"{name}={number}" for name, number in numbers])


# --- reading the file -------------------------------------------------------------


def parse(raw: bytes) -> Dataset:
    """The dataset in ``raw``, or :class:`SchemaError` with the first problem.

    Everything the file can be wrong about on its own is checked here, so a
    bad file is refused before the database is touched: the shapes and
    types, the sizes of the columns the values go into, the names that must
    be unique, the references between the file's own objects, the ticket
    numbering and the order of each ticket's times. What depends on the side
    (a user or group the file names but does not create) is the load's.
    """
    try:
        document = json.loads(raw)
    except ValueError as exc:  # JSONDecodeError, or bytes that are not text
        raise SchemaError(f"the file is not JSON: {exc}") from None
    top = _object(document, "the dataset")
    schema = top.get("schema")
    if isinstance(schema, bool) or schema != SCHEMA:
        raise SchemaError(f"schema must be {SCHEMA}; the file says {schema!r}")

    dataset = Dataset(
        schema=SCHEMA,
        seed=_integer(top, "seed", "the dataset"),
        clock=_date(top, "clock", "the dataset"),
        window_start=_date(top, "window_start", "the dataset"),
        queues=tuple(
            _queue_spec(item, f"queues[{i}]")
            for i, item in enumerate(_array(top, "queues", "the dataset"))
        ),
        users=tuple(
            _user_spec(item, f"users[{i}]")
            for i, item in enumerate(_array(top, "users", "the dataset"))
        ),
        groups=tuple(
            _group_spec(item, f"groups[{i}]")
            for i, item in enumerate(_array(top, "groups", "the dataset"))
        ),
        custom_fields=tuple(
            _field_spec(item, f"custom_fields[{i}]")
            for i, item in enumerate(_array(top, "custom_fields", "the dataset"))
        ),
        rights=tuple(
            _grant_spec(item, f"rights[{i}]")
            for i, item in enumerate(_array(top, "rights", "the dataset"))
        ),
        tickets=tuple(
            _ticket_spec(item, f"tickets[{i}]", i + 1)
            for i, item in enumerate(_array(top, "tickets", "the dataset"))
        ),
    )
    _cross_check(dataset)
    return dataset


def _cross_check(dataset: Dataset) -> None:
    """The references between the file's own objects, and its unique names."""
    _unique((spec.name for spec in dataset.queues), "queue")
    _unique((spec.name for spec in dataset.users), "user")
    _unique((spec.email for spec in dataset.users), "the address")
    _unique((spec.name for spec in dataset.groups), "group")
    _unique((spec.name for spec in dataset.custom_fields), "custom field")
    _unique(
        (
            f"{spec.kind} {spec.principal} on {spec.queue or 'the system'}"
            for spec in dataset.rights
        ),
        "the grant to",
    )

    queue_names = {spec.name for spec in dataset.queues}
    applies: dict[str, frozenset[str]] = {}
    for spec in dataset.custom_fields:
        for queue in spec.applies_to:
            if queue not in queue_names:
                raise SchemaError(f"custom field {spec.name} applies to {queue}, not a queue")
        applies[spec.name] = frozenset(spec.applies_to)
    unprivileged = {spec.name for spec in dataset.users if not spec.privileged}
    for grant in dataset.rights:
        if grant.queue is not None and grant.queue not in queue_names:
            raise SchemaError(f"the grant to {grant.principal} is on {grant.queue}, not a queue")
        if grant.kind == PrincipalKind.USER and grant.principal in unprivileged:
            # The User Rights pages offer privileged users only (plan §8).
            raise SchemaError(
                f"the grant to {grant.principal}: an unprivileged user holds no rights"
            )
    for ticket in dataset.tickets:
        if ticket.queue not in queue_names:
            raise SchemaError(f"ticket {ticket.id} is in {ticket.queue}, not a queue")
        for number, step in enumerate(ticket.steps, start=1):
            if step.type != CUSTOM_FIELD:
                continue
            if step.field not in applies:
                raise SchemaError(
                    f"ticket {ticket.id} step {number} sets {step.field}, not a field"
                )
            if ticket.queue not in applies[step.field]:
                raise SchemaError(
                    f"ticket {ticket.id} step {number} sets {step.field}, "
                    f"which does not apply to {ticket.queue}"
                )


def _object(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SchemaError(f"{where} must be an object")
    return {str(key): item for key, item in value.items()}


def _array(obj: dict[str, object], key: str, where: str) -> list[object]:
    value = obj.get(key)
    if not isinstance(value, list):
        raise SchemaError(f"{where}: {key} must be an array")
    return list(value)


def _text(
    obj: dict[str, object],
    key: str,
    where: str,
    *,
    limit: int | None = None,
    required: bool = True,
    trimmed: bool = True,
) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise SchemaError(f"{where}: {key} must be a string")
    if required and not value.strip():
        raise SchemaError(f"{where}: {key} is empty")
    if trimmed and value != value.strip():
        raise SchemaError(f"{where}: {key} has spaces at an end")
    if limit is not None and len(value) > limit:
        raise SchemaError(f"{where}: {key} is longer than {limit} characters")
    return value


def _nullable_text(obj: dict[str, object], key: str, where: str, *, limit: int) -> str | None:
    if key not in obj:
        raise SchemaError(f"{where}: {key} is missing (null is allowed)")
    return None if obj[key] is None else _text(obj, key, where, limit=limit)


def _integer(obj: dict[str, object], key: str, where: str) -> int:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaError(f"{where}: {key} must be an integer")
    return value


def _flag(obj: dict[str, object], key: str, where: str) -> bool:
    value = obj.get(key)
    if not isinstance(value, bool):
        raise SchemaError(f"{where}: {key} must be true or false")
    return value


def _date(obj: dict[str, object], key: str, where: str) -> dt.datetime:
    """A time in the file's one form, as the naive UTC datetime a column stores."""
    value = obj.get(key)
    if not isinstance(value, str) or not _DATE_SHAPE.match(value):
        raise SchemaError(f"{where}: {key} must be a UTC time like 2025-03-01T09:30:00Z")
    try:
        return dt.datetime.strptime(value, DATE_FORMAT)
    except ValueError:
        raise SchemaError(f"{where}: {key} is not a real time: {value}") from None


def _names(obj: dict[str, object], key: str, where: str) -> tuple[str, ...]:
    found: list[str] = []
    for i, item in enumerate(_array(obj, key, where)):
        if not isinstance(item, str) or not item.strip() or item != item.strip():
            raise SchemaError(f"{where}: {key}[{i}] must be a name")
        found.append(item)
    _unique(found, f"{where}: {key} names")
    return tuple(found)


def _unique(names: Iterable[str], what: str) -> None:
    """Names compared as the database compares them: without case."""
    seen: set[str] = set()
    for name in names:
        folded = name.casefold()
        if folded in seen:
            raise SchemaError(f"{what} {name} appears twice")
        seen.add(folded)


def _queue_spec(value: object, where: str) -> QueueSpec:
    obj = _object(value, where)
    name = _text(obj, "name", where, limit=NAME_MAX)
    if "existing" in obj:
        if obj["existing"] is not True:
            raise SchemaError(f"{where}: existing may only be true")
        return QueueSpec(name=name, existing=True)
    return QueueSpec(
        name=name,
        description=_text(obj, "description", where, limit=DESCRIPTION_MAX, required=False),
        subject_tag=_nullable_text(obj, "subject_tag", where, limit=SUBJECT_TAG_MAX),
    )


def _user_spec(value: object, where: str) -> UserSpec:
    obj = _object(value, where)
    email = _text(obj, "email", where, limit=EMAIL_MAX)
    if not users.looks_like_email(email):
        raise SchemaError(f"{where}: {email} does not look like an email address")
    return UserSpec(
        name=_text(obj, "name", where, limit=NAME_MAX),
        email=email,
        real_name=_text(obj, "real_name", where, limit=REAL_NAME_MAX, required=False),
        privileged=_flag(obj, "privileged", where),
    )


def _group_spec(value: object, where: str) -> GroupSpec:
    obj = _object(value, where)
    return GroupSpec(
        name=_text(obj, "name", where, limit=NAME_MAX),
        description=_text(obj, "description", where, limit=DESCRIPTION_MAX, required=False),
        members=_names(obj, "members", where),
    )


def _field_spec(value: object, where: str) -> FieldSpec:
    obj = _object(value, where)
    return FieldSpec(
        name=_text(obj, "name", where, limit=NAME_MAX),
        description=_text(obj, "description", where, limit=DESCRIPTION_MAX, required=False),
        applies_to=_names(obj, "applies_to", where),
    )


def _grant_spec(value: object, where: str) -> GrantSpec:
    obj = _object(value, where)
    kind = _text(obj, "kind", where)
    if kind not in (PrincipalKind.USER.value, PrincipalKind.GROUP.value):
        raise SchemaError(f"{where}: kind must be user or group")
    queue = _nullable_text(obj, "queue", where, limit=NAME_MAX)
    names = _names(obj, "rights", where)
    if not names:
        raise SchemaError(f"{where}: rights is empty")
    for right in names:
        if right not in RIGHTS:
            raise SchemaError(f"{where}: {right} is not a right")
        if queue is not None and right in GLOBAL_ONLY_RIGHTS:
            raise SchemaError(f"{where}: {right} can only be granted globally (queue null)")
    return GrantSpec(
        principal=_text(obj, "principal", where, limit=NAME_MAX),
        kind=kind,
        queue=queue,
        rights=names,
    )


def _ticket_spec(value: object, where: str, position: int) -> TicketSpec:
    obj = _object(value, where)
    number = _integer(obj, "id", where)
    if number != position:
        raise SchemaError(f"{where}: id is {number}; tickets are numbered 1..N in file order")
    requestor = _text(obj, "requestor", where, limit=EMAIL_MAX)
    if not users.looks_like_email(requestor):
        raise SchemaError(f"{where}: requestor {requestor} does not look like an email address")
    created = _date(obj, "created", where)
    steps: list[Step] = []
    previous = created
    for i, item in enumerate(_array(obj, "steps", where)):
        step = _step(item, f"{where}.steps[{i}]")
        if step.at <= previous:
            raise SchemaError(
                f"{where}.steps[{i}]: at is not after "
                + ("the ticket's created" if i == 0 else "the step before it")
            )
        previous = step.at
        steps.append(step)
    owner = _nullable_text(obj, "owner", where, limit=NAME_MAX)
    return TicketSpec(
        id=number,
        queue=_text(obj, "queue", where, limit=NAME_MAX),
        subject=_text(obj, "subject", where, limit=SUBJECT_MAX),
        content=_text(obj, "content", where, required=False, trimmed=False),
        requestor=requestor,
        owner=owner,
        creator=_text(obj, "creator", where, limit=NAME_MAX),
        created=created,
        steps=tuple(steps),
    )


def _step(value: object, where: str) -> Step:
    obj = _object(value, where)
    kind = _text(obj, "type", where)
    at = _date(obj, "at", where)
    actor = _text(obj, "actor", where, limit=NAME_MAX)
    if kind in (CORRESPOND, COMMENT):
        return Step(kind, at, actor, content=_text(obj, "content", where, trimmed=False))
    if kind == STATUS:
        status = _text(obj, "status", where)
        if status not in STEP_STATUSES:
            raise SchemaError(f"{where}: status must be one of {', '.join(STEP_STATUSES)}")
        return Step(kind, at, actor, status=status)
    if kind == CUSTOM_FIELD:
        return Step(
            kind,
            at,
            actor,
            field=_text(obj, "field", where, limit=NAME_MAX),
            value=_text(obj, "value", where, limit=VALUE_MAX),
        )
    raise SchemaError(f"{where}: type must be one of {', '.join(STEP_TYPES)}")


# --- loading ----------------------------------------------------------------------


def load(db: Session, settings: Settings, raw: bytes) -> str:
    """Load the dataset in ``raw`` into an empty side; return the checksum line.

    The side must already be migrated and seeded (the command does both
    first). :class:`Refused` when it is not empty enough, before anything is
    written; :class:`PartlyLoaded` when a write fails after others went in;
    :class:`Absent` when the database does not verify afterwards.
    """
    dataset = parse(raw)
    _guard(db, dataset)
    with silenced_hooks():
        try:
            _write(db, settings, dataset)
        except Exception as exc:
            with suppress(Exception):  # a lost connection must not hide the reason
                db.rollback()
            reason = str(exc) if isinstance(exc, FixtureError) else summary(exc)
            raise PartlyLoaded(reason) from exc
    return line(dataset, verify(db, dataset), digest(raw))


def check(db: Session, raw: bytes) -> str:
    """Verify the side holds the dataset in ``raw``; return the line. Writes nothing."""
    dataset = parse(raw)
    return line(dataset, verify(db, dataset), digest(raw))


@contextmanager
def silenced_hooks() -> Iterator[None]:
    """Empty the four hook lists for the load, and put them back after it.

    In place: ``hooks.fire`` and the mail package hold the list objects
    themselves, so rebinding a module name would silence nothing.
    """
    lists = [hooks.listeners(name) for name in _HOOKS]
    saved = [list(listeners) for listeners in lists]
    for listeners in lists:
        listeners.clear()
    try:
        yield
    finally:
        for listeners, kept in zip(lists, saved, strict=True):
            listeners[:] = kept


def _guard(db: Session, dataset: Dataset) -> None:
    """Refuse, before any write, a side the dataset cannot land on as numbered."""
    held = db.scalar(select(func.count()).select_from(Ticket)) or 0
    if held:
        raise Refused(f"the tickets table is not empty ({held} ticket{'' if held == 1 else 's'})")
    for queue in dataset.queues:
        found = find_queue(db, queue.name)
        if queue.existing and (found is None or found.name != queue.name):
            raise Refused(f"queue {queue.name} is not on this side")
        if not queue.existing and found is not None:
            raise Refused(f"queue {queue.name} already exists")
    for person in dataset.users:
        if not users.name_is_free(db, person.name):
            raise Refused(f"user {person.name} already exists")
        if tickets.user_by_email(db, person.email) is not None:
            raise Refused(f"a user already has the address {person.email}")
    for group in dataset.groups:
        if not groups.name_is_free(db, group.name):
            raise Refused(f"group {group.name} already exists")
    for custom in dataset.custom_fields:
        if customfields.refusal(db, customfields.FieldForm(name=custom.name)):
            raise Refused(f"custom field {custom.name} already exists")

    # A name the file uses but does not create must be on the side already
    # (root as an actor, Everyone as a grantee): a reference to nobody is the
    # file's mistake, found before the first write rather than after it.
    own_users = {person.name for person in dataset.users}
    own_emails = {person.email.casefold() for person in dataset.users}
    own_groups = {group.name for group in dataset.groups}
    wanted_users: dict[str, str] = {}
    wanted_groups: dict[str, str] = {}
    for group in dataset.groups:
        for member in group.members:
            wanted_users.setdefault(member, f"group {group.name}'s member")
    for grant in dataset.rights:
        into = wanted_users if grant.kind == PrincipalKind.USER else wanted_groups
        into.setdefault(grant.principal, "a grant's principal")
    for ticket in dataset.tickets:
        wanted_users.setdefault(ticket.creator, f"ticket {ticket.id}'s creator")
        if ticket.owner is not None:
            wanted_users.setdefault(ticket.owner, f"ticket {ticket.id}'s owner")
        for step in ticket.steps:
            wanted_users.setdefault(step.actor, f"a step's actor on ticket {ticket.id}")
        if ticket.requestor.casefold() not in own_emails and (
            tickets.user_by_email(db, ticket.requestor) is None
        ):
            raise FixtureError(
                f"ticket {ticket.id}'s requestor {ticket.requestor} is no user's address, "
                "in the file or on this side"
            )
    for name, role in wanted_users.items():
        if name not in own_users and find_user(db, name) is None:
            raise FixtureError(f"{role} {name} is not a user, in the file or on this side")
    for name, role in wanted_groups.items():
        if name not in own_groups and find_group(db, name) is None:
            raise FixtureError(f"{role} {name} is not a group, in the file or on this side")

    # A grantee the side already has must be one its rights pages offer: a
    # privileged, enabled user, or an enabled group (the file's own are).
    for grant in dataset.rights:
        if grant.kind == PrincipalKind.USER and grant.principal not in own_users:
            grantee = find_user(db, grant.principal)
            if grantee is not None and (not grantee.privileged or grantee.disabled):
                raise FixtureError(
                    f"the grant to {grant.principal}: only a privileged, enabled user holds rights"
                )
        if grant.kind == PrincipalKind.GROUP and grant.principal not in own_groups:
            team = find_group(db, grant.principal)
            if team is not None and team.disabled:
                raise FixtureError(f"the grant to {grant.principal}: the group is disabled")


@dataclass(slots=True)
class _Rows:
    """The rows the load made or found, by the names the file uses for them."""

    db: Session
    users: dict[str, User] = field(default_factory=dict)
    queues: dict[str, Queue] = field(default_factory=dict)
    fields: dict[str, CustomField] = field(default_factory=dict)

    def user(self, name: str) -> User:
        found = self.users.get(name)
        if found is None:
            found = find_user(self.db, name)
            if found is None:
                raise FixtureError(f"no user is named {name}")
            self.users[name] = found
        return found

    def queue(self, name: str) -> Queue:
        found = self.queues.get(name)
        if found is None:
            found = find_queue(self.db, name)
            if found is None:
                raise FixtureError(f"no queue is named {name}")
            self.queues[name] = found
        return found


def _write(db: Session, settings: Settings, dataset: Dataset) -> None:
    """Every object of the file, in the order the references need them."""
    rows = _Rows(db)
    unowned = tickets.nobody(db)
    if unowned is None:
        raise FixtureError("this side has no Nobody user: it was never seeded")
    root = find_user(db, ROOT_USER_NAME)

    for person in dataset.users:
        form = users.UserForm(
            name=person.name,
            email=person.email,
            real_name=person.real_name,
            privileged=person.privileged,
            enabled=True,
        )
        problem = users.refusal(db, form)
        if problem:
            raise FixtureError(f"user {person.name}: {problem}")
        rows.users[person.name] = users.create_user(db, form, password="")

    for queue in dataset.queues:
        if queue.existing:
            rows.queue(queue.name)  # found by the guard; used as it is
            continue
        fields = queues.QueueFields(
            name=queue.name, description=queue.description, subject_tag=queue.subject_tag or ""
        )
        problem = queues.name_problem(db, fields)
        if problem:
            raise FixtureError(f"queue {queue.name}: {problem}")
        rows.queues[queue.name] = queues.create_queue(db, fields)

    for spec in dataset.groups:
        group_form = groups.GroupForm(name=spec.name, description=spec.description)
        problem = groups.refusal(db, group_form)
        if problem:
            raise FixtureError(f"group {spec.name}: {problem}")
        group = groups.create_group(db, group_form)
        for member in spec.members:
            person_row = groups.addable_user(db, rows.user(member).id)
            if person_row is None or not groups.add_member(db, group, person_row):
                raise FixtureError(f"group {spec.name}: {member} cannot be added")

    for custom in dataset.custom_fields:
        field_form = customfields.FieldForm(name=custom.name, description=custom.description)
        problem = customfields.refusal(db, field_form)
        if problem:
            raise FixtureError(f"custom field {custom.name}: {problem}")
        made = customfields.create_field(db, field_form)
        rows.fields[custom.name] = made
        boxes = customfields.queue_boxes(db, made.id)
        ticked = frozenset(box.control("queue") for box in boxes if box.name in custom.applies_to)
        added, _ = customfields.save_applies_to(db, made, boxes, ticked)
        if added != len(custom.applies_to):
            raise FixtureError(
                f"custom field {custom.name}: applied to {added} of its "
                f"{len(custom.applies_to)} queues (a disabled queue is not offered)"
            )

    _grant(db, dataset, rows, root.id if root is not None else None)

    for ticket in dataset.tickets:
        _ticket(db, settings, ticket, rows, unowned)


def _grant(db: Session, dataset: Dataset, rows: _Rows, actor_id: int | None) -> None:
    """The rights, one Save per page, as a person ticking boxes would save them.

    ``save_rights`` makes the ticked boxes the truth for every row it is
    handed, so each page's sections are narrowed to the principals the file
    names on that object: the rows the file does not name are never handed
    in and never touched (root's ``SuperUser`` on the global User Rights
    page is one of them). A named principal's own boxes are posted ticked
    too, so the load only ever adds.
    """
    pages: dict[tuple[str | None, str], dict[str, tuple[str, ...]]] = {}
    for grant in dataset.rights:
        pages.setdefault((grant.queue, grant.kind), {})[grant.principal] = grant.rights

    for (queue_name, kind), wanted in pages.items():
        queue_id = None if queue_name is None else rows.queue(queue_name).id
        where = "the global" if queue_name is None else f"{queue_name}'s"
        sections = (
            rights.group_sections(db, queue_id)
            if kind == PrincipalKind.GROUP
            else rights.user_sections(db, queue_id)
        )
        named = [
            rights.Section(
                section.title,
                tuple(row for row in section.rows if row.name in wanted),
                section.empty_message,
            )
            for section in sections
        ]
        offered = [row for section in named for row in section.rows]
        for principal in wanted:
            if not any(row.name == principal for row in offered):
                raise FixtureError(
                    f"{where} {kind} rights page offers no {principal} "
                    "(a user must be privileged and enabled, a group enabled)"
                )
        posted = frozenset(
            row.control(right) for row in offered for right in (*wanted[row.name], *row.checked)
        )
        _, removed = rights.save_rights(
            db,
            named,
            rights.rights_offered(queue_id),
            queue_id,
            posted,
            principal_kind=kind,
            actor_id=actor_id,
        )
        if removed:  # pragma: no cover - every held box is posted ticked
            raise FixtureError(f"{where} {kind} rights: the save removed {removed} grants")


def _ticket(db: Session, settings: Settings, spec: TicketSpec, rows: _Rows, unowned: User) -> None:
    """One ticket through the create form's write, then each step through its page's."""
    queue = rows.queue(spec.queue)
    creator = rows.user(spec.creator)
    owner = unowned if spec.owner is None else rows.user(spec.owner)
    form = tickets.TicketForm(
        queue=queue.id,
        status="new",
        owner=owner.id,
        requestors=spec.requestor,
        subject=spec.subject,
        content=spec.content,
    )
    problem = tickets.refusal(db, form, queue)
    if problem == tickets.BAD_OWNER:
        problem = f"{problem} ({owner.name} in {queue.name})"
    if problem:
        raise FixtureError(f"ticket {spec.id}: {problem}")
    ticket = tickets.create_ticket(db, settings, creator, queue, form)
    if ticket.id != spec.id:
        raise FixtureError(
            f"ticket {spec.id} was written as #{ticket.id}: the tickets table was not "
            "empty, or its ids do not start at 1"
        )
    ticket.created = spec.created
    mark = _stamp(db, ticket, spec.created, creator, 0, (None, None))

    for number, step in enumerate(spec.steps, start=1):
        actor = rows.user(step.actor)
        before = (ticket.started, ticket.resolved)
        problem = _apply(db, settings, ticket, actor, step, rows)
        if problem:
            raise FixtureError(f"ticket {spec.id} step {number} ({step.type}): {problem}")
        mark = _stamp(db, ticket, step.at, actor, mark, before)


def _apply(
    db: Session, settings: Settings, ticket: Ticket, actor: User, step: Step, rows: _Rows
) -> str:
    """One step, written by the service its page calls; a refusal comes back as text."""
    if step.type in (CORRESPOND, COMMENT):
        kind = update.RESPOND if step.type == CORRESPOND else update.COMMENT
        return update.update_ticket(
            db, settings, ticket, actor, update.UpdateForm(update_type=kind, content=step.content)
        )
    if step.type == STATUS:
        if ticket.status == step.status:
            # The product moved it already (a reply opens a new ticket): the
            # step is applied, and there is nothing left to write.
            return ""
        # A status change with no message: the update page's Status select
        # alone, which writes the Status row and nothing else.
        return update.update_ticket(
            db,
            settings,
            ticket,
            actor,
            update.UpdateForm(update_type=update.COMMENT, status=step.status, content=""),
        )
    target = rows.fields[step.field]
    if customfields.apply_values(db, ticket, actor, {target.id: step.value}) != 1:
        return f"{step.field} was not set: its queue does not carry it, or the value is unchanged"
    db.commit()
    return ""


def _stamp(
    db: Session,
    ticket: Ticket,
    at: dt.datetime,
    actor: User,
    mark: int,
    before: tuple[dt.datetime | None, dt.datetime | None],
) -> int:
    """Date the last write with the file's time; return the new high-water mark.

    Every transaction of this ticket above ``mark`` is the write's (a reply
    that opened a ``new`` ticket wrote two), and each gets ``at``. The
    ticket's ``last_updated`` becomes ``at``, and ``started`` or ``resolved``
    too when the write set it. ``created`` is never moved by a service after
    the create, so the ticket's stays the file's.
    """
    written = db.scalars(
        select(Transaction)
        .where(Transaction.ticket_id == ticket.id, Transaction.id > mark)
        .order_by(Transaction.id)
    ).all()
    for row in written:
        row.created = at
        mark = row.id
    started, resolved = before
    if ticket.started is not None and ticket.started != started:
        ticket.started = at
    if ticket.resolved is not None and ticket.resolved != resolved:
        ticket.resolved = at
    ticket.last_updated = at
    ticket.last_updated_by = actor.id
    db.commit()
    return mark


# --- verifying --------------------------------------------------------------------


def verify(db: Session, dataset: Dataset) -> Counts:
    """Find every object of the dataset in the database; :class:`Absent` if one is not.

    Queues, users, groups and custom fields by name, each membership, each
    grant as its exact row and each "applies to" as its row; tickets 1..N
    with their subject, queue, final status, owner, requestor and custom
    field values; and the ``Create``, ``Correspond``, ``Comment`` and
    ``CustomField`` transactions on tickets 1..N counted against the file's
    steps (neither side writes those four implicitly). ``status`` is the
    file's count, proven by the final statuses: a side may add status rows
    of its own. ``last_ticket`` is the highest ticket id, whatever wrote it.
    """
    db.expire_all()  # read the database, not what this session remembers

    queue_ids: dict[str, int] = {}
    for queue in dataset.queues:
        found = find_queue(db, queue.name)
        if found is None or found.name != queue.name:
            raise Absent(f"queue {queue.name} is missing")
        queue_ids[queue.name] = found.id

    user_ids: dict[str, int] = {}

    def user_id(name: str) -> int | None:
        if name not in user_ids:
            found = find_user(db, name)
            if found is None or found.name != name:
                return None
            user_ids[name] = found.id
        return user_ids[name]

    for person in dataset.users:
        if user_id(person.name) is None:
            raise Absent(f"user {person.name} is missing")

    group_ids: dict[str, int] = {}
    members = 0
    for team in dataset.groups:
        group = find_group(db, team.name)
        if group is None or group.name != team.name or group.kind != GroupKind.USER_DEFINED:
            raise Absent(f"group {team.name} is missing")
        group_ids[team.name] = group.id
        for member in team.members:
            member_id = user_id(member)
            if member_id is None or db.get(GroupMember, (group.id, member_id)) is None:
                raise Absent(f"{member} is not a member of {team.name}")
            members += 1

    held = set(
        db.execute(
            select(
                Right.principal_kind,
                Right.principal_id,
                Right.right_name,
                Right.object_kind,
                Right.object_id,
            )
        ).tuples()
    )
    granted = 0
    for grant in dataset.rights:
        if grant.kind == PrincipalKind.USER:
            principal_id = user_id(grant.principal)
        else:
            if grant.principal not in group_ids:
                found_group = find_group(db, grant.principal)
                if found_group is not None:
                    group_ids[grant.principal] = found_group.id
            principal_id = group_ids.get(grant.principal)
        if principal_id is None:
            raise Absent(f"{grant.kind} {grant.principal} is missing")
        object_kind, object_id = rights.object_of(
            None if grant.queue is None else queue_ids[grant.queue]
        )
        for right in grant.rights:
            if (grant.kind, principal_id, right, object_kind, object_id) not in held:
                raise Absent(
                    f"{grant.principal} does not hold {right} on {grant.queue or 'the system'}"
                )
            granted += 1

    field_ids: dict[str, int] = {}
    carried = set(
        db.execute(select(QueueCustomField.queue_id, QueueCustomField.custom_field_id)).tuples()
    )
    applied = 0
    for custom in dataset.custom_fields:
        made = db.scalar(select(CustomField).where(CustomField.name == custom.name))
        if made is None or made.name != custom.name:
            raise Absent(f"custom field {custom.name} is missing")
        field_ids[custom.name] = made.id
        for queue_name in custom.applies_to:
            if (queue_ids[queue_name], made.id) not in carried:
                raise Absent(f"custom field {custom.name} does not apply to {queue_name}")
            applied += 1

    count = len(dataset.tickets)
    unowned = tickets.nobody(db)
    if unowned is None:
        raise Absent("this side has no Nobody user")
    stored = {row.id: row for row in db.scalars(select(Ticket).where(Ticket.id <= count))}
    requestors: dict[int, set[str]] = {}
    for ticket_id, email in db.execute(
        select(TicketWatcher.ticket_id, User.email)
        .join(User, User.id == TicketWatcher.user_id)
        .where(TicketWatcher.ticket_id <= count, TicketWatcher.role == WatcherRole.REQUESTOR)
    ).tuples():
        requestors.setdefault(ticket_id, set()).add((email or "").casefold())
    values = {
        (ticket_id, field_id): value
        for ticket_id, field_id, value in db.execute(
            select(
                TicketCustomFieldValue.ticket_id,
                TicketCustomFieldValue.custom_field_id,
                TicketCustomFieldValue.value,
            ).where(TicketCustomFieldValue.ticket_id <= count)
        ).tuples()
    }
    for expected in dataset.tickets:
        row = stored.get(expected.id)
        if row is None:
            raise Absent(f"ticket {expected.id} is missing")
        if row.subject != expected.subject:
            raise Absent(f"ticket {expected.id}'s subject is not {expected.subject!r}")
        if row.queue_id != queue_ids[expected.queue]:
            raise Absent(f"ticket {expected.id} is not in {expected.queue}")
        if row.status != expected.final_status:
            raise Absent(f"ticket {expected.id} is {row.status}, not {expected.final_status}")
        owner_id = unowned.id if expected.owner is None else user_id(expected.owner)
        if row.owner_id != owner_id:
            raise Absent(f"ticket {expected.id} is not owned by {expected.owner or 'Nobody'}")
        if expected.requestor.casefold() not in requestors.get(expected.id, set()):
            raise Absent(f"ticket {expected.id}'s requestor is not {expected.requestor}")
        for field_name, value in expected.final_values().items():
            if values.get((expected.id, field_ids[field_name])) != value:
                raise Absent(f"ticket {expected.id}'s {field_name} is not {value}")

    by_type: dict[str, int] = {
        str(kind): number
        for kind, number in db.execute(
            select(Transaction.type, func.count())
            .where(Transaction.ticket_id <= count)
            .group_by(Transaction.type)
        ).tuples()
    }
    written = {
        "create": (TransactionType.CREATE, count),
        "correspond": (TransactionType.CORRESPOND, dataset.steps_of(CORRESPOND)),
        "comment": (TransactionType.COMMENT, dataset.steps_of(COMMENT)),
        "custom_field": (TransactionType.CUSTOM_FIELD, dataset.steps_of(CUSTOM_FIELD)),
    }
    for kind, (transaction_type, from_file) in written.items():
        found_rows = by_type.get(transaction_type.value, 0)
        if found_rows != from_file:
            raise Absent(
                f"tickets 1-{count} hold {found_rows} {transaction_type.value} transactions; "
                f"the file makes {from_file} ({kind})"
            )

    return Counts(
        queues=len(dataset.queues),
        users=len(dataset.users),
        groups=len(dataset.groups),
        members=members,
        rights=granted,
        custom_fields=len(dataset.custom_fields),
        applied=applied,
        tickets=count,
        create=by_type.get(TransactionType.CREATE.value, 0),
        correspond=by_type.get(TransactionType.CORRESPOND.value, 0),
        comment=by_type.get(TransactionType.COMMENT.value, 0),
        status=dataset.steps_of(STATUS),
        custom_field=by_type.get(TransactionType.CUSTOM_FIELD.value, 0),
        last_ticket=db.scalar(select(func.max(Ticket.id))) or 0,
    )


__all__ = [
    "SCHEMA",
    "Absent",
    "Counts",
    "Dataset",
    "FixtureError",
    "PartlyLoaded",
    "Refused",
    "SchemaError",
    "check",
    "digest",
    "line",
    "load",
    "parse",
    "silenced_hooks",
    "summary",
    "verify",
]
