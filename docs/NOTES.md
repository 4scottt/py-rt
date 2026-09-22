# NOTES

What Request Tracker does and where py-rt differs, for a reader who
already knows RT. py-rt is a clean-room rewrite of RT's ticket core
(see `NOTICE`): the descriptions below are of *behaviour*, stated in
this rewrite's own words, not of RT's code or documentation.

## Scope

**Sign-in and the home page.** A signed-out visitor gets a login form
(`user`, `pass`, `Login`); a wrong password re-renders it with a
message after the hash's own delay, so the timing tells nobody
whether the name exists. Signed in, `/` is "RT at a glance": the ten
highest-priority tickets the session's user owns and the ten newest
unowned tickets, each list filtered to what the user may see.

**Tickets.** A ticket is created in a queue with a status, an owner,
one requestor (an email, auto-creating an unprivileged user if it is
new), a subject and a content body; the create writes a `Create`
transaction. The ticket page shows Basics, People, Dates, Custom
Fields and History; Reply and Comment share one update form (a radio
between them, a status select, a content box); a reply opens a `new`
ticket into `open`; a basics page edits subject, queue, owner and
priority, each change its own transaction.

**Search.** One box, on every signed-in page, matches words against
the subject and every message body; `status:any` lifts the default
filter that hides resolved, rejected and deleted tickets; a lone
integer opens that ticket directly; the results table shows up to 50
rows, newest first.

**Custom fields.** One kind — a single freeform value — applies to
queues from either side (the field's own Applies-to page or a
queue's Custom Fields page); a set, changed or cleared value writes
both the row and a transaction.

**Users and groups.** Users are privileged or not, disabled rather
than deleted, with a real name and an email. User-defined groups have
members; the system groups (Everyone, Privileged, Unprivileged) and
the roles (Owner, Requestor, Cc, AdminCc) exist without membership
rows — a rule decides who is in them.

**Rights.** Every right is granted to a user, a group, a system
group or a role, on a queue or globally, under RT's own right names.
Group Rights and User Rights pages exist per queue and globally; a
refusal is a denial page, never a server error.

**Mail in.** A gateway command reads one message on standard input,
threads it onto a ticket by its subject tag or opens a new one, and
prints a two-line protocol.

What is out, and why:

- **Approvals** — no approval workflow exists; nothing here gates a
  ticket's progress on a second person's sign-off.
- **SLA** — no service-level timers or business-hours math.
- **Assets** — no separate asset objects or catalogs to track.
- **Articles** — no canned-answer library.
- **Dashboards** — no saved dashboard of several searches at once.
- **Charts** — no charting over ticket data.
- **Saved searches** — a search is typed each time; nothing is stored.
- **The query builder** — only the simple word/`status:any` grammar
  exists; there is no structured query language.
- **REST APIs** — the mail gateway is the one non-browser entry
  point; there is no HTTP API for ticket data.
- **Custom roles** — only Owner, Requestor, Cc and AdminCc exist;
  none can be added.
- **Links and merges** — a ticket cannot be linked to or merged with
  another.
- **Attachments by web** — inbound mail's text part is stored; there
  is no upload control on any form.
- **GnuPG/S-MIME** — mail is read and sent in the clear.
- **The shredder** — there is no bulk-delete/purge tool.
- **The config editor** — configuration is environment variables read
  at start, not a page.
- **Themes** — one fixed stylesheet; nothing is swappable.
- **i18n beyond English** — every string is English, unconditionally.
- **Self-service** — there is no separate low-privilege portal; a
  requestor with no other rights has no page of their own to sign
  into.
- **Mobile** — no separate mobile layout; the one layout is
  responsive at phone width but not a distinct interface.
- **Reminders** — no per-ticket reminder objects.

## Changed on purpose

- **Flat group membership, not a cached closure.** There is no group
  nesting: a `group_members` row is direct membership, and the
  effective principal set is the user, its enabled groups, Everyone,
  and Privileged or Unprivileged — one query, not a maintained
  closure table.
- **One value table for custom fields, not an EAV store.** A field's
  value lives in one row per ticket per field
  (`ticket_custom_field_values`), because tickets are the only object
  a field can apply to here.
- **Ticket-only transactions.** The transaction and message tables
  are not polymorphic; a transaction always belongs to a ticket.
- **A signed cookie, not a sessions table.** Signing in sets a signed,
  time-stamped cookie; nothing is written to the database to hold a
  session, so there is no session row to fill or expire.
- **A textarea, not a rich-text editor.** Message content is plain
  text, stored as `text/plain` and rendered with paragraphs and
  linkified URLs; there is no client-side editor dependency.
- **Three fixed notifications, not scrips.** Outgoing mail is three
  notifications wired into the code (on create, on correspond, on
  resolve, all to the requestors) rather than a configurable rules
  engine; a comment never mails the requestor.
- **One fixed lifecycle.** Statuses and their transitions
  (`new`, `open`, `stalled`, `resolved`, `rejected`, `deleted`) are
  fixed in code; there is no lifecycle editor.
- **Environment, not a config file.** Every runtime setting is an
  environment variable read once at start (see the README's
  Configuration section), not a Perl config file.
- **The gateway as a CLI on the database, not an HTTP endpoint.** The
  mail gateway runs as a command against the database directly; there
  is no unauthenticated network endpoint for mail delivery.
- **Selects, not autocompletes.** Owner and group-member fields are
  `<select>` elements; a requestor is a plain email field. No control
  depends on an autocomplete-only widget.
- **Pages, not tabs.** A modify screen's sections (Basics, Group
  Rights, User Rights, Custom Fields, Members) are separate pages
  linked from a small tab bar, not panes hidden and shown by
  JavaScript.
- **bcrypt only.** Every password is bcrypt; there is no legacy hash,
  external auth, or token scheme.

## The rights model

The effective principal set for the signed-in user is computed once
per request, in one query: the user itself, its enabled user-defined
groups, and either Privileged or Unprivileged, plus Everyone always.
When a ticket is in hand, its roles are added to the set: Owner if
the user owns it, Requestor if the user is a requestor on it (Cc and
AdminCc exist as grantable roles, but nothing in the rewrite's
journeys assigns them). A right check is then one indexed query
against that set.

**SeeQueue.** A queue only appears in the "New ticket in" menu and in
the create and basics queue selects when the user holds both
`CreateTicket` and `SeeQueue` on it — either alone is not enough to
offer the queue as a destination.

**The global rights pages need SuperUser.** A global `AdminQueue`
grant is not sufficient to reach the global Group Rights or User
Rights pages; only `SuperUser` is.

**The foot-gun.** The global User Rights page shows `root`'s own
`SuperUser` grant checked like any other box. Saving that page with
it unticked revokes `SuperUser` from `root`, with no page left that
can grant it back — recovering means re-seeding from an empty
database. This is RT's own shape, kept as it is rather than
special-cased.

## Mail

Three notifications go to a ticket's requestors: the autoreply on
create, a copy on correspond, and a note on resolve; a comment never
mails a requestor. The actor of a notify-worthy change is skipped, so
a requestor who replies to their own ticket does not mail themself.

`MAIL_MODE` selects where outgoing mail goes: `log` (the default,
one JSON line per message, nothing leaves the process), `file`
(appended to `MAIL_FILE`), or `smtp` — and only an explicit
`MAIL_MODE=smtp` sends anything over the network; setting the
`SMTP_*` variables alone does not turn it on. The `From` address is
derived, since no setting names one: `py-rt@` followed by
`BASE_URL`'s host.

Every outgoing message and every threaded reply carries the subject
tag `[<tag> #<id>]`, where the tag is the queue's own Subject Tag if
it has one, else `SITE_NAME`. A reply's subject is matched against
that pattern to decide whether it threads onto an existing ticket or
opens a new one; a tag that does not match a name this deployment
uses is left alone, and the message opens a new ticket instead of
being mistaken for a reply to a different tracker.

The gateway's own protocol, read by anything scripting it: on success,
one line `ok` followed by one line `Ticket: <id>`; on refusal, one
line `not ok: <reason>` and a nonzero exit. The `<reason>` names what
was missing in plain words, for example a right's name or that the
named queue does not exist.

## Search

Words in the query are matched (case-insensitively) against the
ticket subject and every message body; every word must match
something. `status:any` lifts the default filter that otherwise hides
resolved, rejected and deleted tickets; any other `key:value` token
is accepted and ignored rather than treated as an error. A query that
is a single integer is not treated as a search at all — it opens that
ticket directly. Results are capped at 50 rows.

## Custom fields

One kind of field exists: a single freeform value. A field is applied
to a queue from either side — the field's own Applies-to page (a
checkbox per queue) or a queue's own Custom Fields page — and both
write the same join rows. Setting, changing or clearing a value on a
ticket writes the value row and a transaction recording the old and
new value. A ticket moved to a queue that does not carry a field it
had a value for keeps that value, shown but no longer editable — a
stray value, by design, rather than a value silently dropped. There
is no UI to reorder fields, though the storage has a place for an
order. A field's type and "applies to" are shown on its form (the
words the product uses) but are not really stored settings: every
field is the one kind, applying to tickets.

## Words the pages use

Headings: `RT at a glance`, `Login`, `Ticket metadata` (the create
form's box), `Basics`, `People`, `Dates`, `Custom Fields`, `History`,
`Group Rights`, `User Rights`, `Members`, `Applies to`, `Create a
queue`, `Modify a queue` (and the matching pairs for users, groups
and custom fields).

Buttons: `Login`, `Create`, `Save Changes`, `Update Ticket`.

Actions on a ticket: `Reply`, `Comment`, `Resolve`, `Basics`,
`History`.

History sentences, rendered from the transaction record: "Ticket
created", "Correspondence added", "Comment added", "Status changed
from X to Y", "{Field} changed from X to Y", "Custom field {name}
changed from X to Y".

Denial: "You are not allowed" (the 403 page's heading).

Success messages after a write: "Queue created" (and "Queue
updated"), "User created" (and "User updated"), "Group created" (and
"Group updated"), "Member added" (and "Member removed"), "Rights
updated", "Custom field created" (and "Custom field updated"),
"Applies to updated", "Nothing to update", "Nothing changed".

## Not RT

Things a person used to RT will miss here: no `Told` field (last
contact with the customer is not tracked separately from the
timestamps that already exist); no time-worked logging on a ticket;
no attachment upload from the web (only inbound mail's text part is
kept); no per-user preferences beyond the password; one language; no
ticket links or merges; no saved searches.
