# py-rt

A clean-room Python rewrite of [Request Tracker](https://github.com/bestpractical/rt)'s
ticket core: queues, tickets with their history, reply and comment,
simple search, custom fields, users, groups and rights, and a mail
gateway. `docs/NOTES.md` describes what is kept, what is changed on
purpose, and what is out of scope.

Written to run beside the original under the same instrumentation and
the same scripted workload, for the [oldbox](https://oldbox.io)
side-by-side comparison. Apache-2.0; see `NOTICE` for what "clean
room" means here.

## Features

- Queues, with a subject tag and correspond/comment addresses
- Tickets: create with a requestor, a title-bar display with Basics,
  People, Dates, Custom Fields and History, Reply and Comment on one
  update form, a basics edit, and a fixed status lifecycle
- Simple search: words over the subject and message bodies,
  `status:any` to include resolved and closed tickets
- Custom fields: one freeform value, applied to queues from either
  side, shown and set on the ticket
- Users and user-defined groups, with the system groups and roles
  Owner/Requestor/Cc/AdminCc
- Rights granted to a user, a group, a system group or a role, on a
  queue or globally, under RT's own right names
- A mail gateway command that files an inbound message as a new
  ticket or threads it onto one by its subject tag

## Requirements

Python 3.13, [uv](https://docs.astral.sh/uv/), MariaDB 10.11 (Docker
for local work). Nothing else: no Perl, no web server in front, no
build step for the front end.

## Install

    uv sync

## Running

    scripts/dev.sh          # MariaDB in Docker, then the app on http://localhost:8082

It brings up the `db` service from `deploy/compose.yaml`, waits for it
to report healthy, then runs `uv run pyrt serve` with `PORT=8082`,
`DATABASE_URL` pointed at that database, `BASE_URL=http://localhost:8082`,
`SITE_NAME=localhost`, `SESSION_SECRET=devsecret` and
`ROOT_PASSWORD=password` unless already set in the environment — sign
in as `root` / `password`. Any of these can be overridden by exporting
them first.

## Configuration

The whole configuration is environment variables, read once at start;
`deploy/.env.example` lists every name. `BASE_URL` and `SESSION_SECRET`
have no default — the app refuses to serve without them.

| Variable | Meaning |
|---|---|
| `PORT` | Listen port (default 8080) |
| `BASE_URL` | Absolute base for every link, redirect and mail; never derived from `Host` |
| `SITE_NAME` | The name in the subject tag `[<SITE_NAME> #<id>]` and the page title |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | The MariaDB connection |
| `DATABASE_URL` | Overrides the five `DB_*` variables with a full SQLAlchemy DSN (tests and local runs) |
| `ROOT_PASSWORD` | Seeds the `root` user on the first start, when no users exist; ignored after |
| `SESSION_SECRET` | The signing key of the session cookie; the app refuses to serve without it |
| `MAIL_MODE` | `log` (default: every message as a JSON log line), `file` (append to `MAIL_FILE`), or `smtp` (only when set explicitly) |
| `MAIL_FILE` | The file `MAIL_MODE=file` appends to, default `/var/lib/py-rt/mail.log` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | The relay for `MAIL_MODE=smtp`; ignored otherwise |
| `TZ` | Display zone, default UTC; stored times are UTC |
| `WORKERS` | uvicorn workers, default 2 |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_METRIC_EXPORT_INTERVAL` | Standard OpenTelemetry SDK variables; unset, telemetry is a no-op. The protocol variable is accepted but not read: the exporter is always OTLP over HTTP with protobuf |

## Docker

Published on every push to `main` and every `v*` tag as a two-platform
(linux/amd64, linux/arm64) OCI index at
[`ghcr.io/4scottt/py-rt`](https://github.com/4scottt/py-rt/pkgs/container/py-rt) —
`edge` for main, the version for a tag. To build it yourself, from the
repository root:

    docker build -f deploy/Dockerfile -t py-rt .

The build is two stages: `uv sync --frozen --no-dev` into a virtualenv, then
the virtualenv and the package copied into `python:3.13-slim-bookworm`. No
compiler, no uv and no package cache in the runtime image; it is around
200 MB.

Run it against a MariaDB of your own:

    docker run --rm -p 8080:8080 \
      -e BASE_URL=http://localhost:8080 -e SESSION_SECRET=change-me \
      -e DB_HOST=... -e DB_NAME=pyrt -e DB_USER=pyrt -e DB_PASSWORD=... \
      -e ROOT_PASSWORD=password ghcr.io/4scottt/py-rt:edge

or `docker compose -f deploy/compose.yaml --profile app up -d`, which brings
up MariaDB beside it and serves on <http://localhost:8082>. The container
migrates, seeds on a first start and then listens; `docker rm -f` is safe at
any point, because the only durable state is in the database.

`GET /health` answers 200 with no auth and no session (503 when the database
is unreachable), and is what the image's `HEALTHCHECK` asks, through the CLI
(`pyrt healthcheck`) rather than a curl the image does not carry. The process
runs as the non-root user `pyrt`, uid 1000; the only path it writes is
`/var/lib/py-rt`, and only when `MAIL_MODE=file`. No volume is declared.

## Mail in

One message on standard input becomes a ticket, or a reply on one:

    docker exec -i py-rt sh -c 'pyrt mailgate --queue General --action correspond' < message.eml

The command is the whole gateway: no listener, no web hook, no second
process. It reads the message, files it against the database directly and
prints two lines on success —

    ok
    Ticket: 42

— or one line on refusal, and exits 1:

    not ok: permission denied: CreateTicket

`--action` is `correspond` (the default) or `comment`; `--url` and
`--debug` are accepted and ignored, so the argv a caller already sends
keeps working. Log lines go to standard error, so standard output carries
nothing but the protocol.

**The queue is the one named on the command line**, never the `To` header.
**The subject tag decides the rest**: a subject holding `[<tag> #<id>]`
whose tag is `SITE_NAME` or the queue's own Subject Tag and whose id names
a ticket is filed onto that ticket as a correspondence (and a `new` ticket
opens, as a reply through the web opens it); anything else opens a new
ticket, with our tag taken out of the subject it is stored under. Another
tracker's tag is not ours and stays in the subject.

The sender is the `From` address's user, created unprivileged if it is
new — the address as its name, no password, so the account cannot be
signed into. It is the requestor of a ticket it opens and the author of
every transaction it writes, and its rights are what decide: `CreateTicket`
on the queue to open a ticket, `ReplyToTicket` on the ticket's queue to
answer one. A stranger writing in holds `Everyone` and `Unprivileged`, so
mail is accepted exactly where those rights are granted to `Everyone` (or
to `Requestor`, which lets a requestor answer their own ticket). Nothing
is written when the message is refused.

The body is the first `text/plain` part, the message walked depth first
with the part's charset honoured; a message that carries only HTML has its
tags stripped to text. The `Message-ID` and the top-level headers are kept
beside the body.

`pyrt mailgate` needs the database settings and `SITE_NAME` only: unlike
`pyrt serve` it does not ask for `BASE_URL` or `SESSION_SECRET`, though a
ticket link in the autoreply is empty without the first.

## Telemetry

A no-op unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set — no exporter is created, and
nothing is sent. With `OTEL_EXPORTER_OTLP_ENDPOINT` set, the app exports
over OTLP http/protobuf: an `http.server.request.duration` histogram in
seconds, carrying `http.route` (the templated path, e.g. `/ticket/{ticket_id}`,
never an id), `http.request.method` and `http.response.status_code`; and
traces, with one server span per request and one client span per database
query, so a request's time in the database reads the same way on every
side of the comparison.

## Tests

    scripts/test.sh                       # everything, against a real MariaDB
    scripts/test.sh tests/test_seed.py -k seed

It brings up the `db` service, ensures a `pyrt_test` database exists
beside the dev one, and runs `pytest` with `TEST_DSN` pointed at it (CI
uses a `mariadb:10.11` service container instead). Every function point
in `docs/function-points.md` must have a test whose name carries its id;
`tests/test_function_points.py` reads that file and fails on any id
that is missing one.

    node scripts/walk.mjs

`scripts/walk.mjs` is a headless Playwright walk of the product's six
goals in order, in the words the goal text uses, failing on any browser
console error, page error, or own-origin sub-resource answering >= 400.
Its header comment documents every variable it reads; the ones worth
knowing before a run: `WALK_URL` (default `http://localhost:8082`),
`ROOT_PASSWORD` (default `password`), `WALK_MAILGATE` (the gateway
command the mail step pipes a message into; defaults to the compose
stack's `docker compose … exec -T app pyrt mailgate`), `WALK_SITE_NAME`
(the name expected in the mail step's subject tag, default `localhost`),
`WALK_STEPS` (how many of the written steps to run), `WALK_BROWSER`
(`chrome`, the default, or `chromium` for CI), `WALK_GATE_USER` /
`WALK_GATE` (basic-auth credentials, when walking through an oldbox
gate instead of the app directly), and `PLAYWRIGHT_DIR` (where the
`playwright` package is installed; defaults to `scripts/.walk`).

## Layout

    pyrt/
      cli.py             # the `pyrt` command: migrate | seed | serve | healthcheck | mailgate
      app.py             # the ASGI app: config, routers, the shell, the errors
      config.py          # the environment, typed, read once at start
      acl.py             # the rights resolver: effective principals, has_right, require_right
      auth.py            # bcrypt and the signed session cookie
      telemetry.py       # OpenTelemetry: metrics and traces, a no-op when unset
      logging.py         # JSON lines to stdout
      db/                # the SQLAlchemy engine, models, Alembic migrations, the seed
      queues/            # /admin/queues/*
      users/             # /admin/users/*
      groups/            # /admin/groups/*
      rights/            # group rights, user rights, on a queue and globally
      customfields/      # /admin/custom-fields/*, and the values on a ticket
      tickets/           # create, display, update, basics, history, transactions
      search/            # the simple search grammar and results page
      mail/              # inbound parsing, the gateway CLI, outgoing mail and notifications
      web/               # request-scoped user/session/db, home, errors
      templates/         # Jinja2: base, home, ticket/*, admin/*, search/*, errors/*
      static/            # CSS
    tests/                # conftest, one file per area, the function-point traceability test
    docs/                 # function-points.md (the test contract), NOTES.md (this rewrite vs RT)
    deploy/               # Dockerfile, compose.yaml, .env.example
    scripts/              # dev.sh, test.sh, walk.mjs
    .github/workflows/    # test.yml (ruff, mypy, pytest, the walk), image.yml (the GHCR image, the footprint check)

## Licence and provenance

Apache-2.0 (`LICENSE`); `NOTICE` names Request Tracker as the product
this rewrite reimplements. **Clean room**: Request Tracker is
GPL-2.0, so its behaviour was read as a specification — what a screen
offers, what a right gates, what a transaction records, the mail
gateway's protocol — and reimplemented from that reading. No RT code,
templates, stylesheets, scripts, seed text or documentation prose was
copied; the names of rights, statuses, menus and fields are facts
about the product and are kept, because the same goal text is meant
to describe both sides of the comparison.
