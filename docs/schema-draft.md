# Schema and contracts: dashboard / Python worker

**Status.** The tables in `db/001_init.sql` (commands, audit_events, indexes, triggers, RLS,
Realtime publication) are **applied** to the robot-audit Supabase project. That file is the
source of truth for the SQL and must stay identical to what is running. Any change goes in a new
migration file.

The JSON contracts in section 4 are **agreed in shape but still draft in detail**. They mirror
`src/types/draft.ts`, which matches the action vocabulary but not yet every payload decision
below (see "Types" in section 9). Decisions agreed between Abhijit (dashboard) and Garvit (worker) are
marked **DECIDED**.

**Still open:**

- the AI critic's remaining rule ids for a future AI critic (section 8, question 6). The
  deterministic critic's rule ids are defined, see "Critic verdict" in section 4
- the `validatorVersion` format (section 8)

**Resolved:** the action vocabulary (`set_joint` only), the SO-101 joint names and limits, and the
unit (`rad`). See "Action vocabulary" in section 4.

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

The two inserts that create a command are not atomic. See section 9, "Known limitations".

## 2. Tables

The SQL for both tables, the indexes, triggers, RLS and the Realtime publication lives in
**`db/001_init.sql`, which is applied and authoritative**. The blocks below are the original
design record. If they disagree with the file, the file wins. Differences in the applied file:

- an extra index, `commands (status, created_at)`
- `set search_path = ''` on both trigger functions
- the whole file runs in one transaction
- the RLS policies, revokes and Realtime statements are ordered as in `db/001_init.sql`

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

-- Enforce append-only in the database, even against the service-role key.
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
fully agreed yet. A `jsonb` column absorbs changes without migrations, at the cost of weaker
validation. **Recommend `jsonb`** for `payload`, with `command_id`, `event_type` and
`created_at` as real columns because the dashboard filters and sorts on them.

**Should the browser insert commands directly?** An anon INSERT policy would remove the route
handler, but it lets anyone with the anon key insert arbitrary rows, and there is nowhere to
validate or rate-limit. Going through a route handler with the service-role key is a little
more code but gives one validated write path. **Recommend the route handler**, which also
matches the rule that the browser inserts nothing. (The route itself is public, see section 9.)

## 3. Claiming commands

**DECIDED:** claiming uses the `claimed_by` and `claimed_at` columns with a conditional update.
There is no `planning` status.

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

Why not a `planning` status: after a claim the command is still `received`, so
`status = 'received'` alone cannot stop a second claim, which is why `claimed_at is null` is in
the `where` clause. The alternative was a `planning` status, which would change the agreed
status list and `CommandStatus`. The columns also record which worker took the command.

**Crash recovery and retries (DECIDED):**

- Crash recovery for a claimed-but-abandoned command (a worker dies mid-plan) is **manual** for
  the meetup. Restart by hand.
- A command is **never retried automatically once execution has started** (once
  `execution_started` is written). Re-running a robot action that may have partly happened is
  not safe to do without a person deciding.

## 4. JSON contracts

Shapes below are agreed. The action vocabulary is **decided** (see below). They mirror
`ActionType`, `CriticAction`, `CriticRequest`, `CriticResponse` in `src/types/draft.ts`.

**`plan_id` (DECIDED):** a UUID generated by the Python worker when it generates a plan. It is
stored in `commands.plan_id` and stays the same across the planner output, the critic request and
verdict, the validator result and every execution event for that plan.

### Action vocabulary

**DECIDED.** Source: `docs/robot-capabilities.md` (SO-101 in simulation).

- The only action type is **`set_joint`**. `move_forward` (the arm cannot locomote) and `stop` (no
  SDK call exists for arm twins) are **not supported** and are rejected.
- `target` must be one of the six joint names `_1` to `_6`. Their physical meaning is unknown, so
  nothing may rely on it.
- `value` is a finite number in **radians**, within the joint's inclusive `min` and `max`.
  `unit` is mandatory and must be exactly `"rad"`. No unit conversion is done.
- The limits live in one shared file, **`shared/so101-limits.json`**, which the TypeScript critic
  reads. They are copied from `docs/robot-capabilities.md` section 6 and
  `worker/robot_profile.py` holds the same numbers (checked equal). Change all three together.
- A plan has at most **10 actions** (`MAX_ACTIONS` in `src/app/api/critic/route.ts`). A joint may
  be targeted by only one action per plan, and there are six joints, so a valid plan has at most
  six actions. The cap is a guard.
- These are schema limits, not a verified safe envelope (see `docs/robot-capabilities.md`).

### Planner output (also the critic request body)

Example (the hash is illustrative):

```json
{
  "commandId": "5b0c1f7e-0000-0000-0000-000000000001",
  "planId":    "9d2e4a10-0000-0000-0000-000000000002",
  "planHash":  "sha256:<64 lowercase hex characters>",
  "actions": [
    {
      "actionId": "a1",
      "type": "set_joint",
      "target": "_1",
      "value": 0.5,
      "unit": "rad",
      "reason": "user asked to rotate the first joint"
    }
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `planId` | string (UUID) | generated by the worker, see above |
| `planHash` | string, **required** | computed by the worker over `actions`, see "Plan hash contract". A missing or non-string `planHash` is an HTTP 400. |
| `actionId` | string | unique within the plan |
| `type` | string | must be `set_joint` |
| `target` | string, **required** | one of `_1` to `_6` |
| `value` | finite number, **required** | radians, inside the joint's inclusive limits |
| `unit` | string, **required** | must be `"rad"` |
| `reason` | string | why the planner chose this action |

An action may contain **only** `actionId`, `type`, `target`, `value`, `unit` and `reason`. Strings
must not be empty or whitespace-only. `actionId` values are unique within a plan, and no two
actions in a plan may target the same joint.

**Planner rule:** optional fields that are not set must be **omitted**, never sent as `null`.
The critic rejects any `null` in an action (`null_value_not_allowed`). See "Plan hash contract"
for why.

### Critic verdict (response body of `/api/critic`)

Example (the reason text is illustrative):

```json
{
  "commandId": "5b0c1f7e-0000-0000-0000-000000000001",
  "planId":    "9d2e4a10-0000-0000-0000-000000000002",
  "planHash":  "sha256:<64 lowercase hex characters>",
  "verdict": "reject",
  "reason": "Action a1: value is outside the limits for _1 (-1.91986 to 1.91986 rad).",
  "triggeredRules": ["joint_limit_exceeded"]
}
```

`verdict` is `"approve" | "reject"`. `planHash` in the response is the hash the critic
**recomputed** from the received `actions`, never the value the caller supplied. It is omitted
only when the hash could not be computed (`plan_hash_error`).
`triggeredRules` lists every rule that fired (all violations are reported, not just the first).
The critic ids are lowercase snake_case and are **separate from the validator's rule ids**
(`UNKNOWN_JOINT` and so on). The three marked agreed are fixed; the rest are the deterministic
critic's rules.

| Rule id | Meaning |
|---|---|
| `plan_hash_mismatch` | **agreed.** The supplied `planHash` differs from the hash the critic computed. The response carries the critic's hash. |
| `plan_hash_error` | **agreed.** The critic could not compute a hash (for example a non-finite number). Fails closed. The response has no `planHash`. |
| `null_value_not_allowed` | **agreed.** An action contains a `null`. Unset optional fields must be omitted. |
| `empty_actions` | the plan has no actions |
| `max_actions_exceeded` | more than 10 actions |
| `malformed_action` | an action is not an object, a required string is missing, not a string or whitespace-only, or `value` is missing, not a number or not finite |
| `unexpected_field` | a field outside `actionId`, `type`, `target`, `value`, `unit`, `reason` |
| `unsupported_action_type` | `type` is not `set_joint` |
| `unknown_joint` | `target` is not one of `_1` to `_6` |
| `unit_mismatch` | `unit` is not `"rad"` |
| `joint_limit_exceeded` | `value` is outside the joint's inclusive limits. Only checked when the joint, value and unit are valid. |
| `duplicate_action_id` | two actions share an `actionId` |
| `duplicate_joint_target` | two actions target the same joint |

Status-code responses also carry `unauthorized` (401), `invalid_json` and `missing_fields`
(400) and `critic_error` (500). AI critic rules, if added later, are not agreed.

### Critic contract

**Request:** the planner output above. `commandId`, `planId`, `planHash` (string) and `actions`
(array) are all required, and the shared secret header must be present.

**Response:** the verdict above, with `planHash` set to the hash the critic recomputed.

**Check order.** The critic stops at the first gate that fails:

1. **Secret.** A missing or wrong shared secret is rejected before the body is read.
2. **Body validation.** Invalid JSON, or missing or wrongly typed required fields.
3. **Hash check.** Recompute the hash from `actions` and compare it with the supplied
   `planHash`. A mismatch or a hashing failure is a reject (`plan_hash_mismatch`,
   `plan_hash_error`).
4. **Null check.** Any `null` in an action is `null_value_not_allowed`.
5. **Other rules.** Per action: field set, strings, type, joint, value, unit, limits. Then per
   plan: action cap, duplicate `actionId`, duplicate joint. An empty plan (`empty_actions`) is
   rejected before the per-action checks. Every violation is collected into `triggeredRules`.
6. **Approve** only if nothing above triggered.

**HTTP statuses:**

| Status | When |
|---|---|
| 401 | unauthorized: missing or wrong secret |
| 400 | invalid JSON, or invalid or missing fields (including a missing `planHash`) |
| 200 | every verdict, `approve` and `reject` alike. A hash mismatch or hash error is a 200 `reject`. |

On a mismatch the critic returns 200 with `verdict: "reject"`,
`triggeredRules: ["plan_hash_mismatch"]` and `planHash` set to the hash it computed, so the
`critic_result` event shows both the supplied and the computed value.

`planHash` is also stored in the `critic_result` event payload (see "Event payloads").

### Plan hash contract

**DECIDED.** The hash ties the plan the worker validated to the plan the critic reviewed and to
the plan that executes.

- `plan_hash` = SHA-256 of the RFC 8785 (JSON Canonicalization Scheme) canonical form of the
  `actions` array **only**, UTF-8 encoded, output as lowercase hex with a `sha256:` prefix.
- Keys are sorted recursively. Array order is preserved.
- `commandId` and `planId` are not part of the hashed input.
- Both sides use an existing RFC 8785 library. Neither side writes its own canonicalizer.
- Optional fields that are not set are omitted, never `null`, otherwise the two sides hash
  different JSON.

Flow:

1. The worker computes `planHash` and sends it in the critic request with `commandId`, `planId`
   and `actions`.
2. `/api/critic` recomputes the hash from the received `actions`. On a mismatch it **rejects**
   with rule id `plan_hash_mismatch`. On a match it returns the verified `planHash` in the
   response.
3. **Worker contract.** Before executing, the worker must check all of: HTTP status 200,
   `verdict` is `"approve"`, and `commandId`, `planId` and `planHash` in the response match its
   request. Any other outcome is a rejection (section 6).
4. `planHash` is also included in the `critic_result` event payload.

### Hash reference test

**Verified.** The TypeScript side (`canonicalize` 5.1.0) and the Python side (an RFC 8785
library) both produce the canonical JSON and hash below. Purpose: prove the two libraries agree,
especially on number formatting. `1.0` canonicalizes to `1`, and `0.30000000000000004` is kept
unchanged.

Reference `actions` array (input as written):

```json
[{"actionId":"a1","type":"set_joint","target":"joint_a","value":1.0,"unit":"rad","reason":"x"},{"actionId":"a2","type":"set_joint","target":"joint_b","value":0.30000000000000004,"unit":"rad","reason":"y"}]
```

Expected canonical JSON (RFC 8785: keys sorted, no whitespace, `1.0` written as `1`):

```json
[{"actionId":"a1","reason":"x","target":"joint_a","type":"set_joint","unit":"rad","value":1},{"actionId":"a2","reason":"y","target":"joint_b","type":"set_joint","unit":"rad","value":0.30000000000000004}]
```

Expected `plan_hash`:

```
sha256:e97478a64c5d373d46d3abf1d1e544643578eb8107159fe5abe96a183464c684
```

| Item | Value |
|---|---|
| Confirmed by Python worker | yes |
| Confirmed by dashboard / critic | yes (TypeScript, `canonicalize` 5.1.0) |

The joint names, units and reasons are test data, not the real vocabulary. The test vector does
not need to change, as long as both sides keep producing this hash. It is **not a valid plan**:
`joint_a` and `joint_b` would be rejected as `unknown_joint`.

### Validator result

**DECIDED** payload for the `validator_result` event:

```json
{
  "outcome": "pass",
  "planId": "9d2e4a10-0000-0000-0000-000000000002",
  "validatorVersion": "<string>",
  "errors": []
}
```

On `fail`, `outcome` is `"fail"` and `errors` holds one entry per problem:

```json
{ "ruleId": "UNKNOWN_JOINT", "actionId": "a1", "field": "target", "message": "..." }
```

`errors` is always an array, and empty on `pass`. Recommended: `actionId` and `field` are
`string | null`, since a plan-level error such as `MAX_ACTIONS_EXCEEDED` has no single action
or field.

Initial validator rule ids:

`UNSUPPORTED_ACTION`, `MISSING_REQUIRED_FIELD`, `UNKNOWN_JOINT`, `JOINT_LIMIT_EXCEEDED`,
`NON_FINITE_VALUE`, `UNIT_MISMATCH`, `MAX_ACTIONS_EXCEEDED`

These are the deterministic validator's ids. The AI critic's rule ids are separate.

### Event payloads

| Event | `payload` |
|---|---|
| `command_received` | `{ "source": "dashboard" }` |
| `plan_generated` | `{ "plan": <planner output> }` |
| `validator_result` | `{ "outcome": "pass" \| "fail", "planId": string, "validatorVersion": string, "errors": [{ "ruleId", "actionId", "field", "message" }] }` |
| `critic_result` | `{ "outcome": "approve" \| "reject" \| "error", "planHash": string?, "response": <critic verdict>?, "error": { "kind": "http_status" \| "timeout" \| "invalid_body", "httpStatus": number?, "detail": string? }? }` |
| `execution_started` | `{ "dryRun": boolean }` |
| `execution_completed` | `{ "dryRun": boolean, "summary": string? }` |
| `execution_failed` | `{ "dryRun": boolean, "error": string }` |

`planHash` in `critic_result` is omitted when there is no usable hash to record (for example a
timeout).

## 5. Event naming

**DECIDED (Abhijit and Garvit):** one `critic_result` and one `validator_result` event, with
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
2. `/api/critic` returns **HTTP 200** with a body that parses as a valid critic verdict, whose
   `commandId`, `planId` and `planHash` all match the request, and whose `verdict` equals
   `"approve"`.

Anything else is a rejection, and nothing executes: validator fail, critic `reject`, HTTP 400,
HTTP 500, timeout, network error, a 200 with an invalid body, or a response whose `commandId`,
`planId` or `planHash` does not match. The worker logs each case:

- a validator fail is a `validator_result` event with `outcome: "fail"`
- a critic `reject` is a `critic_result` with `outcome: "reject"`
- every critic failure (non-200, timeout, bad body, mismatched ids or hash) is a
  `critic_result` with `outcome: "error"` and `payload.error` describing what went wrong

It then sets `commands.status = 'rejected'`. A failure **during** execution, after approval,
is `failed`, not `rejected`.

The order of checks is the worker's call, and it may stop at the first failure. Either way,
each check that ran gets its own event.

### Dry-run mode

**DECIDED:** command-driven execution stays in **dry-run mode until the validator and the critic
are both integrated**. A dry run does not drive the robot simulation. It is logged as normal
execution events (`execution_started`, then `execution_completed` or `execution_failed`) with
`dryRun: true` in the payload. Real execution writes `dryRun: false`.

## 7. Row Level Security and Realtime

The SQL for this section is in `db/001_init.sql` and is applied. The blocks below are the
design record.

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
  demo. Do not put secrets or personal data in `text` or `payload`. See section 9.
- The service-role and OpenAI keys stay server-only and never go in `NEXT_PUBLIC_*` variables.

## 8. Open questions

1. ~~**Robot and real action types.**~~ **Closed.** `set_joint` only (section 4, "Action
   vocabulary"). `move_forward` and `stop` are not supported.
2. ~~**Joint limits.**~~ **Closed.** Position limits per joint are in `shared/so101-limits.json`,
   copied from `docs/robot-capabilities.md` section 6. Velocity, effort and acceleration limits
   are not known and are not checked.
3. ~~**Units.**~~ **Closed.** Radians, the string `"rad"`, mandatory, no conversion.
4. ~~**`plan_id` ownership.**~~ **Closed.** A UUID generated by the worker, constant across all
   events for the plan (section 4).
5. ~~**Validator output shape.**~~ **Closed.** See "Validator result" in section 4.
6. **Critic rule ids.** The three hash and null ids are agreed. The deterministic critic's other
   ids are defined in section 4, "Critic verdict". Ids for a future AI critic are not agreed.
7. ~~**Hash reference value.**~~ **Closed.** Verified by both sides (section 4, "Hash reference
   test").
8. **`validatorVersion` format.** A string, but the format (semver, git sha, date) is not agreed.

## 9. Known limitations

Accepted for the simulation-only meetup demo.

- **The command submission route is public.** Anyone who can reach the route handler can submit
  a command. There is no login and no rate limiting yet.
- **Anon SELECT exposes the audit log.** Anyone with the anon key can read `commands` and
  `audit_events`, including command text and event payloads (section 7). Do not put secrets or
  personal data in them.
- **Creating a command and its `command_received` event are two inserts that are not atomic.**
  If the second fails, the command exists without its first audit event. For now the error is
  logged and accepted. A Postgres function called through `rpc` would make it atomic later.
- **Crash recovery is manual.** A claimed-but-abandoned command is restarted by hand, and a
  command is never retried automatically once execution has started (section 3).
- **Test rows in `audit_events` cannot be deleted.** The table is append-only through the
  trigger `audit_events_no_update_delete` (`before update or delete`, row-level, function
  `public.audit_events_block_mutation()`, defined in `db/001_init.sql`). Removing test rows
  means temporarily disabling that trigger, then re-enabling it. Also,
  `audit_events.command_id` is `on delete restrict`, so a command that has events cannot be
  deleted before its events are.
- **Types.** `src/types/draft.ts` mirrors the action shape above. It has not been updated for
  the new `validator_result` shape or `dryRun`.
- **The Python validator is looser than the critic.** `worker/robot_profile.py` has
  `max_actions=None` (no cap) and `worker/safety_validator.py` has no duplicate-joint rule. Both
  gates must pass, so nothing unsafe gets through, but the two should be aligned (set
  `max_actions=10`, add a duplicate-target rule).
- **Whitespace definitions differ slightly.** The validator uses Python `str.strip()`, the critic
  uses JavaScript `trim()`. They differ only for a few control and format characters.
