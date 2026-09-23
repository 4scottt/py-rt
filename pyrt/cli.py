"""The ``pyrt`` command: migrate | seed | serve | healthcheck | mailgate.

``serve`` is the image's default command and does the startup of plan §6:
migrate, seed, listen. ``mailgate`` is the mail gateway of FP M01: one
message on standard input, a ticket or a reply out of it, and the two-line
protocol the platform's exercise body reads. ``seed --fixture`` is FP O09:
a fixture dataset loaded through the services, and its one checksum line.
"""

from __future__ import annotations

import argparse
import logging
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

from pyrt import __version__
from pyrt import logging as pyrt_logging
from pyrt.config import ConfigError, Settings, load_settings
from pyrt.db import migrate as db_migrate
from pyrt.db.engine import make_engine, make_session_factory, session_scope
from pyrt.db.seed import seed as run_seed

log = logging.getLogger("pyrt.cli")

HEALTH_TIMEOUT = 5.0

#: The gateway's default action, and the two lines of its protocol (plan
#: §10). ``ok`` alone says the message was filed, ``Ticket: <id>`` says
#: which ticket it is on -- always, so the caller never has to go looking
#: in the database for it.
DEFAULT_ACTION = "correspond"
OK_LINE = "ok"
TICKET_LINE = "Ticket: {ticket}"
NOT_OK_LINE = "not ok: {reason}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyrt", description="py-rt: Request Tracker's ticket core"
    )
    parser.add_argument("--version", action="version", version=f"py-rt {__version__}")
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="root log level for this command (default INFO)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="upgrade the database to the latest revision")
    # FP O09: ``--fixture`` loads a dataset after the seed and prints its
    # checksum line; ``--check`` only verifies a loaded one, writing nothing.
    seed = sub.add_parser("seed", help="migrate, then seed a fresh database")
    seed.add_argument(
        "--fixture",
        metavar="PATH|-",
        help="then load this fixture dataset (- reads standard input); print its checksum line",
    )
    seed.add_argument(
        "--check",
        action="store_true",
        help="with --fixture: verify the database holds the dataset, print the line; write nothing",
    )
    sub.add_parser("serve", help="migrate, seed and serve")
    sub.add_parser("healthcheck", help="GET /health on the local port; exit 0 when it is 200")

    # FP M01: the argv shape the platform's exercise body sends, verbatim.
    # ``--url`` and ``--debug`` are accepted so that shape holds; this
    # gateway talks to the database, so there is no URL to call and no
    # second level of output to turn on.
    mailgate = sub.add_parser("mailgate", help="file one message read on standard input")
    mailgate.add_argument("--queue", required=True, help="the queue the message is filed in")
    mailgate.add_argument("--action", default=DEFAULT_ACTION, help="correspond or comment")
    mailgate.add_argument("--url", default="", help="accepted and ignored")
    mailgate.add_argument("--debug", action="store_true", help="accepted; the output is the same")
    return parser


def _migrate(settings: Settings) -> None:
    db_migrate.upgrade(settings.database_url())


def _seed(settings: Settings) -> bool:
    engine = make_engine(settings.database_url())
    try:
        with session_scope(make_session_factory(engine)) as session:
            return run_seed(session, settings.root_password)
    finally:
        engine.dispose()


def _serve(settings: Settings) -> int:
    import uvicorn

    _migrate(settings)
    _seed(settings)
    uvicorn.run(
        "pyrt.app:create_app",
        factory=True,
        host="0.0.0.0",  # a container listens on every interface
        port=settings.port,
        workers=settings.workers,
        access_log=True,
        # A worker is a *spawned* process (a fresh interpreter): the JSON
        # handler main() installed in this process is not inherited, and
        # log_config=None would leave the worker with no handler at all, so
        # nothing — not the mail sender's lines — would reach stdout. The
        # dict configures each worker the same way pyrt.logging.configure does.
        log_config=pyrt_logging.uvicorn_log_config(logging.getLogger().getEffectiveLevel()),
    )
    return 0


def _healthcheck(settings: Settings) -> int:
    url = f"http://127.0.0.1:{settings.port}/health"
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT) as response:
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, OSError, ValueError):
        return 1


def _logs_to_stderr() -> None:
    """Move the log lines off standard output.

    Standard output is the gateway's protocol and a program reads it: the
    JSON log lines, and any traceback behind a refusal, belong on standard
    error beside it.
    """
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout:
            handler.setStream(sys.stderr)


def _mailgate(
    settings: Settings, queue_name: str, action: str, stdin: BinaryIO | None = None
) -> int:
    """FP M01: read one message, file it, print the protocol, exit on it.

    Mail out is wired exactly as the app wires it, so a ticket opened by
    mail sends its autoreply through ``MAIL_MODE``'s sender (the log, by
    default). A reply arriving by mail notifies nobody: the actor is the
    sender and the correspond notification never writes back to the actor.

    Only the database settings and ``SITE_NAME`` are needed here --
    :meth:`Settings.require_serving` is the web server's check, and a
    message filed by hand should not want a session secret.
    """
    from pyrt.mail import gateway
    from pyrt.mail import notify as mail_notify
    from pyrt.mail import send as mail_send
    from pyrt.tickets import hooks as ticket_hooks

    _logs_to_stderr()
    mail_send.configure(mail_send.sender_from_settings(settings))
    mail_notify.register(ticket_hooks)

    raw = (stdin or sys.stdin.buffer).read()
    engine = make_engine(settings.database_url())
    try:
        with session_scope(make_session_factory(engine)) as session:
            result = gateway.deliver(
                session, settings, queue_name=queue_name, action=action, raw=raw
            )
    finally:
        engine.dispose()

    if not result.ok:
        print(NOT_OK_LINE.format(reason=result.reason))
        return 1
    print(OK_LINE)
    print(TICKET_LINE.format(ticket=result.ticket_id))
    return 0


def _seed_fixture(
    settings: Settings, source: str, *, check: bool, stdin: BinaryIO | None = None
) -> int:
    """FP O09: load a fixture dataset (or, with ``check``, look for it); print its line.

    Standard output carries the checksum line and nothing else, so the log
    moves to standard error first. A failure is one line there and a
    non-zero exit: 2 for a file this command cannot read or does not
    understand (the database untouched), 1 for a side it will not load or
    does not find the dataset on. A load migrates and seeds first, as
    ``seed`` always does, so it works on a database nothing has started on
    yet; ``--check`` only reads. The loader writes no file.
    """
    from pyrt.db import fixture

    _logs_to_stderr()
    try:
        raw = (stdin or sys.stdin.buffer).read() if source == "-" else Path(source).read_bytes()
    except OSError as exc:
        print(f"fixture: cannot read {source}: {exc.strerror or exc}", file=sys.stderr)
        return 2

    try:
        fixture.parse(raw)  # a file it cannot understand never reaches the database
        if not check:
            _migrate(settings)
            _seed(settings)
        engine = make_engine(settings.database_url())
        try:
            with session_scope(make_session_factory(engine)) as session:
                if check:
                    text = fixture.check(session, raw)
                else:
                    text = fixture.load(session, settings, raw)
        finally:
            engine.dispose()
    except fixture.FixtureError as exc:
        print(exc.report(), file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # the database, most likely: one line, the trace in the log
        log.error("fixture failed", exc_info=True)
        print(f"fixture: {fixture.summary(exc)}", file=sys.stderr)
        return 1
    print(text)
    return 0


def main(argv: Sequence[str] | None = None, stdin: BinaryIO | None = None) -> int:
    """Entry point; the console script exits with what this returns.

    ``stdin`` is the gateway's message, or ``seed --fixture -``'s dataset; it
    defaults to the process's own standard input and is a parameter so a
    test can hand one in.
    """
    args = _parser().parse_args(argv)
    if args.command == "seed" and args.check and args.fixture is None:
        print("pyrt seed: --check needs --fixture", file=sys.stderr)
        return 2
    pyrt_logging.configure(args.log_level)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "healthcheck":
        return _healthcheck(settings)

    if args.command == "mailgate":
        return _mailgate(settings, args.queue, args.action, stdin)

    if args.command == "serve":
        try:
            settings.require_serving()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return _serve(settings)

    if args.command == "migrate":
        _migrate(settings)
        return 0

    if args.command == "seed":
        if args.fixture is not None:
            return _seed_fixture(settings, args.fixture, check=args.check, stdin=stdin)
        _migrate(settings)
        seeded = _seed(settings)
        log.info("seed", extra={"seeded": seeded})
        return 0

    return 2  # pragma: no cover - argparse rejects an unknown command first


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
