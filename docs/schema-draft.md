# Schema draft: dashboard / Python worker contract

**Status: DRAFT. Nothing in this file has been applied to any database.** The SQL below is
text for review. The JSON shapes mirror `src/types/draft.ts`, which is itself a draft.
Decisions that need agreement between Abhijit (dashboard) and Garvit (worker) are marked
**DECISION**.

Out of scope: robot telemetry (joint states, sensor streams). This contract covers the
command lifecycle and its audit trail only.

## 1. Lifecycle and the single-writer rule

```
received ──► approved ──► executing ──► completed
    │                          └──────► failed
    └──────► rejected
```

Statuses: `received`, `approved`, `rejected`, `executing`, `completed`, `failed`.
`rejected`, `completed` and `failed` are terminal.

**Single-writer rule.** Each lifecycle event has exactly one writer:

| Component | Writes |
|---|---|
| Dashboard route handler (service-role key, server-side) | the `commands` row (status `received`) and the `command_received` event. Nothing else. |
| Python worker (service-role key) | every later event and every later status change on `commands` |
| `/api/critic` | nothing. It receives a plan and returns a verdict. The worker records it. |
| Browser (anon key) | nothing. Read-only. |

Why: one writer per event means no two components race to write the same fact, and the audit
log has a clear author for every row.

Known gap: creating the command row and the `command_received` event are two inserts. If the
second fails, the command exists without its first audit event. Options: accept it and log the
error (simple), or wrap both in a Postgres function called through `rpc` (atomic, a bit more
SQL). Recommendation: accept it for the hackathon.

## 2. Tables (SQL text only)

Conventions: `uuid` primary keys from `gen_random_uuid()`, `timestamptz` for all times,
snake_case in Postgres. The TypeScript types use camelCase, so the dashboard data layer maps
`created_at` to `createdAt` and so on.

```sql
-- Current state of each command. One row per command, updated in place.
create table public.commands (
  id          uuid primary key default gen_random_uuid(),
  text        text not null check (length(btrim(text)) > 0),
  status      text not null default 'received'
              check (status in ('received','approved','rejected',
                                'executing','completed','failed')),
  plan_id     uuid,                       -- null until the worker generates a plan
  claimed_by  text,                       -- worker instance id, see section 3
  claimed_at  timestamptz,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create function public.set_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

create trigger commands_set_updated_at
  before update on public.commands
  for each row execute function public.set_updated_at();

-- Append-only history. Rows are inserted, never changed or removed.
create table public.audit_events (
  id          uuid primary key default gen_random_uuid(),
  command_id  uuid not null references public.commands(id) on delete restrict,
  event_type  text not null
              check (event_type in ('command_received','plan_generated',
                                    'validator_result','critic_result',
                                    'execution_started','execution_completed',
                                    'execution_failed')),
  message     text not null,
  payload     jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);

create index audit_events_command_created_idx
  on public.audit_events (command_id, created_at);

-- OPTIONAL: enforce append-only in the database, even against the service-role key.
create function public.audit_events_block_mutation() returns trigger
language plpgsql as $$
begin
  raise exception 'audit_events is append-only';
end $$;

create trigger audit_events_no_update_delete
  before update or delete on public.audit_events
  for each row execute function public.audit_events_block_mutation();
```

Event type to status, for reference:

| Event | Worker then sets `commands.status` to |
|---|---|
| `command_received` | `received` (set at insert by the route handler) |
| `plan_generated` | unchanged |
| `validator_result` / `critic_result` | unchanged until both are in. Then `approved` or `rejected`. |
| `execution_started` | `executing` |
| `execution_completed` | `completed` |
| `execution_failed` | `failed` |

### Design choices

**Status: `text` + CHECK vs Postgres enum.** An enum gives a named type, but adding or removing
a value needs `ALTER TYPE`, which is awkward inside a migration. `text` + CHECK is the same
protection and changing the list is a one-line constraint swap. **Recommend `text` + CHECK**,
since the status and event lists are still moving. The same reasoning applies to `event_type`.

**Payload: `jsonb` vs separate columns.** Separate columns give type checks and easy SQL
filtering, but every schema change is a migration, and the action and verdict schemas are not
agreed yet. A `jsonb` column absorbs changes without migrations, at the cost of weaker
validation. **Recommend `jsonb`** for `payload`, with `command_id`, `event_type` and
`created_at` as real columns because the dashboard filters and sorts on them.

**Should the browser insert commands directly?** An anon INSERT policy would remove the route
handler, but it lets anyone with the anon key insert arbitrary rows, and there is nowhere to
validate or rate-limit. Going through a route handler with the service-role key is a little
more code but gives one validated write path. **Recommend the route handler**, which also
matches the rule that the browser inserts nothing.

## 3. Claiming commands

The worker must never process the same command twice, even with several workers or a restart.
It claims a `received` command with a single conditional update that returns the row:

```sql
update public.commands
   set claimed_by = $1,          -- worker instance id
       claimed_at = now()
 where id = $2
   and status = 'received'
   and claimed_at is null
returning *;
```

- One row returned: this worker won the claim and goes on to plan the command.
- Zero rows returned: another worker has it, or it is no longer `received`. Skip it.

Postgres re-checks the `where` clause after waiting for a row lock, so two simultaneous
claims cannot both succeed. The worker finds candidates with a normal `select` (oldest
unclaimed `received` first, or from a Realtime event) and then claims by `id`.

**DECISION: the status list has no "planning" state**, so after a claim the command is still
`received`. `status = 'received'` alone cannot stop a second claim. Options:

1. **Add `claimed_by` / `claimed_at` columns** (as in the SQL above). Status list unchanged,
   and the claim doubles as a record of which worker took it.
2. **Add a `planning` status.** The claim becomes `set status = 'planning' where status =
   'received'`. It is simpler SQL but changes the agreed status list and `CommandStatus`.

**Recommend option 1.** Crash recovery for a claimed-but-abandoned command (a worker dies
mid-plan) is not handled here. For the demo, restart by hand.

## 4. JSON contracts

All DRAFT. They mirror `ActionType`, `CriticAction`, `CriticRequest`, `CriticResponse` in
`src/types/draft.ts`.

### Planner output (also the critic request body)

```json
{
  "commandId": "5b0c1f7e-0000-0000-0000-000000000001",
  "planId":    "9d2e4a10-0000-0000-0000-000000000002",
  "actions": [
    {
      "actionId": "a1",
      "type": "move_forward",
      "value": 50,
      "unit": "m",
      "reason": "User asked to drive forward 50 metres"
    }
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `actionId` | string | unique within the plan |
| `type` | `"set_joint" \| "move_forward" \| "stop"` | **unconfirmed**, see Open questions |
| `target` | string, optional | e.g. a joint name for `set_joint` |
| `value` | number, optional | |
| `unit` | string, optional | units not agreed, see Open questions |
| `reason` | string | why the planner chose this action |

### Critic verdict (response body of `/api/critic`)

```json
{
  "commandId": "5b0c1f7e-0000-0000-0000-000000000001",
  "planId":    "9d2e4a10-0000-0000-0000-000000000002",
  "verdict": "reject",
  "reason": "Requested distance and speed exceed the safe envelope",
  "triggeredRules": ["max_speed"]
}
```

`verdict` is `"approve" | "reject"`. `triggeredRules` is a list of rule ids. The rule names
are illustrative, and the real list is not agreed.

### Event payloads

| Event | `payload` |
|---|---|
| `command_received` | `{}` |
| `plan_generated` | `{ "plan": <planner output> }` |
| `validator_result` | `{ "outcome": "pass" \| "fail", "errors": string[] }` (validator output shape not agreed) |
| `critic_result` | `{ "outcome": "approve" \| "reject" \| "error", "response": <critic verdict>?, "error": { "kind": "http_status" \| "timeout" \| "invalid_body", "httpStatus": number?, "detail": string? }? }` |
| `execution_started` | `{}` |
| `execution_completed` | `{ "summary": string? }` |
| `execution_failed` | `{ "error": string }` |

## 5. Event naming

**DECISION (Abhijit and Garvit):** one `critic_result` and one `validator_result` event, with
the outcome in `payload`, instead of separate pass and fail event types. This keeps the event
type list short and means each check has one event type to query. The cost is that a UI badge
has to read `payload.outcome` rather than only `event_type`.

Mapping from the mock event names in `src/mocks/mock-data.ts`:

| Mock `eventType` | Proposed `event_type` | Outcome in payload |
|---|---|---|
| `received` | `command_received` | |
| `critic_pass` | `critic_result` | `outcome: "approve"` |
| `critic_fail` | `critic_result` | `outcome: "reject"` (or `"error"`, see section 6) |
| `validator_pass` | `validator_result` | `outcome: "pass"` |
| (none in mocks) | `validator_result` | `outcome: "fail"` |
| (none in mocks) | `plan_generated` | |
| `executing` | `execution_started` | |
| `completed` | `execution_completed` | |
| `failed` | `execution_failed` | |
| `rejected` | **no event of its own** | the status change to `rejected` follows the failing `validator_result` or `critic_result`, which carries the reason |

If you want an explicit "command rejected" row in the timeline, the alternative is an eighth
event type. Recommendation: do not add one, because the rejecting event already holds the
reason.

## 6. Fail-closed rule

The worker executes a command **only if both** hold:

1. the deterministic validator passes, and
2. `/api/critic` returns **HTTP 200** with a body that parses as a valid critic verdict
   (matching `commandId` and `planId`) and `verdict` equal to `"approve"`.

Anything else is a rejection, and nothing executes: validator fail, critic `reject`, HTTP 400,
HTTP 500, timeout, network error, or a 200 with an invalid body. The worker logs each case:

- a validator fail is a `validator_result` event with `outcome: "fail"`
- a critic `reject` is a `critic_result` with `outcome: "reject"`
- every critic failure (non-200, timeout, bad body) is a `critic_result` with
  `outcome: "error"` and `payload.error` describing what went wrong

It then sets `commands.status = 'rejected'`. A failure **during** execution, after approval,
is `failed`, not `rejected`.

The order of checks is the worker's call, and it may stop at the first failure. Either way,
each check that ran gets its own event.

## 7. Row Level Security and Realtime

The browser (anon key) can SELECT from both tables and INSERT nothing. All writes use the
service-role key, which bypasses RLS, so it needs no policies.

```sql
alter table public.commands     enable row level security;
alter table public.audit_events enable row level security;

-- Read-only for the browser. No insert, update or delete policies exist,
-- so those operations are denied for anon and authenticated.
create policy "anon can read commands"
  on public.commands for select to anon, authenticated using (true);

create policy "anon can read audit events"
  on public.audit_events for select to anon, authenticated using (true);

-- Defence in depth: remove table privileges too, in case a policy is added by mistake.
revoke insert, update, delete, truncate on public.commands     from anon, authenticated;
revoke insert, update, delete, truncate on public.audit_events from anon, authenticated;

-- Realtime for the dashboard.
alter publication supabase_realtime add table public.commands;
alter publication supabase_realtime add table public.audit_events;
```

Notes:

- Realtime `postgres_changes` evaluates RLS for the subscribing role. Without the SELECT
  policies above, the dashboard would receive no events.
- **Security note: because anon can SELECT, the audit log, including command text and event
  payloads, is readable by anyone who has the anon key.** The anon key ships in the browser
  bundle, so treat both tables as public. That is acceptable for a simulation-only hackathon
  demo. Do not put secrets or personal data in `text` or `payload`.
- The service-role and OpenAI keys stay server-only and never go in `NEXT_PUBLIC_*` variables.

## 8. Open questions

1. **Robot twin and real action types.** Which Cyberwave twin is used, and what actions does it
   actually support? `set_joint | move_forward | stop` is a guess. If the twin supports other
   actions, `ActionType`, the planner prompt, the validator and the critic rules all change.
2. **Joint limits.** What are the min and max per joint (and any velocity or torque limits)?
   The validator needs hard numbers, and the critic needs them to explain a rejection.
3. **Units.** Degrees or radians for joints? Metres and metres per second for movement? Is
   `unit` a free string or a fixed set, and does the validator convert or reject mismatches?
4. **`plan_id` ownership.** This draft makes the worker generate a uuid when it produces a
   plan. Confirm, or say whether it should be a string.
5. **Validator output shape.** What does the validator return on failure (error list, rule
   ids)? It determines the `validator_result` payload.
6. **Critic rule ids.** The list of `triggeredRules` values is not defined yet.
