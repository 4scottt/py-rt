"""The simple search (FP S01-S05, and FP R06's search clause).

Plan §8's ``/search?q=`` and §10's grammar: words that must all match, one
option word (``status:any``) and a bare ticket number that is a ticket, not
a search. The gate is ``ShowTicket``, applied in SQL by the queues the user
may see plus the two roles a ticket can give it, so the table never shows a
row the ticket page would refuse.

The grammar half needs no database and is tested as pure functions; the rest
goes through the app, because the filter is the point and it lives in SQL.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import event
from sqlalchemy.orm import Session

from pyrt.db.models import (
    Queue,
    Ticket,
    TicketWatcher,
    TransactionType,
    User,
    WatcherRole,
)
from pyrt.search import service
from pyrt.search.router import NO_RESULTS, RESULTS_HEADING
from pyrt.tickets import transactions
from tests.test_acl import grant, group_id, make_queue
from tests.test_auth import make_user, sign_in
from tests.test_queues import fresh
from tests.test_tickets import general, make_ticket, nobody, root

AGENT = "searcher"
AGENT_PASSWORD = "searcher-password"

SHOW_TICKET = "ShowTicket"


# --- helpers ---------------------------------------------------------------


def agent_user(db: Session) -> User:
    """A privileged user holding nothing at all until a test grants it."""
    return make_user(db, AGENT, AGENT_PASSWORD, privileged=True)


def as_agent(client: TestClient) -> None:
    sign_in(client, AGENT, AGENT_PASSWORD)


def say(db: Session, ticket: Ticket, actor: User, body: str) -> None:
    """A Correspond transaction with that body, the way a reply writes one."""
    transactions.record(db, ticket, TransactionType.CORRESPOND, actor, body=body)
    db.commit()


def own(db: Session, ticket: Ticket, user: User) -> None:
    ticket.owner_id = user.id
    db.commit()


def ask(db: Session, ticket: Ticket, user: User) -> None:
    """Make ``user`` a requestor of the ticket (the Requestor role)."""
    db.add(TicketWatcher(ticket_id=ticket.id, user_id=user.id, role=WatcherRole.REQUESTOR))
    db.commit()


def search(client: TestClient, q: str) -> Response:
    return client.get("/search", params={"q": q}, follow_redirects=False)


def rows_of(page: Response) -> str:
    """The results table alone, so the shell's links are not mistaken for hits."""
    start = page.text.index('id="search-results"')
    return page.text[start:]


def shown(page: Response, ticket: Ticket) -> bool:
    return f'/ticket/{ticket.id}"' in rows_of(page)


# --- S01: the words --------------------------------------------------------


def test_fp_s01_words_match_the_subject_and_a_message_body(client: TestClient, db: Session) -> None:
    """Every word must match, in the subject or in any message of the ticket."""
    queue = general(db)
    actor = root(db)
    printer = make_ticket(db, queue, actor, subject="Printer is on fire")
    laptop = make_ticket(db, queue, actor, subject="Laptop will not boot")
    say(db, laptop, actor, "The printer in the corner is fine, the laptop is not.")
    sign_in(client)

    # A subject word finds one; the same word in a body finds the other too.
    both = search(client, "printer")
    assert both.status_code == 200
    assert shown(both, printer) and shown(both, laptop)

    # Case is nothing: the collation is utf8mb4_unicode_ci.
    assert shown(search(client, "PRINTER"), printer)

    # Two words is an AND, over subject and body together.
    narrowed = search(client, "printer corner")
    assert shown(narrowed, laptop)
    assert not shown(narrowed, printer)

    # A word nothing holds is no rows, not every row.
    assert not shown(search(client, "printer aardvark"), printer)


def test_fp_s01_a_lone_integer_opens_that_ticket(client: TestClient, db: Session) -> None:
    """A bare number is the ticket, not a search (plan §10)."""
    ticket = make_ticket(db, general(db), root(db), subject="Printer is on fire")
    sign_in(client)

    jump = search(client, f" {ticket.id} ")
    assert jump.status_code == 303
    assert jump.headers["location"].endswith(f"/ticket/{ticket.id}")

    # A number beside a word is a word: "12 printer" is a search.
    assert search(client, f"{ticket.id} printer").status_code == 200


# --- S02: the status filter ------------------------------------------------


def test_fp_s02_resolved_is_hidden_until_status_any(client: TestClient, db: Session) -> None:
    """The goal's own search: "Printer status:any" finds the resolved one."""
    queue = general(db)
    actor = root(db)
    open_one = make_ticket(db, queue, actor, subject="Printer is jammed", status="open")
    done = make_ticket(db, queue, actor, subject="Printer is on fire", status="resolved")
    rejected = make_ticket(db, queue, actor, subject="Printer smells", status="rejected")
    deleted = make_ticket(db, queue, actor, subject="Printer is gone", status="deleted")
    sign_in(client)

    default = search(client, "Printer")
    assert shown(default, open_one)
    for hidden in (done, rejected, deleted):
        assert not shown(default, hidden), hidden.subject

    every = search(client, "Printer status:any")
    for ticket in (open_one, done, rejected, deleted):
        assert shown(every, ticket), ticket.subject

    # The bonus the grammar gets for free: a named status narrows to it.
    only_resolved = search(client, "Printer status:resolved")
    assert shown(only_resolved, done)
    assert not shown(only_resolved, open_one)


# --- S03: the options ------------------------------------------------------


def test_fp_s03_an_unknown_option_is_ignored_and_an_empty_query_lists_nothing() -> None:
    """The grammar alone (no database): tokens, options, the lone integer."""
    assert service.parse("foo:bar").words == ()
    assert service.parse("foo:bar").is_empty
    assert service.parse("printer foo:bar").words == ("printer",)
    assert service.parse("printer status:any").status == service.STATUS_ANY
    assert service.parse("printer status:open").status == "open"
    assert service.parse("printer status:nonsense").status == ""
    assert service.parse("  ").is_empty
    assert service.parse("").words == ()
    assert service.parse("42").ticket_id == 42
    assert service.parse("42 printer").ticket_id is None
    assert service.parse("0").ticket_id is None


def test_fp_s03_an_unknown_option_is_not_an_error(client: TestClient, db: Session) -> None:
    """Through the app: a strange token searches for the words beside it."""
    ticket = make_ticket(db, general(db), root(db), subject="Printer is on fire")
    sign_in(client)

    odd = search(client, "printer foo:bar")
    assert odd.status_code == 200
    assert shown(odd, ticket)

    # An empty box is a page, not an error, and it lists nothing (FP S03, S05).
    empty = search(client, "")
    assert empty.status_code == 200
    assert not shown(empty, ticket)
    assert NO_RESULTS in empty.text


# --- S04: the table, its order and its gate --------------------------------


def test_fp_s04_the_results_table_has_the_six_columns_newest_first(
    client: TestClient, db: Session
) -> None:
    """Id, Subject, Status, Queue, Owner, Created; the newest ticket first."""
    queue = general(db)
    actor = root(db)
    older = make_ticket(db, queue, actor, subject="Printer one")
    newer = make_ticket(db, queue, actor, subject="Printer two")
    own(db, newer, actor)
    sign_in(client)

    page = search(client, "printer")
    assert page.status_code == 200
    assert RESULTS_HEADING in page.text
    for column in ("Id", "Subject", "Status", "Queue", "Owner", "Created"):
        assert f"<th>{column}</th>" in page.text, column
    table = rows_of(page)
    assert table.index(f'/ticket/{newer.id}"') < table.index(f'/ticket/{older.id}"')
    assert queue.name in table
    assert nobody(db).name in table  # the Owner column, unowned
    assert ">root</td>" in table  # and owned

    # The box comes back holding the query (the shell's input, plan §8).
    assert 'name="q"' in page.text
    assert 'value="printer"' in page.text


def test_fp_s04_show_ticket_is_applied_in_sql_by_queue(client: TestClient, db: Session) -> None:
    """A user sees the queues it may ShowTicket in, and no others."""
    support = make_queue(db, "Support")
    actor = root(db)
    mine = make_ticket(db, support, actor, subject="Printer in Support")
    theirs = make_ticket(db, general(db), actor, subject="Printer in General")
    agent = agent_user(db)
    grant(db, ("user", agent.id), SHOW_TICKET, queue=support)
    as_agent(client)

    page = search(client, "printer")
    assert page.status_code == 200
    assert shown(page, mine)
    assert not shown(page, theirs)


def test_fp_s04_no_show_ticket_anywhere_is_an_empty_table(client: TestClient, db: Session) -> None:
    """A user granted nothing sees nothing, and the page still renders."""
    make_ticket(db, general(db), root(db), subject="Printer is on fire")
    agent_user(db)
    as_agent(client)

    page = search(client, "printer")
    assert page.status_code == 200
    assert NO_RESULTS in page.text


def test_fp_s04_super_user_sees_every_queue(client: TestClient, db: Session) -> None:
    """root holds SuperUser globally: no filter at all."""
    actor = root(db)
    here = make_ticket(db, general(db), actor, subject="Printer here")
    there = make_ticket(db, make_queue(db, "Support"), actor, subject="Printer there")
    sign_in(client)

    page = search(client, "printer")
    assert shown(page, here)
    assert shown(page, there)


# --- S05: the empty state --------------------------------------------------


def test_fp_s05_no_tickets_found(client: TestClient, db: Session) -> None:
    """A query that matches nothing says so, in the table (FP S05)."""
    make_ticket(db, general(db), root(db), subject="Printer is on fire")
    sign_in(client)

    page = search(client, "aardvark")
    assert page.status_code == 200
    assert RESULTS_HEADING in page.text
    assert NO_RESULTS in page.text


# --- R06: the gate's search clause -----------------------------------------


def test_fp_r06_show_ticket_gates_search_results(client: TestClient, db: Session) -> None:
    """The third half of R06 (the gates package left it to this one).

    ShowTicket reaches the results table the way it reaches the ticket page:
    through the queue, through the Owner role and through the Requestor
    role. A row the table shows is a row the ticket page opens; a row it
    hides is one the ticket page refuses.
    """
    queue = make_queue(db, "Support")
    actor = root(db)
    agent = agent_user(db)
    stranger = make_ticket(db, queue, actor, subject="Printer nobody told me about")
    owned = make_ticket(db, queue, actor, subject="Printer I own")
    asked = make_ticket(db, queue, actor, subject="Printer I asked about")
    own(db, owned, agent)
    ask(db, asked, agent)
    as_agent(client)

    # Nothing granted: nothing shown, and the ticket page refuses too.
    blind = search(client, "printer")
    assert not shown(blind, owned)
    assert client.get(f"/ticket/{owned.id}").status_code == 403

    # The Owner role on this queue: the owned one, and only it.
    grant(db, ("group", group_id(db, "Owner")), SHOW_TICKET, queue=queue)
    as_owner = search(client, "printer")
    assert shown(as_owner, owned)
    assert not shown(as_owner, stranger)
    assert not shown(as_owner, asked)
    assert client.get(f"/ticket/{owned.id}").status_code == 200

    # The Requestor role, globally this time: the one it wrote in about.
    grant(db, ("group", group_id(db, "Requestor")), SHOW_TICKET)
    as_both = search(client, "printer")
    assert shown(as_both, owned)
    assert shown(as_both, asked)
    assert not shown(as_both, stranger)

    # The queue itself: everything in it.
    grant(db, ("user", agent.id), SHOW_TICKET, queue=queue)
    all_three = search(client, "printer")
    for ticket in (stranger, owned, asked):
        assert shown(all_three, ticket), ticket.subject


# --- the query budget (plan §12) -------------------------------------------


def test_the_search_page_costs_two_queries(db: Session) -> None:
    """Beyond the principal set: where ShowTicket reaches, and the page.

    Plan §12's budget for a search page is two queries, and these are them:
    the grants that say where ``ShowTicket`` reaches (one query for the
    queues, the Owner role and the Requestor role together) and the results
    with their queue and owner joined. The principal set is the third, and
    it is the shell's on every page, already counted in M1.
    """
    queue: Queue = general(db)
    actor = root(db)
    make_ticket(db, queue, actor, subject="Printer is on fire")
    fresh(db)
    actor_id = actor.id  # read now: an expired row would be a query of its own
    held = frozenset({("user", actor_id)})
    query = service.parse("printer")

    counted: list[str] = []

    def count(conn: object, cursor: object, statement: str, *rest: object) -> None:
        counted.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", count)
    try:
        rows = service.results(db, held, query, actor_id)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", count)

    assert len(rows) == 1
    assert len(counted) == 2, counted
