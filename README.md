# py-rt

A Python rewrite of [Request Tracker](https://github.com/bestpractical/rt)'s
ticket core: queues, tickets with their history, reply and comment,
simple search, custom fields, users, groups and rights, and a mail
gateway. Written for the [oldbox](https://oldbox.io) side-by-side
comparison, where it runs beside RT 5 and RT 6 under the same telemetry
and the same workload. Apache-2.0; a clean-room rewrite, see `NOTICE`.

Work in progress: milestone M1. The plan and the function-point contract
are in `docs/`.

## Requirements

Python 3.13, [uv](https://docs.astral.sh/uv/), MariaDB 10.11 (Docker for
local work). Nothing else: no Perl, no web server in front, no build step
for the front end.

The whole configuration is environment variables, read once at start;
`deploy/.env.example` lists every name with a comment. The ones without a
default are `BASE_URL` (the absolute base for every link, redirect and
mail — never derived from `Host`) and `SESSION_SECRET` (the signing key of
the session cookie); the app refuses to serve without them. The database is
`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`. The rest have
image defaults: `PORT` (8080), `WORKERS` (2), `SITE_NAME`, `TZ` (UTC),
`MAIL_MODE` (`log`, so no mail leaves the container unless you set `smtp`)
and `MAIL_FILE`. `ROOT_PASSWORD` seeds the `root` user on the first start,
when no users exist, and is ignored afterwards. The standard `OTEL_*`
variables turn telemetry on; unset, it is a no-op.

## Install

    uv sync

## Running

    scripts/dev.sh          # MariaDB in Docker, then the app on http://localhost:8082

## Docker

Published on every push to `main` and every `v*` tag as a two-platform
(linux/amd64, linux/arm64) OCI index at
[`ghcr.io/4scottt/py-rt`](https://github.com/4scottt/py-rt/pkgs/container/py-rt) —
`edge` for main, the version for a tag. To build it yourself, from the
repository root:

    docker build -f deploy/Dockerfile -t py-rt .

The build is two stages: `uv sync --frozen --no-dev` into a virtualenv, then
the virtualenv and the package copied into `python:3.13-slim`. No compiler,
no uv and no package cache in the runtime image; it is around 200 MB.

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
