# Python worker

Robotics and execution side of the auditable robot agent. Simulation only.

**This is a foundation, not a working agent.** It holds tested building blocks. Nothing
connects them yet: no command is claimed, planned, sent to the critic or executed.

## Status

| Part | Status | What exists |
|---|---|---|
| Configuration | Implemented and tested | `config.py` |
| Plan hashing | Implemented and tested | `plan_hasher.py`. Matches the shared Python/TypeScript reference vector |
| Safety validator | Partially implemented | `safety_validator.py` is tested, but it is not called by anything, it checks position limits only, and its vocabulary is not agreed (see "Safety validator") |
| Critic handling | Partially implemented | `critic_gate.py` builds the request and judges a response. **The HTTP call to `/api/critic` is not implemented** |
| Execution gate | Partially implemented | `execution_gate.py` is a tested decision function. Nothing calls it and it executes nothing |
| Execution | Not implemented | No movement code exists. Execution is to stay dry-run only until both gates are integrated |
| Audit logging | Partially implemented | `supabase_client.py` can insert a command and an audit event. No status updates, no event sequence. **The write path has never been run against the database** |
| Command claiming and the worker loop | Not implemented | The claim rule is decided in docs/schema-draft.md, but there is no code |
| Planner | Not implemented, blocked | Needs the action vocabulary and a decision on the language model |
| Cyberwave connection | Implemented, read-only | `cyberwave_client.py` connects in simulation mode and reads the joint schema. It cannot move anything |

Facts that must not be lost:

- No physical robot movement has been tested, and no Cyberwave simulation has been started.
- Capabilities reported by the Cyberwave API have been read, not physically verified.
- The validator is not a complete physical safety envelope.
- The Supabase smoke test (the only script that writes) has not been executed. A read-only
  check has: both tables are reachable and have the expected columns.

## Layout

| File | Purpose |
|---|---|
| `config.py` | Loads settings from `worker/.env` or the environment. Never prints values |
| `schemas.py` | Typed shapes mirroring `db/001_init.sql` (statuses, event types, rows) |
| `cyberwave_client.py` | `SimulatedRobot`: connect in simulation mode, read joints. No movement |
| `supabase_client.py` | `AuditStore`: insert into `commands` and `audit_events` |
| `plan_hasher.py` | RFC 8785 + SHA-256 hash of a plan's actions array |
| `robot_profile.py` | SO-101 joint names and schema position limits used by the validator |
| `safety_validator.py` | Deterministic validator for a plan's actions. Pure, never raises, fails closed |
| `critic_gate.py` | Builds the critic request and judges a critic response. Pure, sends no request |
| `execution_gate.py` | Allows only if validator and critic both passed the same plan. Executes nothing |
| `supabase_read_check.py` | Manual script: read-only Supabase check, zero-row SELECTs only |
| `test_cyberwave_connection.py` | Manual script: Cyberwave connection check |
| `discover_cyberwave_assets.py` | Manual script: read-only asset lookup, creates nothing |
| `supabase_smoke_test.py` | Manual script: one-time Supabase write check. Needs `--confirm-write`, and refuses to write a second time |
| `tests/` | Unit tests. All offline: no network, no database, no robot |

The modules are plain files, not a package, so run everything **from the `worker/`
directory**.

## Setup

Python 3.12, virtual environment in `worker/.venv/`.

```
cd worker
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Create `worker/.env` (ignored by Git) with these variables. Names only, never commit
values:

| Variable | Used by | Notes |
|---|---|---|
| `CYBERWAVE_API_KEY` | Cyberwave | required |
| `CYBERWAVE_WORKSPACE_ID` | Cyberwave | optional. Only needed when the API token is scoped to more than one workspace. Without it, a multi-workspace token gets `400 workspace_required` |
| `ROBOT_ASSET_IDENTIFIER` | Cyberwave | required, no default. The asset UUID. For the SO-101 it is `89696429-d5d7-4243-85be-ac0d3c148044`. Not needed by `discover_cyberwave_assets.py` |
| `CYBERWAVE_ENVIRONMENT_ID` | Cyberwave | optional. The UUID of an existing environment. Unset: the SDK reuses or creates a "Quickstart Environment" |
| `SUPABASE_URL` | Supabase | required |
| `SUPABASE_SECRET_KEY` | Supabase | required. Backend key (`sb_secret_...`). Server-side only |
| `CRITIC_URL` | critic call | required once the pipeline exists |
| `CRITIC_SHARED_SECRET` | critic call | required once the pipeline exists |

In the worker, `SUPABASE_SECRET_KEY` replaces the older name `SUPABASE_SERVICE_ROLE_KEY`.
The Next.js app has its own environment file and still uses its own variable names.

`ROBOT_ASSET_IDENTIFIER` replaces `ROBOT_ASSET_SLUG`. The old name is not read as a
fallback: if it is set and the new one is not, the worker stops with an error asking for
the rename.

**Use the asset UUID, not the registry id.** SDK 0.7.5 resolves a UUID with a direct
fetch. It resolves a registry id such as `the-robot-studio/so101` through a list call that
returns nothing once the client carries a workspace filter, and `cw.twin()` adds that
filter itself, so the registry id fails with "Asset not found". Details are in
`docs/robot-capabilities.md`, section 8.

## Commands

All from `worker/`.

Unit tests (offline, safe to run any time):

```
.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

Cyberwave connection test (network calls, read-only, no simulation started):

```
.venv\Scripts\python.exe test_cyberwave_connection.py
```

Cyberwave asset discovery (network calls, GET only, creates no twin or environment). Use
it to find an asset's UUID, or when `twin()` reports "Asset not found", because the SDK
hides the real API error. It looks the SO-101 up by its registry id, which is kept in the
script, and reports whether `ROBOT_ASSET_IDENTIFIER` matches what it found:

```
.venv\Scripts\python.exe discover_cyberwave_assets.py
```

Supabase read-only check (network calls, four zero-row SELECTs, writes nothing). Prints
only True/False results and sanitised errors:

```
.venv\Scripts\python.exe supabase_read_check.py
```

Supabase smoke test. A one-time test: it checks for an earlier smoke-test command, then
writes one command and one audit event. A second run is refused:

```
.venv\Scripts\python.exe supabase_smoke_test.py --confirm-write
```

The behaviour is explained under "Supabase smoke test" below.

## Things to know before running the manual scripts

**Cyberwave connection test**

- It never calls `affect()`, so it does not start a billable simulation, and it sends no
  joint, gripper or motion command.
- `Cyberwave.twin()` is get-or-create. On first run it may create an SO-101 twin, and a
  "Quickstart Environment" if `CYBERWAVE_ENVIRONMENT_ID` is not set.
- With `CYBERWAVE_WORKSPACE_ID` unset, the SDK picks the workspace itself when it needs
  one for the Quickstart Environment: a workspace named "Quickstart Workspace" if the token
  sees one, otherwise the first workspace the token sees. If the token sees none, it
  creates "Quickstart Workspace".
- With no simulation running, no joint telemetry arrives and the SDK returns 0.0 for every
  joint. The script says so instead of presenting zeros as measurements.

**Supabase smoke test**

This is a one-time test. Its behaviour was agreed with Abhijit.

| Flags given | What the script does |
|---|---|
| none | Checks that the configuration is present and exits. It sends no request to Supabase |
| `--confirm-write`, first time | Looks for an existing smoke-test command, finds none, and inserts one command and one audit event |
| `--confirm-write`, any later time | Finds the existing command, prints its id, writes nothing, and exits with code 3 |

There is no flag that overrides the refusal.

What a successful run writes, and nothing else:

| Table | Values |
|---|---|
| `commands` | `text = "[smoke test] do not process"`, `status = "received"` (the database default), `claimed_by = "supabase_smoke_test"`, `claimed_at` set to the time of the run |
| `audit_events` | one row: `event_type = "command_received"`, `message = "Smoke test: synthetic command received"`, `payload = {"source": "smoke_test"}` |

No plan, validator, critic or execution event is written.

- **`--confirm-write` is mandatory** before the script sends any request or writes
  anything. Without it, the script exits without contacting Supabase.
- **Duplicate check.** Before writing, the script looks for an existing command whose
  text is exactly `[smoke test] do not process`. This is a read-only query.
- **An existing record means a refusal.** The script prints the existing id and creation
  time, writes nothing, and leaves the existing rows untouched.
- **Lookup failures always stop the script.** If the lookup cannot be made, errors, or
  returns anything unexpected, nothing is written (fail closed, exit code 1).
- **The records are permanent.** `audit_events` is append-only, a command with events
  cannot be deleted, and the script never updates or deletes a row.
- **The duplicate check is not atomic.** It is a SELECT followed by an INSERT, in separate
  requests. Two runs started at the same moment can both find nothing and both insert.
  Run it from one terminal, once.
- **A half-finished run cannot be retried with this script.** If the command is inserted
  but the audit event fails, the script prints `PARTIAL WRITE` with the command id. Later
  runs are refused because the command exists, so the command stays without its audit
  event until someone adds it by other means.
- The command is inserted already claimed. Under the draft claim rule in
  docs/schema-draft.md section 3, which requires `claimed_at is null`, a worker skips it.
  It keeps the status `received` and shows in the dashboard as a `received` command.
- In the real flow the dashboard route handler owns the `commands` insert and the
  `command_received` event (single-writer rule, docs/schema-draft.md section 1). The smoke
  test writes them only to prove connectivity.

## Plan hashing contract

`plan_hasher.hash_actions(actions)` returns `sha256:<64 lowercase hex chars>`.

- Input is the `actions` array only.
- Canonicalization is RFC 8785 (JCS) through the `rfc8785` library. No custom JSON code.
- UTF-8 bytes, SHA-256.
- Array order matters. Object key order does not.
- NaN and Infinity are rejected, as are integers beyond 2^53 and non-JSON types. All raise
  `PlanHashError`.

Numbers follow ECMAScript rules, so `50` and `50.0` hash identically. That is what keeps
the Python and TypeScript sides in agreement.

The shared Python/TypeScript reference vector from docs/schema-draft.md ("Hash reference
test") is an automated test in `tests/test_plan_hasher.py`. Its expected canonical form and
hash are a contract with the TypeScript side: do not edit them to make a test pass.

## Safety validator

`safety_validator.validate_plan(plan_id, actions)` returns a result whose `to_payload()` is
the `validator_result` event payload from docs/schema-draft.md.

What it checks, for the SO-101 profile in `robot_profile.py`:

- `actions` is a non-empty array of objects, with unique `actionId` values
- every action has `actionId`, `type` and `reason` as non-empty strings, and no `null`
- only `set_joint` is accepted as an action type
- `target` is one of the schema joint names `_1` to `_6`. No alias exists for other names
- `value` is a finite number (not a string, not a boolean, not NaN or Infinity)
- `unit` is exactly `rad`. No conversion is done
- `value` is inside the schema position limits for that joint, limits included
- no field outside the agreed action shape is present

What it does not check: velocity, acceleration, effort, collisions, how far a move is from
the current position, or the order of actions. The limits are schema values that have not
been confirmed by moving the arm. **Passing the validator does not make a plan safe to
execute.**

Both limits are inclusive: a value exactly at `lower` or `upper` passes. That follows the
usual meaning of joint limits and matches the critic mock, which rejects only values
beyond its bound. It is the worker's reading, not a written agreement.

Provisional behaviour, not yet agreed with the dashboard side. Each item keeps the safe
choice until it is confirmed, and none of it should be treated as the external contract:

| Point | What the validator does now | What the contract says |
|---|---|---|
| Action vocabulary | accepts `set_joint` only | OPEN. `set_joint` and `stop` are placeholders |
| Target names | accepts `_1` to `_6` only | OPEN. To come from `docs/robot-capabilities.md` |
| `unit` | required, must be exactly `rad` | listed as optional, units OPEN |
| Maximum actions | not enforced | `MAX_ACTIONS_EXCEEDED` is agreed, but no number |
| Rule ids | adds `INVALID_PLAN`, `INVALID_FIELD_TYPE`, `DUPLICATE_ACTION_ID`, `UNEXPECTED_FIELD` | seven "initial" ids, which the validator also uses |
| `validatorVersion` | `0.1.0` | a string, format OPEN |

## Critic and execution gates

`critic_gate.py` and `execution_gate.py` implement the worker's side of the fail-closed
rule (docs/schema-draft.md section 6) as pure functions. They send no request and execute
nothing.

A critic response counts as an approval only if the status is 200, the body is a valid
verdict, `commandId`, `planId` and `planHash` all equal the request, and `verdict` is
exactly `approve`. Everything else is one of two things:

- **reject**: the critic reviewed this plan and said no.
- **error**: anything else. Each case has its own internal category so they are not
  confused: wrong HTTP status, unauthorized (401), timeout, network error, malformed body,
  `commandId` mismatch, `planId` mismatch, and `planHash` mismatch.

**A `planHash` mismatch is never an approval and never an ordinary reject.** It is
recorded as an error with both hashes: the one the worker sent, and the critic's response
carrying the one the critic computed. This holds whatever the status and verdict say.

The execution gate re-runs the validator and recomputes the hash itself, so a plan changed
after approval is refused. It also refuses an approval given for a different command or
plan, even when the actions and hash are identical, and it refuses rather than raises
when a result is missing or malformed. "Allowed" means both checks passed. It is not
permission to move a robot.

Open points in the critic contract:

- No error kind is defined for a `commandId`, `planId` or `planHash` mismatch. The worker
  writes `invalid_body` with a detail saying which one.
- No error kind is defined for a network error that is not a timeout. The worker writes
  `timeout` with a detail saying what happened. This is provisional.

Not built yet: the planner, the HTTP call to `/api/critic`, command claiming, status
updates, the audit events after `command_received`, and any execution.

## Safety rules for this code

- Simulation only. `SimulatedRobot` refuses to continue if the SDK client is not in
  simulation mode.
- No movement code exists yet. It is to be added only behind the validator and critic
  gates (fail closed).
- Secrets come from the environment only. `config.redact()` scrubs them from third-party
  error text before it is printed.

See `docs/robot-capabilities.md` for what is known about the SO-101 twin.
