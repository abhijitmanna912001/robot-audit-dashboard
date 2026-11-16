# Robot Audit Dashboard

Every robot action is checked twice and logged.

An auditable safety layer for AI-driven robot control. A user types a command, a planner turns it into joint actions, two independent checks must both approve, and every step is written to an append-only audit log that the dashboard shows live.

**Live demo:** https://robot-audit-dashboard.vercel.app

> **Dry run only.** Nothing is sent to a robot. The system plans, validates, reviews and records, and the execution step is a dry run.

## How it works

```
Dashboard ──> POST /api/commands ──> Supabase (commands + audit_events)
                                          │
                              Python worker (one command per run)
                                          │
        planner ─> validator (Python) ─> critic (Next.js route /api/critic)
                                          │
              both approve ─> dry run ─> every step logged ─> dashboard (Realtime)
```

1. **Plan.** A deterministic planner parses commands like `set joint _1 to 0.25 rad` into actions (joints `_1` to `_6`, unit `rad`, at most 10 actions). Anything else is recorded as "Command not understood" and nothing runs.
2. **Validator.** A Python validator checks every action against the SO-101 joint limits.
3. **Critic.** A separate service re-checks the same plan against the shared limits file (`shared/so101-limits.json`), verifies the plan hash (RFC 8785 canonical JSON, SHA-256), and can only approve or reject. Any error means nothing executes.
4. **Execution.** Only when both approve: a dry run, with `execution_started` written first.
5. **Audit log.** Every event is stored in an append-only table (a database trigger blocks updates and deletes) and shown in the dashboard timeline.

## Safety design

- **Fail closed:** a critic error, a timeout, or a malformed or mismatched response ends in `failed`, and nothing executes.
- **Two independent checks:** a rejecting verdict from either one wins, and the command ends `rejected`.
- **Immutable plan snapshot:** the approved plan is canonicalised and hashed once, and every stage works from that exact snapshot.
- **Append-only audit trail:** the log can't be edited, so what the dashboard shows is what happened.
- **Least privilege:** the browser uses the read-only anon key; writes go through the server with a service key; the critic requires a shared secret.

## Try it

Type these into the command box:

| Command | Expected result |
|---|---|
| `set joint _1 to 0.25 rad` | Both checks approve, dry run completes |
| `set joint _6 to -1.0 rad` | Rejected: below the joint 6 limit |
| `Pick up the cup and move it to the left.` | Failed: command not understood, nothing executed |

Processing needs the Python worker to run (see `worker/README.md`); without it, a new command stays "Received".

## Repository layout

- `src/app/api/commands`: command submission route
- `src/app/api/critic`: the critic
- `src/components`, `src/lib`: dashboard UI and display logic
- `worker/`: planner, validator, critic client, audit store, dry-run executor (Python)
- `db/001_init.sql`: database schema, Row Level Security, Realtime
- `shared/so101-limits.json`: joint limits used by the critic
- `docs/`: contracts (`schema-draft.md`) and robot capabilities

## Run locally

Frontend (Node 20+):

```bash
cp .env.example .env.local   # fill in your own Supabase values; never commit real keys
npm install
npm run dev
npm test
```

Worker (Python 3.10+): see `worker/README.md`.

## Operator sign-in

Only operators can submit commands: email + password sign-in (Supabase Auth, cookie session via `@supabase/ssr`), an allowlist in the server-only `OPERATORS` variable, and a rate limit. Each command records the operator's display name (`submitted_by`) and Auth user id (`submitted_by_id`). **Emails are never stored or shown**: `commands` and `audit_events` are publicly readable.

Manual setup (nothing here is automated):

1. **Supabase, Authentication, Sign In / Providers:** the **Email** provider stays enabled, with password sign-in. No emails are sent for sign-in (Supabase's built-in email service only sends 2 per hour).
2. **Keep public sign-ups off:** "Allow new users to sign up" stays disabled.
3. **Minimum password length:** in the same Auth settings, set it to **12**. This is the real enforcement; the form check is only quick feedback.
4. **Create the operators:** Authentication, Users, **Create new user**, enter the email and a temporary password, and tick **Auto Confirm User**. This is the only way accounts exist once sign-ups are off.
5. **Each operator changes the temporary password** at `/account` (header, "Account") after the first sign-in.
6. **Run the migration before deploying:** in the SQL editor run `db/004_command_submitted_by_check.sql`, then `db/004_command_submitted_by.sql`, then `db/004_command_submitted_by_test.sql` (rolls back). The route writes the new columns, so deploying first makes every submit fail with a generic 500 (nothing is saved).
7. **Vercel, Settings, Environment Variables:** add `OPERATORS` (Production, Preview and Development), then redeploy. Never prefix it with `NEXT_PUBLIC_`. Locally, add the same line to your own `.env.local`.

`OPERATORS` maps each allowed email to a display name, comma separated:

```
OPERATORS=abhijit@example.com=Abhijit,garvit@example.com=Garvit
```

Emails compare case-insensitively. A display name is 1 to 40 letters, digits, spaces, dots, hyphens or apostrophes. If any entry is malformed or an email repeats, the whole variable is rejected and submitting fails closed with a generic 500. Responses are generic: 401 not signed in, 403 not allowlisted, 429 rate limited.

## Status and roadmap

- Working end to end: command intake, planning, validation, critic review, dry run, live audit timeline.
- Next: a simulation executor (draft PR, movement gates closed). Open verification items: what a simulation stop cancels, whether joint readings are measured or echoed, and hardware isolation. See `docs/schema-draft.md`.

## Known limitations

- The command route needs a signed-in, allowlisted operator and is rate limited (5 per minute, 50 per day per operator, counted before the insert, so two simultaneous requests can exceed it by one). Reading the dashboard stays public. See "Operator sign-in".
- The audit log is readable with the public anon key, so commands must not contain secrets.
- Creating a command and its first event are two inserts, not one transaction.
- The worker handles one command per run, with no lease for stranded claims (manual recovery is documented).

## Team

Built by Abhijit Manna and Garvit Mathur.