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
local work).

## Install

    uv sync

## Running

    scripts/dev.sh          # MariaDB in Docker, then the app on http://localhost:8082

## Docker

    docker build -f deploy/Dockerfile -t py-rt .

The image reads its configuration from the environment; `deploy/.env.example`
lists the names.
