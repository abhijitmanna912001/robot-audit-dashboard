# Python worker

Robotics and execution side of the auditable robot agent. Simulation only.

**This is a decision pipeline with a dry run, not a working agent.** `command_processor.py`
claims one command and plans it. A planner failure is recorded and the command set to
`failed`. A valid plan is validated by the Python safety validator, sent to `/api/critic`,
both results are recorded, and the command is decided `approved`, `rejected` or `failed`.
An approved command then goes through the **dry-run** lifecycle and ends `completed` or
`failed`. **Nothing is sent to a robot**: no Cyberwave call, no simulation, no movement,
and no flag, setting or environment variable can change that.

## Status

| Part | Status | What exists |
|---|---|---|
| Configuration | Implemented and tested | `config.py` |
| Plan hashing | Implemented and tested | `plan_hasher.py`. Matches the shared Python/TypeScript reference vector |
| Safety validator | Implemented and tested, called by the processor | `safety_validator.py` checks the agreed action vocabulary, schema position limits, one action per joint and the 10-action cap (`validatorVersion` `"1.0"`). Position limits only: no velocity, effort or collision checks (see "Safety validator") |
| Critic handling | Implemented, offline tests only | `critic_gate.py` builds the request and judges a response. `critic_client.py` sends it (one request, `x-critic-secret`, finite timeouts, no redirects, no retries). **The worker-to-critic request has not been made in a controlled live run yet** |
| Plan snapshot | Implemented and tested | `plan_snapshot.py` freezes a plan as RFC 8785 canonical bytes; the validator, the critic and the audit event each get a copy of exactly the hashed plan |
| Execution gate | Implemented, used only as a consistency check | `execution_gate.py` is a pure decision function. The processor calls it once before writing `approved`. It executes nothing |
| Execution | Dry run only, offline tests only | `dry_run.py` re-hashes the approved plan snapshot and returns a summary; `command_processor.py` records `execution_started`, `execution_completed` or `execution_failed` with `dryRun: true`. No movement code exists, and there is no real-execution path or switch. **The dry-run lifecycle has only run against mocks and the in-memory fake** |
| Audit logging | Implemented | `audit_store.py` is the canonical writer: claim a command, append one audit event (no retry), and conditional status updates checked against the agreed lifecycle. Used live once, by the planner-failure rehearsal. `supabase_client.py` is the older insert-only helper used by the smoke test |
| Command processing | Implemented. Planner-failure path verified live; successful-plan path offline tests only | `command_processor.py` processes ONE command per run (no loop). Planner failures, validator and critic results and the final status are recorded. The planner-failure path ran once against the live database (14 Nov 2026: `command_received`, `plan_generated` error, status `failed`). **The successful-plan path (validator, critic, `approved`/`rejected`/`failed`) has only run against mocks** |
| Planner | Implemented and tested, deterministic | `planner.py` parses ASCII-only `set joint _1 to 0.25 rad` clauses separated by `;`. No language model |
| Cyberwave connection | Implemented, read-only | `cyberwave_client.py` connects in simulation mode and reads the joint schema. It cannot move anything |

Facts that must not be lost:

- No physical robot movement has been tested, and no Cyberwave simulation has been started.
- Capabilities reported by the Cyberwave API have been read, not physically verified.
- The validator is not a complete physical safety envelope.
- Two scripts write to Supabase: the one-time smoke test and `command_processor.py`. Both
  need `--confirm-write`, and `command_processor.py` also calls the critic. The smoke test
  has not been executed. `command_processor.py` has run live once, for a planner failure
  only; its successful-plan path has run only in tests, against mocks, an in-memory fake
  client and `httpx.MockTransport`.

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
| `critic_client.py` | Sends ONE request to `/api/critic` and returns the `critic_gate` decision. Never raises |
| `plan_snapshot.py` | Immutable snapshot of one plan (canonical bytes, plan id, hash) |
| `execution_gate.py` | Allows only if validator and critic both passed the same plan. Executes nothing |
| `planner.py` | Deterministic parser from command text to `set_joint` actions, or a structured planner error |
| `command_processor.py` | Claim one command, plan, validate, ask the critic, record everything, set the status, dry-run an approved plan. Manual run needs `--confirm-write` |
| `dry_run.py` | The offline dry run: checks the approved snapshot against its hash and describes it. Sends nothing |
| `supabase_read_check.py` | Manual script: read-only Supabase check, zero-row SELECTs only |
| `test_cyberwave_connection.py` | Manual script: Cyberwave connection check |
| `discover_cyberwave_assets.py` | Manual script: read-only asset lookup, creates nothing |
| `supabase_smoke_test.py` | Manual script: one-time Supabase write check. Needs `--confirm-write`, and refuses to write a second time |
| `tests/` | Unit tests. All offline: no network, no database, no robot. `tests/net_guard.py` blocks all network access |
| `conftest.py`, `pytest.ini` | Install the network guard before pytest collects any test |

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
| `CRITIC_URL` | critic call | required by `command_processor.py` for a valid plan. `https` only (plain `http` only for a loopback host). Missing or unusable: the critic result is an error and nothing is approved |
| `CRITIC_SHARED_SECRET` | critic call | required, same as above. Sent only as the `x-critic-secret` header |

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

### The test suite is network-isolated

**The entire Python test suite is network-isolated, and real HTTP calls are forbidden in
tests.** `tests/net_guard.py` installs a process-wide guard before any test module is
imported:

- pytest: `worker/conftest.py` installs it before collection. `worker/pytest.ini` makes
  `worker/` the rootdir for any path inside it, so this also holds for
  `python -m pytest worker` or a single test file run from the repository root. The run
  header shows `worker network guard: installed`.
- unittest: `worker/tests/__init__.py` installs it before any `tests.*` module.

Blocked, raising `NetworkBlocked` before anything leaves the process:
- the default httpx transports (`httpx.Client` and `httpx.AsyncClient`)
- httpcore pools and backends
- `http.client`, which covers `urllib` and urllib3
- `requests`, if installed
- socket `connect` / `connect_ex` / `sendto`, and `create_connection`
- DNS lookups (`getaddrinfo` and friends)
- asyncio connection, datagram and resolver calls

The only local exception is `socket.socketpair()`, which asyncio needs to create an event
loop. Tests also cannot load the real critic settings
(`critic_client.load_critic_settings` is blocked), and `worker/.env` is never read
(`load_dotenv` is a no-op).

`NetworkBlocked` derives from `BaseException`, so the worker's fail-closed
`except Exception` handlers cannot hide a blocked request as an ordinary critic error:
the test fails loudly. `tests/test_network_guard.py` proves each blocked path, using only
`.invalid` hostnames and loopback.

Rules for tests:

- **Critic tests must use `httpx.MockTransport`** (or inject a fake `critic` into
  `process_one_command`). There is no live fallback and no opt-out.
- Database tests use autospecced `audit_store` mocks, the in-memory `FakeClient`, or the
  real PostgREST client on `httpx.MockTransport`.
- **Live critic authentication is verified only through a separately approved, controlled
  rehearsal** (`command_processor.py --confirm-write`), never by a test.

Note: before this guard existed, one pytest run on 14 Nov 2026 sent about four requests to
the deployed critic, through tests that fell back to the default critic client. Those
requests were an accident, **not a controlled authentication test**, and do not count as
verifying live critic authentication.

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
verdict, `commandId`, `planId` and `planHash` all equal the request, `verdict` is
exactly `approve`, and `triggeredRules` is an empty list. An `approve` that lists triggered
rules contradicts itself: it is an `invalid_body` error, never an approval and never a
reject. Everything else is one of two things:

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

Only the agreed verdict fields of a response are kept in the `critic_result` payload
(`commandId`, `planId`, `planHash`, `verdict`, `reason`, `triggeredRules`). `reason` is
capped at 200 characters, `triggeredRules` at 20 entries of 64 characters, and any other
field the critic returns is dropped (`critic_gate.sanitize_response`).

Open points in the critic contract:

- No error kind is defined for a `commandId`, `planId` or `planHash` mismatch. The worker
  writes `invalid_body` with a detail saying which one.
- No error kind is defined for a network error that is not a timeout. The worker writes
  `timeout` with a detail saying what happened. This is provisional.
- **New, unresolved:** no error kind is defined for "no request was sent" (missing or
  unusable `CRITIC_URL` / `CRITIC_SHARED_SECRET`, invalid timeout). The worker writes
  `timeout` with detail `critic request not sent: ...`, by the same reasoning as the network
  error (no response arrived). Provisional, to agree with Abhijit.

### Critic HTTP client

`critic_client.review_plan(request)` makes the call. It never raises: every problem becomes
an `error` decision, never an approval and never a skip.

| Rule | How |
|---|---|
| Authentication | `x-critic-secret: <CRITIC_SHARED_SECRET>` header. The secret is never in the body, the URL, an audit payload, an exception message or output. `CriticSettings.__repr__` hides it |
| Configuration | `config.load_critic_settings()` (`CRITIC_URL`, `CRITIC_SHARED_SECRET`). No fallback names. Missing, blank, non-`https` (except loopback `http`), or a URL with credentials, query or fragment: error, no request |
| Redirects | `follow_redirects=False`. Any 3xx is an `http_status` error ("redirect not followed"); the secret never reaches another host |
| Environment | `trust_env=False`: no proxy variables, `.netrc` credentials or other environment settings are applied to the request |
| Timeouts | `httpx.Timeout` with connect, read, write and pool all set, default 10 s, each capped at the total deadline. `build_timeout` refuses 0, negative, NaN, infinite, `None`, booleans and anything over 60 s |
| Total deadline | 10 s (`TOTAL_DEADLINE_SECONDS`), enforced by cancellation. The request runs on `httpx.AsyncClient` inside `asyncio.timeout(deadline)`, on a private event loop that each call creates and closes, so `review_plan` and the processor stay synchronous. The scope covers client setup, DNS/TCP/TLS, sending, response headers (however slowly they trickle), the body (`aiter_raw`) and closing the response and client. At the deadline, the read or connect in progress is cancelled, the response and client are closed, and the result is a `timeout` error. Extra checkpoints run after headers, after each chunk and after JSON decoding. Validated like the timeouts (over 0, at most 60 s). No background thread. Calling `review_plan` from inside a running event loop is refused (critic error). Not `asyncio.run`, whose shutdown can wait minutes for a blocked DNS thread |
| Retries | None, for anything. One worker run makes at most one critic request |
| Encoding | Sends `accept-encoding: identity`. Any `Content-Encoding` other than absent or `identity` (gzip, br, deflate, lists, ...) is an `invalid_body` error, and nothing is decompressed |
| Response | Non-200: error (body not read). 200: a declared `Content-Length` over 64 KB is refused before reading; otherwise the RAW bytes are read (`aiter_raw`) and reading stops as soon as they exceed 64 KB (exactly 65,536 bytes is accepted). Then strict JSON (no NaN), secret values scrubbed from its strings, and `critic_gate.evaluate_critic_response` |
| Error detail | Fixed text plus an exception class name only. Never exception text, headers or a raw body |

| Situation | Decision | `payload.error.kind` |
|---|---|---|
| 200, valid body, ids and hash match, `verdict: "approve"`, `triggeredRules: []` | approve | |
| 200, valid body, ids match, `verdict: "reject"` (any `triggeredRules`) | reject | |
| 401 | error | `http_status` (401) |
| other non-200, including 3xx | error | `http_status` |
| timeout (any phase) or total deadline passed | error | `timeout` |
| connection or protocol failure | error | `timeout` (provisional, detail names the error class) |
| malformed JSON, not an object, missing field, unknown verdict, too large, compressed or unknown encoding, `approve` with triggered rules | error | `invalid_body` |
| `commandId`, `planId` or `planHash` mismatch | error | `invalid_body` |
| configuration missing or unusable | error | `timeout` (provisional, see above) |

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
4. Planner success (the successful-plan lifecycle):
   1. A new `planId` (UUID, generated once) and a `PlanSnapshot`: the actions frozen as
      RFC 8785 canonical bytes, `planHash = plan_hasher.hash_actions(...)` of them.
   2. Append `plan_generated` with `{"plan": {commandId, planId, planHash, actions},
      "planId", "planHash"}`, message `Plan generated: N action(s)`. **If this fails,
      nothing else happens**: no check runs and the status is not touched.
   3. Run the safety validator on one copy of the snapshot and send another copy to the
      critic. Both always run, even when the other one rejects.
   4. Append `validator_result` (the `ValidationResult.to_payload()` shape), then
      `critic_result` (`CriticDecision.to_payload()`, sanitized). The second is attempted
      even if the first insert failed. Neither is retried.
   5. Only if every result event was written: ONE conditional status update
      `received -> approved | rejected | failed`, which also sets `commands.plan_id`.

   Final status precedence (agreed, `decide_final_status`):

   | Validator | Critic | Status |
   |---|---|---|
   | pass | approve | `approved` |
   | fail | reject | `rejected` |
   | fail | error | `rejected` |
   | error | reject | `rejected` |
   | pass | reject | `rejected` |
   | pass | error | `failed` |
   | error | approve | `failed` |
   | error | error | `failed` |

   A rejection by either check wins over an error in the other. `failed` means no check
   rejected but at least one errored. `approved` needs a validator pass AND a critic
   approval of exactly this command, plan id and hash; before writing it, the processor
   also asks the pure `execution_gate.decide_execution` to re-validate and re-hash the
   request, and treats a disagreement as a critic error.

5. Approved plans only (validator pass AND critic approve, `received -> approved` written):
   the **dry-run lifecycle** (docs/schema-draft.md sections 2 and 6).

   | Step | Write | Only if |
   |---|---|---|
   | 1 | `execution_started` `{"dryRun": true}` | |
   | 2 | `approved -> executing` | step 1 was written |
   | 3 | `dry_run.run_dry_run(snapshot)` | step 2 succeeded |
   | 4 | `execution_completed` `{"dryRun": true, "summary"}` | step 3 succeeded |
   | 5 | `executing -> completed` | step 4 was written |

   The dry run gets the same immutable `PlanSnapshot` that was validated and sent to the
   critic. The command text is never parsed again. It re-hashes the snapshot, sends
   nothing anywhere and returns a summary such as `Dry run only, nothing sent to a robot.
   Would set a1: _1 = 0.25 rad.`.

   | Failure | What happens |
   |---|---|
   | `execution_started` write fails | No dry run. `execution_failed` is written, then `approved -> failed`. |
   | `approved -> executing` not applied (refused, or `WriteError`) | No dry run. `execution_failed`, then the conditional `approved -> failed` (it cannot overwrite `executing`). |
   | `approved -> executing` matches no row (`StateConflictError`) | No dry run. `execution_failed` is attempted once; the status is unknown and not touched. Stage `executing_update`. |
   | the dry run raises | `execution_failed`, then `executing -> failed`. |
   | `execution_completed` write fails | The dry run succeeded; status stays `executing`. Stage `execution_completed_write`. |
   | `executing -> completed` fails | `execution_completed` was written. Not retried, not written again, dry run not rerun. Stage `completed_update`. |
   | `execution_failed` write fails | The failure is NOT in the audit log, so the status is not changed. Stage `execution_failed_write`. |
   | `-> failed` fails after `execution_failed` | Stage `failed_update`. |

   `execution_failed.error` is fixed text plus an exception class name (at most 200
   characters), never exception text. Event messages: `Dry run started: nothing is sent to
   a robot`, `Dry run completed: nothing was sent to a robot`, `Dry run failed: nothing was
   sent to a robot`. The dashboard summary reads the payloads ("Executing (dry run)", "Dry
   run completed", "Dry run failed").

   `ProcessResult.decision_status` is what the checks decided. `final_status` is where this
   run left the command, `execution` is the dry-run outcome and `execution_error` says why
   it failed.

   **Validator "error" (new, unresolved contract question).** The validator never raises:
   it turns its own internal errors into `fail` (`INVALID_PLAN`). The processor still
   treats `validate_plan` raising, or returning something unusable, as `error`. The agreed
   `validator_result` payload only has `pass` and `fail`, so in that case **no
   `validator_result` is written**; the critic still runs and the status follows the table.
   The dashboard summary then has no validator event to explain a `failed` status. To agree
   with Abhijit: whether `validator_result` gets `outcome: "error"`.

Event messages: `Validator passed` / `Validator rejected the plan`; `Critic approved the
plan` / `Critic rejected the plan` / `Critic unavailable: no verdict`. The dashboard
derives its summary from the payloads, not from these messages.

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

- the `plan_generated` write fails (stage `event_write`; status untouched; for a valid plan
  no check ran; after a `WriteError` the event may or may not exist),
- a result event write fails (stage `validator_event_write` or `critic_event_write`; both
  checks ran, the other result event was still attempted, status untouched), or
- every event is written but the status update fails (stage `status_update`).

An approved command is left `approved` or `executing` for manual recovery when a dry-run
write fails in a way the worker cannot resolve safely (stages `executing_update`,
`execution_failed_write`, `failed_update`, `execution_completed_write`, `completed_update`).

`CommandProcessingError` carries `command_id`, `stage` (`claim`, `event_write`,
`validator_event_write`, `critic_event_write`, `status_update`, or one of the dry-run
stages above), `event_written` (for
the failed stage: True, False, or None when unknown), and, for a valid plan, `plan_id`,
`events_written` (event types confirmed written) and `intended_status` (what the status
would have been set to). Nothing is retried automatically, and nothing releases a claim.

### Manual recovery

Each step below is a deliberate write by a person, run once from `worker/`. Look first: in
the dashboard, open the command and read its timeline and status.

| What failed | What the timeline shows | What to do |
|---|---|---|
| `status_update` | one `plan_generated` error event, status still `received` | Set the status only. Do **not** write the event again. The update is conditional on `received`, so running it a second time changes nothing and raises `StateConflictError`. |
| `status_update` with `StateConflictError` | the event, and a status other than `received` | Someone else changed the status. Leave it; investigate who. |
| `event_write`, `event_written` None | check: the event may or may not be there | If the `plan_generated` error event is there, set the status only (row above). If not, write the event once, then set the status. Never write it without looking: `audit_events` rows cannot be deleted. |
| `event_write`, `event_written` False | no `plan_generated` event | The request was never sent (for example the payload was rejected). Fix the cause before writing anything. |
| `event_write` for a valid plan (`plan_id` set) | maybe a `plan_generated` event with that `planId`, nothing after it | No check ran. Do not re-run the checks by hand against the same plan id without deciding what the audit trail should say. Failing it (`received -> failed`) is a human decision. |
| `validator_event_write` / `critic_event_write` | `plan_generated`, plus the events in `events_written` | The message lists both checks' outcomes and `intended_status`. If an event with `event_written` None is in the timeline, do not write it again. A missing result cannot be rebuilt by the worker; decide by hand whether to set `intended_status` or `failed`. |
| `status_update` after a valid plan | `plan_generated`, `validator_result` (unless the validator errored), `critic_result`; status still `received` | Set the status only, to `intended_status`, with `plan_id`. Do **not** write any event again. |
| `claim` | nothing | A row may have been claimed: look for a `received` command whose `claimed_by` is this worker id. |
| `executing_update` | ... `critic_result`, `execution_started`, maybe `execution_failed`; status changed by someone else | The dry run did not run. Do not change the status until you know who changed it and why. |
| `execution_failed_write` | ... `execution_started` (maybe), no `execution_failed`; status `approved` or `executing` | The failure is not audited. If `event_written` is None, check the timeline first. Write `execution_failed` `{"dryRun": true, "error": ...}` once, then `approved|executing -> failed`. |
| `failed_update` | ... `execution_failed`; status `approved` or `executing` | Set the status only, to `failed`, from the status it has now. Do **not** write `execution_failed` again. |
| `execution_completed_write` | ... `execution_started`, maybe `execution_completed`; status `executing` | The dry run succeeded. If `execution_completed` is there, set `executing -> completed` only. If not, decide by hand; do not rerun the dry run. |
| `completed_update` | ... `execution_completed`; status `executing` | Set `executing -> completed` only. Do **not** write `execution_completed` again and do not rerun the dry run. |

Setting the status by hand, after reading the timeline (replace the id):

```
.venv\Scripts\python.exe -c "import audit_store; audit_store.set_command_status('<command-id>', expected_status='received', status='failed')"
```

After a valid plan, with the status and plan id from the error message:

```
.venv\Scripts\python.exe -c "import audit_store; audit_store.set_command_status('<command-id>', expected_status='received', status='<intended-status>', plan_id='<plan-id>')"
```

While a command has its `plan_generated` error event but is still `received`, the
dashboard summary already reads "Command not understood: nothing executed" (events take
priority over status in `src/lib/summary.ts`). The status column shows `received` until the
status is fixed.

Manual run (claims a real command, writes to Supabase and, for a valid plan, makes one
request to the critic; an approved plan is dry-run and its execution events are written):

```
.venv\Scripts\python.exe command_processor.py --confirm-write
```

## Safety rules for this code

- Simulation only. `SimulatedRobot` refuses to continue if the SDK client is not in
  simulation mode.
- No movement code exists. `command_processor.py` runs only the offline dry run
  (`dry_run.py`) after both gates approved, and writes only `dryRun: true`. There is no
  flag, setting, environment variable or injectable executor that could turn it into
  simulation or physical execution; real execution would be a separate, agreed change.
- Every test runs under the process-wide network guard (`tests/net_guard.py`, see "The
  test suite is network-isolated"). Critic responses come from `httpx.MockTransport` or
  an offline fake.
- Secrets come from the environment only. `config.redact()` scrubs them from third-party
  error text before it is printed.

See `docs/robot-capabilities.md` for what is known about the SO-101 twin.
