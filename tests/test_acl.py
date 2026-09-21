"""The resolver and the grants (FP R01, R02, U07).

Plan §10: the principal set is one query and the memo of the request; a
right is one indexed query on ``rights``. Membership in the system groups is
a rule, never a row, and a disabled group or user falls out of the set
without a grant being deleted.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pyrt import acl
from pyrt.db.models import (
    DEFAULT_QUEUE_NAME,
    ROOT_USER_NAME,
    SYSTEM_GROUP_EVERYONE,
    SYSTEM_GROUP_PRIVILEGED,
    SYSTEM_GROUP_UNPRIVILEGED,
    SYSTEM_GROUPS,
    Group,
    GroupKind,
    GroupMember,
    ObjectKind,
    PrincipalKind,
    Queue,
    Right,
    Ticket,
    TicketWatcher,
    User,
    WatcherRole,
    utcnow,
)
from tests.test_auth import make_user

SHOW_TICKET = "ShowTicket"


def group_id(db: Session, name: str) -> int:
    found = db.scalar(select(Group.id).where(Group.name == name))
    assert found is not None, name
    return found


def make_group(db: Session, name: str, *, disabled: bool = False) -> Group:
    group = Group(name=name, description=name, kind=GroupKind.USER_DEFINED, disabled=disabled)
    db.add(group)
    db.commit()
    return group


def add_member(db: Session, group: Group, user: User) -> None:
    db.add(GroupMember(group_id=group.id, user_id=user.id))
    db.commit()


def make_queue(db: Session, name: str) -> Queue:
    queue = Queue(name=name, description=name)
    db.add(queue)
    db.commit()
    return queue


def grant(
    db: Session,
    principal: tuple[str, int],
    right: str,
    *,
    queue: Queue | None = None,
) -> None:
    kind, ident = principal
    db.add(
        Right(
            principal_kind=kind,
            principal_id=ident,
            right_name=right,
            object_kind=ObjectKind.QUEUE if queue else ObjectKind.SYSTEM,
            object_id=queue.id if queue else 0,
        )
    )
    db.commit()


def make_ticket(db: Session, queue: Queue, owner: User) -> Ticket:
    now = utcnow()
    ticket = Ticket(
        queue_id=queue.id,
        owner_id=owner.id,
        subject="a ticket",
        status="new",
        priority=0,
        created=now,
        last_updated=now,
        creator_id=owner.id,
        last_updated_by=owner.id,
    )
    db.add(ticket)
    db.commit()
    return ticket


def test_fp_r01_effective_principals_of_a_user(db: Session) -> None:
    """The user, its enabled groups, Everyone and its privilege group."""
    root = db.scalar(select(User).where(User.name == ROOT_USER_NAME))
    assert root is not None

    everyone = group_id(db, SYSTEM_GROUP_EVERYONE)
    privileged = group_id(db, SYSTEM_GROUP_PRIVILEGED)
    unprivileged = group_id(db, SYSTEM_GROUP_UNPRIVILEGED)

    assert acl.principals(db, root) == {
        (PrincipalKind.USER.value, root.id),
        (PrincipalKind.GROUP.value, everyone),
        (PrincipalKind.GROUP.value, privileged),
    }

    # An unprivileged user gets Unprivileged, not Privileged.
    plain = make_user(db, "plain")
    assert acl.principals(db, plain) == {
        (PrincipalKind.USER.value, plain.id),
        (PrincipalKind.GROUP.value, everyone),
        (PrincipalKind.GROUP.value, unprivileged),
    }

    # A user-defined group joins the set while it is enabled.
    support = make_group(db, "Support")
    add_member(db, support, plain)
    assert (PrincipalKind.GROUP.value, support.id) in acl.principals(db, plain)

    support.disabled = True
    db.commit()
    assert (PrincipalKind.GROUP.value, support.id) not in acl.principals(db, plain)
    # The membership row is still there; only the set changed.
    assert db.scalar(select(func.count()).select_from(GroupMember)) == 1

    # A group the user is not in never joins.
    other = make_group(db, "Others")
    assert (PrincipalKind.GROUP.value, other.id) not in acl.principals(db, plain)

    # A disabled user holds nothing at all, not even Everyone.
    plain.disabled = True
    db.commit()
    assert acl.principals(db, plain) == acl.NO_PRINCIPALS
    assert acl.principals(db, None) == acl.NO_PRINCIPALS


def test_fp_r01_ticket_roles_join_the_set(db: Session) -> None:
    """Owner when the user owns it, Requestor when it is a requestor watcher."""
    owner = make_user(db, "owner")
    asker = make_user(db, "asker")
    bystander = make_user(db, "bystander")
    queue = db.scalar(select(Queue).where(Queue.name == DEFAULT_QUEUE_NAME))
    assert queue is not None
    ticket = make_ticket(db, queue, owner)
    db.add(TicketWatcher(ticket_id=ticket.id, user_id=asker.id, role=WatcherRole.REQUESTOR))
    db.commit()

    owner_group = (PrincipalKind.GROUP.value, group_id(db, acl.ROLE_OWNER))
    requestor_group = (PrincipalKind.GROUP.value, group_id(db, acl.ROLE_REQUESTOR))

    held = acl.ticket_principals(db, owner, ticket)
    assert owner_group in held
    assert requestor_group not in held

    held = acl.ticket_principals(db, asker, ticket)
    assert requestor_group in held
    assert owner_group not in held

    held = acl.ticket_principals(db, bystander, ticket)
    assert owner_group not in held
    assert requestor_group not in held
    assert held == acl.principals(db, bystander)

    assert acl.ticket_principals(db, None, ticket) == acl.NO_PRINCIPALS


def test_fp_r02_a_grant_is_on_a_queue_or_global_and_superuser_covers_all(
    db: Session,
) -> None:
    """A queue grant, a global grant, SuperUser, and a right nobody holds."""
    general = db.scalar(select(Queue).where(Queue.name == DEFAULT_QUEUE_NAME))
    assert general is not None
    other = make_queue(db, "Other")

    user = make_user(db, "agent")
    held = acl.principals(db, user)

    assert acl.has_right(db, held, SHOW_TICKET, general.id) is False

    grant(db, (PrincipalKind.USER.value, user.id), SHOW_TICKET, queue=general)
    assert acl.has_right(db, held, SHOW_TICKET, general.id) is True
    assert acl.has_right(db, held, SHOW_TICKET, other.id) is False
    assert acl.has_right(db, held, SHOW_TICKET) is False, "a queue grant is not global"
    assert acl.has_right(db, held, "ModifyTicket", general.id) is False

    # A global grant covers every queue, and the global object too.
    everyone = group_id(db, SYSTEM_GROUP_EVERYONE)
    grant(db, (PrincipalKind.GROUP.value, everyone), "CreateTicket")
    assert acl.has_right(db, held, "CreateTicket", general.id) is True
    assert acl.has_right(db, held, "CreateTicket", other.id) is True
    assert acl.has_right(db, held, "CreateTicket") is True

    # A grant through a group is the same as one to the user.
    support = make_group(db, "Support")
    add_member(db, support, user)
    grant(db, (PrincipalKind.GROUP.value, support.id), "ModifyTicket", queue=other)
    assert acl.has_right(db, acl.principals(db, user), "ModifyTicket", other.id) is True

    # SuperUser covers every right on every object; root holds it from the seed.
    root = db.scalar(select(User).where(User.name == ROOT_USER_NAME))
    assert root is not None
    root_held = acl.principals(db, root)
    for right in ("ShowTicket", "AdminUsers", "ShowConfigTab", "ModifyTicket"):
        assert acl.has_right(db, root_held, right, other.id) is True
        assert acl.has_right(db, root_held, right) is True

    # A disabled user holds no principal, so no grant reaches them.
    user.disabled = True
    db.commit()
    assert acl.has_right(db, acl.principals(db, user), SHOW_TICKET, general.id) is False
    # The grant itself was not deleted (plan §10).
    assert (
        db.scalar(
            select(func.count())
            .select_from(Right)
            .where(Right.principal_kind == PrincipalKind.USER, Right.principal_id == user.id)
        )
        == 1
    )


def test_fp_u07_system_groups_are_membership_by_rule_never_by_row(db: Session) -> None:
    """The three exist from the seed, hold no rows, and a row changes nothing."""
    kinds = {
        name: db.scalar(select(Group.kind).where(Group.name == name)) for name in SYSTEM_GROUPS
    }
    assert kinds == dict.fromkeys(SYSTEM_GROUPS, GroupKind.SYSTEM.value)
    assert db.scalar(select(func.count()).select_from(GroupMember)) == 0

    plain = make_user(db, "plain")
    everyone = group_id(db, SYSTEM_GROUP_EVERYONE)
    privileged = group_id(db, SYSTEM_GROUP_PRIVILEGED)
    unprivileged = group_id(db, SYSTEM_GROUP_UNPRIVILEGED)

    # Everyone is in the set with no row to say so.
    assert (PrincipalKind.GROUP.value, everyone) in acl.principals(db, plain)

    # A row saying otherwise does not make the user privileged.
    db.add(GroupMember(group_id=privileged, user_id=plain.id))
    db.commit()
    held = acl.principals(db, plain)
    assert (PrincipalKind.GROUP.value, privileged) not in held
    assert (PrincipalKind.GROUP.value, unprivileged) in held

    # The flag is what decides it.
    plain.privileged = True
    db.commit()
    held = acl.principals(db, plain)
    assert (PrincipalKind.GROUP.value, privileged) in held
    assert (PrincipalKind.GROUP.value, unprivileged) not in held
