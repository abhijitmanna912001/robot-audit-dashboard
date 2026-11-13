# Python worker

Robotics and execution side of the auditable robot agent. Simulation only.

**This is a foundation, not a working agent.** It holds tested building blocks. The only
connected path is a planner failure: `command_processor.py` claims one command, and if the
planner cannot parse it, records `plan_generated` (error) and sets `received -> failed`.
No plan is validated, sent to the critic or executed.

## Status

| Part | Status | What exists |
|---|---|---|
| Configuration | Implemented and tested | `config.py` |
| Plan hashing | Implemented and tested | `plan_hasher.py`. Matches the shared Python/TypeScript reference vector |
| Safety validator | Implemented and tested, not yet called | `safety_validator.py` checks the agreed action vocabulary, schema position limits, one action per joint and the 10-action cap (`validatorVersion` `"1.0"`). Nothing in the pipeline calls it yet. Position limits only: no velocity, effort or collision checks (see "Safety validator") |
| Critic handling | Partially implemented | `critic_gate.py` builds the request and judges a response. **The HTTP call to `/api/critic` is not implemented** |
| Execution gate | Partially implemented | `execution_gate.py` is a tested decision function. Nothing calls it and it executes nothing |
| Execution | Not implemented | No movement code exists. Execution is to stay dry-run only until both gates are integrated |
| Audit logging | Implemented, offline tests only | `audit_store.py` is the canonical writer: claim a command, append one audit event (no retry), and conditional status updates checked against the agreed lifecycle. `supabase_client.py` is the older insert-only helper used by the smoke test. **Neither write path has been run against the database** |
| Command processing | Partially implemented, offline tests only | `command_processor.py` processes ONE command per run (no loop). Planner failures are recorded end to end. A command the planner accepts is left `received` and claimed, with nothing written. **Never run against the database yet** |
| Planner | Implemented and tested, deterministic | `planner.py` parses ASCII-only `set joint _1 to 0.25 rad` clauses separated by `;`. No language model |
| Cyberwave connection | Implemented, read-only | `cyberwave_client.py` connects in simulation mode and reads the joint schema. It cannot move anything |

Facts that must not be lost:

- No physical robot movement has been tested, and no Cyberwave simulation has been started.
- Capabilities reported by the Cyberwave API have been read, not physically verified.
- The validator is not a complete physical safety envelope.
- Two scripts write to Supabase: the one-time smoke test and `command_processor.py`. Both
  need `--confirm-write`. The smoke test has not been executed, and `command_processor.py`
  has never been run against the database: its tests use mocks and an in-memory fake
  client. A read-only check has run: both tables are reachable and have the expected columns.

## Layout

| File | Purpose |
|---|---|
| `config.py` | Loads settings from `worker/.env` or the environment. Never prints values |
| `schemas.py` | Typed shapes mirroring `db/001_init.sql` (statuses, event types, rows) |
| `cyberwave_client.py` | `SimulatedRobot`: connect in simulation mode, read joints. No movement |
| `supabase_client.py` | `AuditStore`: insert into `commands` and `audit_events` (used by the smoke test) |
| `audit_store.py` | Canonical writer: claim a command, append an audit event, conditional status update |
| `plan_hasher.py` | RFC 8785 + SHA-256 hash of a plan's actions array |
| `robot_profile.py` | SO-101 joint names and schema position limits used by the validator |
| `safety_validator.py` | Deterministic validator for a plan's actions. Pure, never raises, fails closed |
| `critic_gate.py` | Builds the critic request and judges a critic response. Pure, sends no request |
| `execution_gate.py` | Allows only if validator and critic both passed the same plan. Executes nothing |
| `planner.py` | Deterministic parser from command text to `set_joint` actions, or a structured planner error |
| `command_processor.py` | Claim one command, plan it, record a planner failure. Manual run needs `--confirm-write` |
| `supabase_read_check.py` | Manual script: read-only Supabase check, zero-row SELECTs only |
| `test_cyberwave_connection.py` | Manual script: Cyberwave connection check |
| `discover_cyberwave_assets.py` | Manual script: read-only asset lookup, creates nothing |
| `supabase_smoke_test.py` | Manual script: one-time Supabase write check. Needs `--confirm-write`, and refuses to write a second time |
| `tests/` | Unit tests. All offline: no network, no database, no robot |

The modules are plain files, not a package, so run everything **from the `worker/`
directory**.

## Setup

Python 3.12, virtual environment in `worker/.venv/` (inside `worker/`, not at the repository
root).

| File | Contents |
|---|---|
| `requirements.txt` | runtime dependencies only |
| `requirements-dev.txt` | `-r requirements.txt` plus the test runner, `pytest==9.2.0` |

Runtime only:

```
cd worker
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Development (runtime plus tests), in PowerShell from the repository root:

```
cd worker
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

If PowerShell refuses to run `Activate.ps1` because of the execution policy, skip activation
and call the interpreter directly: `.venv\Scripts\python.exe -m pip install -r
requirements-dev.txt`, then `.venv\Scripts\python.exe -m pytest -q`.

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

Unit tests (offline, safe to run any time). pytest runs every test, including the
pytest-style ones in `tests/test_audit_store.py`; `unittest discover` runs only the
`unittest.TestCase` classes:

```
.venv\Scripts\python.exe -m pytest -q
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
  (`DUPLICATE_ACTION_ID`)
- at most 10 actions (`MAX_ACTIONS_EXCEEDED`, from `SO101_PROFILE.max_actions`, which must
  equal `"maxActions"` in `shared/so101-limits.json`, the value the critic reads; a parity
  test enforces it)
- each joint is targeted by at most one action (`DUPLICATE_JOINT_TARGET`, reported on every
  later action that repeats a known joint). This is a separate rule from
  `DUPLICATE_ACTION_ID`
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

How this lines up with docs/schema-draft.md ("Action vocabulary" and "Validator result"),
which is the external contract:

| Point | What the validator does | Contract |
|---|---|---|
| Action vocabulary | accepts `set_joint` only | DECIDED: `set_joint` only |
| Target names | accepts `_1` to `_6` only | DECIDED |
| `unit` | required, must be exactly `rad` | DECIDED: mandatory, exactly `"rad"` |
| Maximum actions | 10, enforced | DECIDED: 10 (same as the critic) |
| One action per joint | `DUPLICATE_JOINT_TARGET` | DECIDED |
| Rule ids | `AGREED_RULE_IDS` (eight, including `MAX_ACTIONS_EXCEEDED` and `DUPLICATE_JOINT_TARGET`) plus `INVALID_PLAN`, `INVALID_FIELD_TYPE`, `DUPLICATE_ACTION_ID`, `UNEXPECTED_FIELD` | all listed as agreed |
| `validatorVersion` | `"1.0"` | DECIDED: the plain string `"1.0"`, bumped by hand when the rules change |

The version moved from the provisional `"0.1.0"` to the agreed `"1.0"` together with
`DUPLICATE_JOINT_TARGET` and the active cap. No `validator_result` event has been written
yet, so no stored event carries the old version.

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

Not built yet: the HTTP call to `/api/critic`, the successful-plan path (`plan_generated`
with a plan, `validator_result`, `critic_result`), and any execution.

## Command processor

`command_processor.process_one_command(worker_id)` handles at most one command:

1. `audit_store.claim_next_command`. Nothing eligible: returns `no_command`, writes nothing.
2. Plans `row["text"]` with `planner.plan_command`.
3. Planner failure: appends `plan_generated` with
   `{"outcome": "error", "error": {"kind", "detail"}}` (detail at most 200 characters), then,
   **only if that write succeeded**, `set_command_status(received -> failed)`. The two kinds
   (DECIDED in docs/schema-draft.md) are:
   - `unsupported_command`: the text is not in the grammar below. Message
     `Command not understood: nothing executed`.
   - `planner_error`: any other planner failure (the text is not a string, or an internal
     error). Message `Planning failed: nothing executed`.
4. Planner success: writes nothing and returns `planned_not_processed`.

Planner grammar: `set joint <_1.._6> to <decimal> rad`, up to 10 clauses separated by `;`.
The whole command must be plain ASCII: Arabic-Indic or fullwidth digits, Unicode
lookalike letters, non-breaking spaces and so on are rejected before parsing. Keywords are
case-insensitive (ASCII only). Joint names and the unit are read from `SO101_PROFILE`.
Empty clauses, other units, exponents, `nan`/`inf` and other joints are
`unsupported_command`. Each action's `reason` is at most 200 characters. Out-of-limit values
and a joint named twice are passed through unchanged: the validator rejects them
(`JOINT_LIMIT_EXCEEDED`, `DUPLICATE_JOINT_TARGET`), and so does the critic.

**Before a rehearsal**, check in the dashboard that no other `received` command is
waiting. The claim takes the **oldest** eligible command, which may be an old test command
rather than the one just typed, and whatever it claims is failed or stranded for good.

**A command can be stranded.** The claim has no lease and no recovery, so the command stays
`received` and claimed, and no later run picks it up, when:

- the `plan_generated` write fails (stage `event_write`; status untouched; after a
  `WriteError` the event may or may not exist),
- the event is written but the status update fails (stage `status_update`), or
- the planner accepts the command (`planned_not_processed`: the rest of the pipeline does
  not exist yet, and a valid plan is deliberately NOT marked failed).

`CommandProcessingError` carries `command_id`, `stage` (`claim`, `event_write` or
`status_update`) and `event_written` (True, False, or None when unknown). Nothing is retried
automatically, and nothing releases a claim.

### Manual recovery

Each step below is a deliberate write by a person, run once from `worker/`. Look first: in
the dashboard, open the command and read its timeline and status.

| What failed | What the timeline shows | What to do |
|---|---|---|
| `status_update` | one `plan_generated` error event, status still `received` | Set the status only. Do **not** write the event again. The update is conditional on `received`, so running it a second time changes nothing and raises `StateConflictError`. |
| `status_update` with `StateConflictError` | the event, and a status other than `received` | Someone else changed the status. Leave it; investigate who. |
| `event_write`, `event_written` None | check: the event may or may not be there | If the `plan_generated` error event is there, set the status only (row above). If not, write the event once, then set the status. Never write it without looking: `audit_events` rows cannot be deleted. |
| `event_write`, `event_written` False | no `plan_generated` event | The request was never sent (for example the payload was rejected). Fix the cause before writing anything. |
| `planned_not_processed` | no worker event, status `received`, `claimed_by` set | Nothing to recover automatically. It waits for the validator, critic and execution steps. Failing it by hand is a human decision, not something the worker does. |
| `claim` | nothing | A row may have been claimed: look for a `received` command whose `claimed_by` is this worker id. |

Setting the status by hand, after reading the timeline (replace the id):

```
.venv\Scripts\python.exe -c "import audit_store; audit_store.set_command_status('<command-id>', expected_status='received', status='failed')"
```

While a command has its `plan_generated` error event but is still `received`, the
dashboard summary already reads "Command not understood: nothing executed" (events take
priority over status in `src/lib/summary.ts`). The status column shows `received` until the
status is fixed.

Manual run (claims a real command and writes to Supabase):

```
.venv\Scripts\python.exe command_processor.py --confirm-write
```

## Safety rules for this code

- Simulation only. `SimulatedRobot` refuses to continue if the SDK client is not in
  simulation mode.
- No movement code exists yet. It is to be added only behind the validator and critic
  gates (fail closed).
- Secrets come from the environment only. `config.redact()` scrubs them from third-party
  error text before it is printed.

See `docs/robot-capabilities.md` for what is known about the SO-101 twin.
