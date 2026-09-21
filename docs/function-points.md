# Function points

The test contract of py-rt: every id below must have a test whose name
contains it (`tests/test_function_points.py` checks). The tables are the
plan's §9 (`docs/PLAN.md`), copied here so the repository is self-contained;
change the plan first, then this file.

This is the test contract. `docs/function-points.md` in the repo holds
this table; `tests/test_function_points.py` reads the ids from it and
fails if any id has no test whose name contains it (lower-cased:
`test_fp_t03_…`). Legend for the *test* column: **u** unit (pure
Python), **h** handler test with `httpx` against a real MariaDB
(`TEST_DSN`), **w** the Playwright walk in `scripts/walk.mjs`, **g**
golden HTML.

### L. Sign-in and session

| Id | Function point | RT source | Test |
|---|---|---|---|
| L01 | Signed-out `/` shows the login form (`user`, `pass`, `Login`); a wrong password re-renders with a message after the hash's delay; a disabled user cannot sign in | Login.html, RT::User | h |
| L02 | Signed in, `/` is "RT at a glance" with the two lists (10 highest priority I own; 10 newest unowned), resolved tickets excluded | index.html, MyTickets | h,g |
| L03 | The session cookie is signed, HttpOnly, SameSite=Lax, Secure when `BASE_URL` is https; tampering signs out; `/logout` clears it | Interface::Web::Session | u,h |
| L04 | Every route but `/`, `/login`, `/health` and `/static` requires a session; the redirect returns to the page after sign-in | autohandler | h |
| L05 | The quick search box (`q`, `Search`) is on every signed-in page's header | Elements/Header | g |

### Q. Queues

| Id | Function point | RT source | Test |
|---|---|---|---|
| Q01 | Queues list: name, description, enabled; disabled hidden unless asked; needs `AdminQueue` or `SeeQueue` | Admin/Queues/index.html | h |
| Q02 | Create a queue: unique name (case-insensitive), description, subject tag, reply and comment addresses; the creator sees it in Select; needs `AdminQueue` | Admin/Queues/Modify.html, RT::Queue | h,w |
| Q03 | Modify a queue's basics; disabling hides it from the create form's queue select | Modify.html | h |
| Q04 | The queue page links Group Rights, User Rights and Custom Fields for that queue | Admin/Queues/Elements/Tabs | g |
| Q05 | The General queue exists after the seed; the seed grants nothing to Everyone on it (as `initialdata`) | initialdata | h |
| Q06 | A queue's subject tag, when set, replaces `SITE_NAME` in the mail subject tag | RT::Queue::SubjectTag | u |

### T. Tickets

| Id | Function point | RT source | Test |
|---|---|---|---|
| T01 | Create: queue, status (default new), owner (default Nobody), one requestor email (auto-created as an unprivileged user if unknown), subject (default `[no subject]`), content; needs `CreateTicket` on the queue | Ticket/Create.html, RT::Ticket::Create | h,w |
| T02 | Create records a `Create` transaction with the content as its message and sets Created, Starts and LastUpdated | RT::Ticket::Create | h |
| T03 | Display: title bar `#{id}: {subject}`, Basics (id, status, queue, owner, priority), People (owner, requestors), Dates (created, last updated, resolved), Custom Fields, History; needs `ShowTicket` | Ticket/Display.html | h,g |
| T04 | History renders the transactions oldest first: type, creator, time, the message body with paragraphs and linkified URLs, escaped | Ticket/Elements/ShowHistory | h,g |
| T05 | Reply (`respond`): a `Correspond` transaction with the message; needs `ReplyToTicket`; a new ticket becomes open | Ticket/Update.html, RT::Ticket::Correspond | h,w |
| T06 | Comment: a `Comment` transaction; needs `CommentOnTicket`; comments are not mailed to the requestor | RT::Ticket::Comment | h,w |
| T07 | Status change on the update page and on basics: a `Status` transaction with old and new; only the lifecycle's transitions are offered | RT::Ticket::SetStatus | h |
| T08 | Resolve sets `resolved` and the Resolved timestamp; reopening clears it; the goal's check (a `Status` transaction) holds | RT::Ticket::SetStatus | h,w |
| T09 | Basics edit: subject, queue, owner, priority, each change its own `Set` transaction with old and new; needs `ModifyTicket` | Ticket/Modify.html | h |
| T10 | Owner: the select lists privileged users with `OwnTicket` on the queue plus Nobody; changing it records `Set Owner`; `TakeTicket`/`StealTicket` semantics reduced to the select | RT::Ticket::SetOwner | h |
| T11 | Ticket ids are global integers; `/ticket/{unknown}` is 404; a deleted-status ticket is hidden from lists but its page stays | RT::Ticket::Load | h |
| T12 | Priority: an integer 0–99 shown in Basics | RT::Ticket | h |
| T13 | The update page preselects Reply or Comment from the link and offers the status select | Ticket/Update.html | g |
| T14 | LastUpdated and LastUpdatedBy move on every transaction | RT::Record | h |

### S. Search

| Id | Function point | RT source | Test |
|---|---|---|---|
| S01 | Words match the subject and every message body (case-insensitive `LIKE`, every word must match); an integer alone opens that ticket | Search/Simple.html, RT::Search::Simple | h |
| S02 | Without `status:any` the results hide resolved, rejected and deleted tickets; with it they show | RT::Search::Simple | h,w |
| S03 | Other `key:value` tokens are ignored (not an error); an empty query lists nothing | RT::Search::Simple | u |
| S04 | The results table: Id, Subject, Status, Queue, Owner, Created; newest first; only tickets the user may `ShowTicket` | Search/Results.html | h,g |
| S05 | "No tickets found" empty state | Search/Results.html | h |

### F. Custom fields

| Id | Function point | RT source | Test |
|---|---|---|---|
| F01 | Create a custom field: unique name, description, type `Enter one value`, applies to `Tickets`; needs `AdminCustomField` | Admin/CustomFields/Modify.html | h,w |
| F02 | Applies to: a checkbox per queue; saving writes and removes `queue_custom_fields` rows; the goal's check holds | Admin/CustomFields/Objects.html | h,w |
| F03 | A queue's custom fields appear on the create form, on basics and on the ticket page, in sort order | Elements/EditCustomFields | h,g |
| F04 | Setting a value on basics writes `ticket_custom_field_values` and a `CustomField` transaction with old and new; clearing deletes the row | RT::Ticket::AddCustomFieldValue | h,w |
| F05 | The value shows on the ticket page under Custom Fields; needs `SeeCustomField`; setting needs `ModifyCustomField` | Elements/ShowCustomFields | h |
| F06 | Disabling a field hides it from every form; its values stay | RT::CustomField | h |
| F07 | A ticket moved to a queue without the field keeps its value (shown, not editable) | RT::Ticket::SetQueue | h |

### U. Users and groups

| Id | Function point | RT source | Test |
|---|---|---|---|
| U01 | Users list: name, real name, email, privileged, enabled; disabled hidden unless asked; needs `AdminUsers` | Admin/Users/index.html | h |
| U02 | Create a user: unique name, email, real name, an optional password, the privileged checkbox; needs `AdminUsers` | Admin/Users/Modify.html | h,w |
| U03 | Modify a user; the password changes only when the field is filled; disabling (not deleting) removes them from sign-in and the owner select | Admin/Users/Modify.html | h |
| U04 | `root` is seeded with `ROOT_PASSWORD` once, privileged, with `SuperUser` globally; the seed is idempotent | initialdata | h |
| U05 | Groups list (user-defined only); create with a unique name and description; needs `AdminGroup` | Admin/Groups/index.html, Modify.html | h,w |
| U06 | Members: add a user from a select, remove one; a user is in a group at most once; needs `AdminGroupMembership` | Admin/Groups/Members.html | h,w |
| U07 | The system groups Everyone, Privileged and Unprivileged exist from the seed and are membership by rule (everyone; the privileged flag) never by row | initialdata, RT::Group | u,h |
| U08 | Disabling a group removes its rights from the effective set without deleting the grants | RT::Group | h |

### R. Rights and the ACL

| Id | Function point | RT source | Test |
|---|---|---|---|
| R01 | The resolver: a user's effective principals are the user, its enabled groups, Everyone, Privileged or Unprivileged, and per ticket the roles it holds (Owner, Requestor); one query for the set, cached on the request | RT::Principal::HasRight, ACL.pm | u,h |
| R02 | A right is granted to a principal on a queue or globally; a global grant covers every queue; `SuperUser` covers every right | ACL.pm | u,h |
| R03 | Group Rights on a queue: a checkbox per right for each system group, role and user-defined group; Save writes the differences; the goal's check (`rights`) holds | Admin/Queues/GroupRights.html | h,w |
| R04 | User Rights on a queue: the same for privileged users | Admin/Queues/UserRights.html | h |
| R05 | Global group and user rights pages | Admin/Global/GroupRights.html | h |
| R06 | `ShowTicket` gates display, history and search results; `SeeQueue` gates the queue in selects; `CreateTicket` gates create by web and by mail; `ReplyToTicket`/`CommentOnTicket` gate the update page's radio; `ModifyTicket` gates basics; `OwnTicket` gates the owner select | ACL checks across share/html | h |
| R07 | `AdminQueue`, `AdminUsers`, `AdminGroup`, `AdminGroupMembership`, `AdminCustomField` gate their admin pages; `ShowConfigTab` gates the Admin menu | Admin autohandler | h |
| R08 | A refused action renders the denial page ("You are not allowed") with 403, never a 500 | Elements/Error | h,g |
| R09 | Everyone with `CreateTicket` and `ReplyToTicket` on General lets the gateway open and answer tickets for an unknown sender (the mail goal's first subtask) | RT::Interface::Email | h |

### M. Mail

| Id | Function point | RT source | Test |
|---|---|---|---|
| M01 | `pyrt mailgate --queue <name> --action correspond [--url ignored] [--debug]` reads one RFC 5322 message on stdin, prints `ok` and `Ticket: <id>` on success, `not ok: <reason>` and exit 1 otherwise; `--url` is accepted and ignored so the exercise body's argv shape holds | bin/rt-mailgate.in, REST mail-gateway | u,h |
| M02 | A message whose subject has no tag creates a ticket in the named queue with the sender as requestor (auto-created, unprivileged) and the text part as the `Create` message; the subject is the mail's | RT::Interface::Email::Gateway | h |
| M03 | A subject tag `[<SITE_NAME> #<id>]` (or the queue's own tag) threads the message onto that ticket as a `Correspond`; an unknown id creates a new ticket | ParseTicketId | u,h |
| M04 | The sender needs `CreateTicket` (create) or `ReplyToTicket` (reply) on the queue, resolved through Everyone for an unknown sender; refused otherwise with the reason in `not ok` | Gateway ACL | h |
| M05 | Parsing: the first `text/plain` part (multipart walked), charset honoured, HTML-only messages converted to text by stripping tags; `Message-ID` stored | RT::EmailParser | u |
| M06 | Outgoing mail: `Sender` with `log`, `file` and `smtp` implementations; `MAIL_MODE` unset means `log`; SMTP only with `MAIL_MODE=smtp` (the user's rule); every message carries the subject tag and `BASE_URL`'s ticket link | RT::Action::SendEmail | u,h |
| M07 | Three notifications: on create to the requestor (the autoreply), on correspond to the requestor (unless the actor is the requestor), on resolve to the requestor; comments never mail the requestor | initialdata's scrips | h |
| M08 | Mail to the requestor of a mail-created ticket carries the tag, so the exercise body's reply threads | SubjectTag | h |

### O. Operations and platform

| Id | Function point | Test |
|---|---|---|
| O01 | Migrations idempotent from empty and from the previous version; `pyrt migrate` alone works; the seed runs only when `users` is empty | h |
| O02 | `/health` 200 without auth or session; 503 when the DB is unreachable; `pyrt healthcheck` exit codes; no row written by a probe | h |
| O03 | Starts and serves with no `OTEL_*` env; with env set and no collector, no error above debug within the first 60 s | h |
| O04 | HTTP metrics: a request produces an `http.server.request.duration` histogram point in seconds with `http.route` templated and `http.response.status_code` (in-memory reader); a query produces a client span | u |
| O05 | Every link, redirect and mail URL is built from `BASE_URL`, not Host | h |
| O06 | Refuses to serve without `SESSION_SECRET` or `BASE_URL`, with a one-line reason (the card's signatures) | u |
| O07 | Image builds for linux/amd64 and linux/arm64, runs as non-root, `/var/lib/py-rt` writable, under 250 MB; the footprint job records the size and the RSS after the walk | CI |
| O08 | The acceptance walk (§13) passes with zero console errors, zero page errors, zero ≥400 own-origin sub-resources | w |

Seventy function points: L5 Q6 T14 S5 F7 U8 R9 M8 O8.
