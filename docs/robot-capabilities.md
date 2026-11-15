# Robot capabilities: SO-101 (Cyberwave digital twin)

**Status: identity, twin and schema verified. Behaviour unverified.** Every value in this
file carries one of four source labels:

| Label | Meaning | How it was obtained |
|---|---|---|
| **Catalog** | Returned by the Cyberwave asset catalog | `worker/discover_cyberwave_assets.py`, run 2026-11-13 ([R1]: output not stored in the repo) |
| **Twin init** | Returned or observed while the twin was successfully initialized. Written **Twin init (API-reported)** for values in the API's twin response, such as the capability fields | `worker/test_cyberwave_connection.py`, run 2026-11-13 ([R1]: output not stored in the repo) |
| **Schema** | Read from the twin's universal schema | same connection test run, `twin.get_schema("/joints")` and `twin.joints.list()` |
| **Unverified** | Not observed against the platform | either only read from the installed SDK 0.7.5 source ("SDK source"), or unknown ("TBD") |

Nothing in this file is guessed. An unknown value stays TBD.

### Citation rules and sources

Every statement about SDK or platform behaviour in this file cites one of the sources
below, or sits in an **Unverified** list. A claim that cannot be cited is not stated as fact.

| Tag | Source |
|---|---|
| **SDK `file:line`** | The installed Cyberwave SDK 0.7.5, `worker/.venv/lib/python3.14/site-packages/cyberwave/` (on Windows: `worker/.venv/Lib/site-packages/cyberwave/`). Line numbers were re-checked against that install on 2026-11-15. A different SDK version needs them re-checked |
| **[TS]** | https://docs.cyberwave.com/support/troubleshooting.md |
| **[SIM]** | https://docs.cyberwave.com/overview/features/simulation.md |
| **[PY]** | https://docs.cyberwave.com/overview/tools/python-sdk.md |
| **[MQTT]** | https://docs.cyberwave.com/api-reference/mqtt/main.md |
| **[CC]** | https://docs.cyberwave.com/get-started/core-concepts.md |
| **[R1]** | Our own run on 2026-11-13 of `worker/test_cyberwave_connection.py` and `worker/discover_cyberwave_assets.py`. **Evidence: the terminal output of that run, read by the author. It is not stored in this repo, so it cannot be re-checked from here.** Treat every [R1] value as reported, not reproducible from the repo |
| **[S]** | A text search on 2026-11-15 of `worker/cyberwave_client.py`, `test_cyberwave_connection.py`, `discover_cyberwave_assets.py` and `read_joint_telemetry.py` for calls to `affect`, `set_joints`, `grip`, `release`, `set_pose`, `run_movement`, `twin(`, and attribute assignment on `joints`. Only comments and docstrings matched. This shows the scripts contain no such call. It does not show what else was ever done to the twin (for example from the Cyberwave web app) |

The five documentation pages were read on 2026-11-14 (as recorded by the author). Statements
that rely on them are **paraphrases**, each followed by the page link and "read 2026-11-14".
They are not verbatim quotations: a later re-check went through a summarising fetch tool, so
exact wording was not confirmed.

**API-reported does not mean physically verified.** A capability field in the API
response is the platform's declaration about the asset. It has been read, not exercised.
A label of Catalog, Twin init or Schema says where a value came from. It never says the
behaviour behind it has been tested.

### What is verified, by source

**Catalog** (section 1): asset name, asset UUID, registry id, catalog slug, visibility,
supported simulation backends, and the lookup behaviour with and without the workspace
filter.

**Twin init** (sections 1 and 3): twin UUID, twin class, all twenty capability fields in
the twin's API response, reuse of the existing Quickstart Environment, and a successful
MQTT connection and subscription.

**Schema** (sections 2 and 6): joint count, joint names, joint types, and per-joint
position, velocity and effort limits.

**Unverified** (sections 3, 5 and 7): current joint positions, the physical meaning of
each joint, every action that moves the robot, and gripper operation.

### What has not been tested

- **Robot movement is untested.** No joint command is sent by the worker's scripts ([S]).
- **Gripper execution is untested.** `can_grip: true` is a capability field in the twin's
  API response. It says the platform classes the asset as having a gripper. It is not
  evidence that `grip()` or `release()` works, and neither has been called.
- **Current joint positions are unavailable.** No telemetry has been received (section 3).
- **Zero-fill values are not measurements.** The 0.0 values `get_joints()` returned are
  SDK placeholders.
- **No simulation was started by the worker's scripts**, and they sent nothing to live
  hardware. Evidence: [S] (the scripts contain no `affect()` or command call). Whether
  anyone started a simulation or used hardware by other means is **unverified**.

Two kinds of joint data appear in this file, and they must not be confused:

- **Schema limits** (section 6): static numbers the Cyberwave universal schema declares
  for the asset. They describe what the model allows, not where the arm is.
- **Measured state** (section 3): joint positions reported by a running simulation or
  robot. None has been received yet.

Simulation mode only. The worker's scripts did not start a simulation or command anything,
and contain no joint, gripper or motion command ([S]). That is the whole evidence: nothing
else about the twin's history is known from this repo, so "no command has ever been sent to
this twin" is **unverified**.

## 1. Robot asset

### 1.1 Asset identity (Catalog)

| Item | Value | Source |
|---|---|---|
| Asset name | SO-101 | Catalog |
| Asset UUID | `89696429-d5d7-4243-85be-ac0d3c148044` | Catalog. Matches the SDK's bundled data |
| Canonical registry id | `the-robot-studio/so101` | Catalog |
| Catalog slug | `the-robot-studio/catalog/so101` | Catalog |
| Visibility | public | Catalog |
| Supported simulation backends | `playground`, `mujoco` | Catalog |

### 1.2 Our twin (Twin init)

| Item | Value | Source |
|---|---|---|
| Twin UUID | `100cb801-daf0-46eb-90ca-7103f5fc5b1d` | Twin init ([R1]) |
| Twin class | `GripperJointTwin` | Twin init ([R1]). See the note below on how the SDK picks it |
| Environment | reported as the existing "Quickstart Environment", reused, none created. Its UUID is not recorded here. Evidence: [R1] only (printed output, not stored). `Cyberwave.twin()` uses that environment when none is configured: SDK `client.py:999`; the Python SDK page says the SDK auto-creates a Quickstart Environment when no default is configured (https://docs.cyberwave.com/overview/tools/python-sdk.md, read 2026-11-14); the troubleshooting page says `cw.twin(...)` uses it when no environment is set (https://docs.cyberwave.com/support/troubleshooting.md, read 2026-11-14). That it was created earlier, and by whom, is unverified | Twin init ([R1]) |
| Runtime mode of the client | `simulation` | Twin init ([R1]). The constructor argument is `mode`, SDK `client.py:315`. That no simulation instance was started is supported by [S] only |

Note on the twin class: `GripperJointTwin` is reported ([R1]) as the Python class the SDK
instantiated for our twin. The SDK picks a class in `create_twin()` from the capability data
bundled in its own package for the registry id, and uses the twin's own response only when
that is empty (`twin/factory.py:141-145`). The class is defined at `twin/classes.py:1119`
and has `get_joints` (`twin/mixins.py:93`), `set_joints` (`twin/mixins.py:133`), `grip`
(`twin/classes.py:1107`) and `release` (`twin/classes.py:1111`). So it shows which SDK
methods exist on the object. It does not show that those methods do anything on the
platform.

How the worker refers to the twin and the asset (changed on 2026-11-14):

- The worker **looks the existing twin up by its id. It no longer calls `cw.twin()`**, which
  is what the connection test on 2026-11-13 used (the "Twin init" rows above record that
  run and are not changed by this).
- Three settings are required to load the twin, each a canonical UUID:
  `CYBERWAVE_ENVIRONMENT_ID`, `CYBERWAVE_TWIN_ID` and `ROBOT_ASSET_IDENTIFIER` (the
  **asset UUID**). The asset UUID is no longer passed to the SDK to find a twin. It is only
  compared with the twin's own `asset_uuid`.
- The registry id is kept separately, as `SO101_REGISTRY_ID` in
  `worker/discover_cyberwave_assets.py`, because discovery looks the asset up by registry
  id. A registry id is refused as `ROBOT_ASSET_IDENTIFIER` for twin loading: it is never
  resolved and never compared with a UUID.

#### Existing-twin lookup

Three kinds of statement are kept apart here: what the SDK source says, what the worker
does about it, and what the platform has not confirmed.

**A. Read from the installed Cyberwave SDK 0.7.5 source.** Paths are under
`worker/.venv/lib/python3.14/site-packages/cyberwave/` (see the citation rules above). These are facts about the client code. By the
labels at the top of this file they are **Unverified (SDK source)**: none of them has been
exercised against the platform by the lookup path.

| Fact | Where |
|---|---|
| `Cyberwave.twin()` is get-or-create. Its docstring reads "Get or create a twin instance". With no matching twin it calls `self.twins.create(...)` | `client.py:942-1036`; the create call is at `client.py:1033-1035` |
| With no environment configured, `Cyberwave.twin()` calls `get_or_create_quickstart_environment()`, which calls `self.environments.create(...)` when no "Quickstart Environment" exists | `client.py:999`; the create call is at `client.py:936-939` |
| `client.twins.get_raw(twin_id)` (`TwinManager.get_raw`) is a lookup: it returns `self._resolve_twin(twin_id)` and contains no create call | `resources.py:2950-2961` |
| For a value that parses as a UUID, `_resolve_twin()` makes one call, `src_app_api_twins_get_twin(twin_id)` | `resources.py:2963-2966`; the UUID test is `_is_uuid()` at `resources.py:196-202` |
| That call is serialised as `GET /api/v1/twins/{uuid}` | `rest/api/default_api.py:140478` (method), `:140718-140719` (`method='GET'`, path) |
| The returned record (`TwinSchema`) carries the twin UUID, the asset UUID and the environment UUID | `rest/models/twin_schema.py:32` (`uuid`), `:36` (`asset_uuid`), `:37` (`environment_uuid`) |
| `client.twins.list(environment_id=...)` (`TwinManager.list`) calls `src_app_api_environments_get_environment_twins`, serialised as `GET /api/v1/environments/{uuid}/twins` | `resources.py:2917-2923`; `rest/api/default_api.py:50195` (method), `:50437-50438` (`method='GET'`, path) |

So the lookup makes network requests, and both are GET. It is not "no Cyberwave calls".

**B. Application safeguards in the worker.** These are checks in our code
(`config.load_cyberwave_twin_settings` in `worker/config.py`,
`SimulatedRobot.load_existing_twin` in `worker/cyberwave_client.py`), covered by offline
tests with a fake SDK (`worker/tests/test_config.py`, `worker/tests/test_cyberwave_client.py`):

- A missing, blank or malformed id stops with an error that names the variable. Only the
  hyphenated 8-4-4-4-12 UUID form is accepted, so the SDK always takes the direct GET and
  never its slug lookup.
- The twin is fetched with `client.twins.get_raw(twin_id)`. The worker refuses unless the
  record's `uuid` is the configured twin id.
- **Environment membership, two checks.** The record's `environment_uuid` must be the
  configured environment id, and the configured environment's own twin list
  (`client.twins.list(environment_id=...)`) must contain the twin id. Both answers come
  from the same platform through two different endpoints.
- **Asset identity.** The record's `asset_uuid` must be the configured asset UUID. A UUID
  is compared with a UUID only.
- Any failed call, unreadable field or mismatch refuses and leaves no twin loaded. There is
  no fallback to an asset-based lookup, to creating a twin or to creating an environment.
- No module in `worker/` calls `Cyberwave.twin()` or
  `get_or_create_quickstart_environment()`. A test fails if one is added.

**C. Not verified. Do not rely on any of these.**

- That `GET /api/v1/twins/{uuid}` never creates or changes anything on the server. The
  client code has no create call on this path; the server's behaviour has not been
  confirmed by Cyberwave.
- That an API key cannot reach live physical hardware. The SDK client defaults to
  `mode="live"` (`client.py:315`) and the troubleshooting page says live is the default when
  `cw.affect(...)` is never called, and that the SDK does not check for a paired edge before
  sending live commands (https://docs.cyberwave.com/support/troubleshooting.md, read 2026-11-14). No key scope or hardware restriction was found by reading the client source
  (a reading, not an exhaustive search).
- That simulation mode is a permanent, server-side restriction on live hardware. It is a
  setting of the client (`client.config.runtime_mode`), checked by the worker before it
  continues. Nothing cited shows that the platform enforces it. The core-concepts
  page describes Playground, Simulation and Live as separate modes and says code stays the
  same when switching from simulation to live (https://docs.cyberwave.com/get-started/core-concepts.md, read 2026-11-14); it says nothing about enforcement.
- That `sim.stop()` cancels in-flight SO-101 joint movements. In the source it requests
  the stop of a simulation run (`POST .../simulations/{simulation_id}/stop`) and sets the
  local status to `stopped` without re-reading it (`managers/simulations.py:212-217`,
  `:635-653`). The simulation page only says a run is stopped with `sim.stop()` (https://docs.cyberwave.com/overview/features/simulation.md, read 2026-11-14).
- That the SDK's joint-state view (`get_joints()`) is measured simulator state. It returns
  zeros when no update arrived, and a joint command writes its target into the same cache
  (`twin/capabilities/joints.py:327-341`, `:497-502`). See section 3.
- That telemetry source metadata proves a real measurement. The MQTT page lists `sim` as
  a source type meaning simulation state (https://docs.cyberwave.com/api-reference/mqtt/main.md, read 2026-11-14), a label the sender sets. Nothing cited says the
  platform checks it or that the positions were measured.

Lookup behaviour (Catalog, [R1]: reported output of `discover_cyberwave_assets.py`, not stored in the repo):

| Lookup | No workspace filter | With `workspace_uuid` filter |
|---|---|---|
| `assets.list_by_registry_id("the-robot-studio/so101")` | 1 asset | **0 assets** |
| `assets.get("89696429-d5d7-4243-85be-ac0d3c148044")` | 1 asset | 1 asset |

### 1.3 Capability flags

Twenty capability fields from the twin's API response are documented here. The connection
test printed them in its step 4, "Twin capabilities (as reported by the API)". All twenty
are **Twin init (API-reported)**, from [R1] (printed output, not stored in the repo).

**API-reported does not mean physically verified.** Every row below is the platform's
declaration about the asset, read from one API response. None of it has been exercised.
In particular, `can_actuate: true` and `can_grip: true` do not mean actuation or gripping
has been tested. Neither has.

All values below are copied from the API response printed by the connection test. None
is taken from the SDK's bundled data.

| Field | API-reported value | Source |
|---|---|---|
| `has_joints` | true | Twin init (API-reported) |
| `can_actuate` | true | Twin init (API-reported) |
| `can_grip` | true | Twin init (API-reported) |
| `can_locomote` | false | Twin init (API-reported) |
| `can_fly` | false | Twin init (API-reported) |
| `has_legs` | false | Twin init (API-reported) |
| `has_wheels` | false | Twin init (API-reported) |
| `manipulator_count` | 1 | Twin init (API-reported) |
| `locomotion_mode` | `stationary` | Twin init (API-reported) |
| `locomotion_config` | `{"dof": 0, "max_angular_velocity": 0.0, "max_linear_velocity": 0.0}` | Twin init (API-reported) |
| `navigation_autonomy_level` | `none` | Twin init (API-reported) |
| `navigation_obstacle_avoidance` | false | Twin init (API-reported) |
| `maximum_frequency_hz` | 0 | Twin init (API-reported) |
| `minimum_frequency_hz` | 0 | Twin init (API-reported) |
| `payload_capacity_kg` | 0.4 | Twin init (API-reported) |
| `reach_m` | null (no value provided) | Twin init (API-reported) |
| `power_source` | `tethered` | Twin init (API-reported) |
| `power_capacity_wh` | -1 | Twin init (API-reported) |
| `sensors` | `[]` (empty list) | Twin init (API-reported) |
| `calibration_hand_eye` | true | Twin init (API-reported) |

For reference only: the author reports ([R1]-style, no stored evidence) that the same
twenty fields in the SDK's bundled data (`assets_capabilities.json`) hold the same values.
That check is not repeatable from this file. It is not where the values above come from.

Reading the fields:

- `has_legs`, `has_wheels`, `can_locomote` and `can_fly` are all false, `locomotion_mode`
  is `stationary`, and every entry in `locomotion_config` is 0. The API reports a stationary
  asset. The SDK defines locomotion commands on locomotion twin classes (`LocomoteTwin`,
  `twin/classes.py:454`), and `GripperJointTwin` is not one (`twin/classes.py:1119`), so
  `move_forward` is not offered on it (section 5).
- `navigation_autonomy_level: none` and `navigation_obstacle_avoidance: false` are the
  reported values. What the platform does or does not do to prevent collisions is
  **unverified** (section 7).
- `maximum_frequency_hz: 0` and `minimum_frequency_hz: 0`: none of the cited sources
  explains zero here. It is not taken to be a control rate.
- `power_source: tethered`: the reported value.
- `power_capacity_wh: -1`: API returned -1; meaning not confirmed. No meaning is inferred
  here without confirmation from Cyberwave.
- `reach_m: null` is not a measurement of the arm. A null reach means the platform gives
  no reach figure, so workspace reach stays unknown (section 7).
- `sensors: []` is the reported value: no sensors listed for this twin. Separately,
  the core-concepts page says the Playground has no camera frames (https://docs.cyberwave.com/get-started/core-concepts.md, read 2026-11-14) and the
  troubleshooting page says it does not render sensors (https://docs.cyberwave.com/support/troubleshooting.md, read 2026-11-14).
- `payload_capacity_kg: 0.4` is a declared figure. No payload has been tested.

## 2. Joints

| Item | Value | Source |
|---|---|---|
| Joint count | 6 | Schema ([R1]) |
| Joint names | `_1`, `_2`, `_3`, `_4`, `_5`, `_6` | Schema ([R1]) |
| Joint types | all six are `revolute` | Schema ([R1]) |
| Physical meaning of each joint | **TBD** | Unverified. Not provided by the schema |

The names are exactly as the schema gives them: an underscore and a digit. **Which
physical joint each one is (base, shoulder, elbow, wrist, gripper) is not known and is not
assumed here.** Nothing in the data retrieved so far maps `_1` to `_6` onto parts of the
arm. Until that mapping is confirmed, worker code must address joints only by these names
and must not attach meanings to them.

The SDK's calibration docstrings use `shoulder_pan` as an example joint name
(`twin.py:1701`, `:1728`). That is an example in a docstring. It is not a name on our twin and is not a mapping.

How names are discovered: `twin.joints.list()` returns the joints of type revolute,
prismatic or continuous from the twin's universal schema, **sorted by name**
(`twin/capabilities/joints.py:37`, `:80-95`, `:214-215`; the Python SDK page describes `joints.list()` as returning the controllable joint names from the
schema (https://docs.cyberwave.com/overview/tools/python-sdk.md, read 2026-11-14)). The SDK says to match joints by name, not position (`joints.py:80-82`).
Match joints by name, never by index.

## 3. Current joint positions (measured state)

**Unverified. Unavailable.** No joint position has been measured by the worker's scripts
([R1]: none received).

What happened in the connection test on 2026-11-13 ([R1]: printed output, not stored in the repo):

| Step | Result | Source |
|---|---|---|
| MQTT connection to the Cyberwave broker | succeeded | Twin init |
| Subscription to the twin's joint update topic | succeeded | Twin init |
| Joint update received within the wait window | **none** | Twin init |
| Values returned by `twin.get_joints()` | 0.0 for all six joints | placeholders, not data |

So MQTT connectivity is confirmed for connecting and subscribing only. The worker
published nothing, and no message was received.

Those zeros are **zero-fill placeholders from the SDK, not measurements**. When no joint
update arrives within the timeout, the SDK seeds zeros for every controllable joint instead
of raising (`twin/capabilities/joints.py:327-341`, `:238-245`). The default timeout is 3.0
seconds (`mqtt/state.py:10`, used at `twin/mixins.py:98`). Why no update arrived is
**unverified**: it may be that no simulation was running, but nothing cited establishes
that (see section 7).

So the zeros do not mean the arm is at its zero pose, and they must not be used as a
starting position by the planner or the validator.

Getting real positions probably needs something to publish joint state. the simulation page
lists `pose` and `joints` data as available in any simulation runtime
(https://docs.cyberwave.com/overview/features/simulation.md, read 2026-11-14). Whether a simulation is required
for this twin is **unverified**. The worker's scripts start none ([S]). MuJoCo is billed,
see section 7.

## 4. Units

| Quantity | Unit | Source |
|---|---|---|
| Schema position limits (`lower`, `upper`) | assumed radians | **Unverified.** The schema values are from [R1]; no cited source states their unit. Radians is the URDF convention for revolute joints, which is an assumption here |
| Schema velocity limit (`velocity`) | assumed radians per second | **Unverified.** Same note |
| Schema effort limit (`effort`) | assumed newton metres | **Unverified.** Same note |
| Joint position, read (`get_joints`) | radians | SDK `twin/mixins.py:93-107` (docstring: "in radians"); the MQTT page gives joint position as radians for revolute and metres for prismatic joints (https://docs.cyberwave.com/api-reference/mqtt/main.md, read 2026-11-14). Unverified in practice: no telemetry has been received ([R1]) |
| Joint position, write (`set_joints`) | radians by default, degrees only if `degrees=True` | SDK `twin/mixins.py:133-147`; the Python SDK page says positions are radians by default and degrees need `degrees=True` (https://docs.cyberwave.com/overview/tools/python-sdk.md, read 2026-11-14). Never called by the scripts ([S]) |
| Joint velocity and effort telemetry | **TBD** | Unverified. The SDK names `velocity`, `acceleration` and `effort` kinds (`twin/mixins.py:105-107`) and the MQTT page gives no units for velocity or effort (https://docs.cyberwave.com/api-reference/mqtt/main.md, read 2026-11-14) |
| Gripper `force` | unitless, clamped to 0.0 to 1.0 | SDK `twin/capabilities/gripper.py:35` (`max(0.0, min(1.0, force))`). Never called ([S]) |
| Gripper `max_effort_nm` | newton metres (torque cap) | SDK `twin/capabilities/gripper.py:31`, `:36-37`. Valid range for SO-101: **TBD** (unverified) |
| Cartesian pose (`set_pose`) | metres, and radians or a quaternion (never degrees) | SDK `twin/mixins.py:163-185`. Never called ([S]) |

Open decision for the action schema (docs/schema-draft.md, open question 3): the worker
should pick one joint unit and convert at one boundary. Radians match the SDK default
(`twin/mixins.py:93-107`, `:133-147`).

## 5. Supported actions

What the SDK exposes on the `GripperJointTwin` class (`twin/classes.py:1119`). Only the
two read-only calls were run ([R1]). **The worker's scripts contain no call that moves the
robot or operates the gripper ([S]); none has been tested.** That these methods exist on the class, and that the twin reports
`can_actuate: true` and `can_grip: true`, shows the actions are offered. It does not show
that they work.

| Action | SDK call | Executed? | Source |
|---|---|---|---|
| List joints | `twin.joints.list()` | yes, read-only ([R1]) | Schema ([R1]): six names in section 2. SDK `twin/capabilities/joints.py:214` |
| Read joint positions | `twin.get_joints()` | yes, read-only ([R1]) | Twin init ([R1]): placeholders only, see section 3. SDK `twin/mixins.py:93` |
| Set joint positions | `twin.set_joints({name: value})` | never ([S]) | SDK `twin/mixins.py:133` |
| Close gripper | `twin.grip(force)` or `twin.gripper.grip(force)` | never ([S]) | SDK `twin/classes.py:1107`, `twin/capabilities/gripper.py:24` |
| Open gripper | `twin.release()` | never ([S]) | SDK `twin/classes.py:1111` |
| Move tool to a Cartesian pose | `twin.set_pose(pose)` | never ([S]) | SDK `twin/mixins.py:163`. The docstring says the driver reports the result on `state/motion` (`:196-199`); whether a driver or planner is needed in simulation is unverified |
| Named movements | `twin.list_movements()`, `twin.run_movement(...)` | never ([S]) | SDK `twin/base.py:346`, `:354` |
| Stop | **TBD** | never ([S]) | Unverified. By reading `twin/classes.py` and `twin/capabilities/` no stop or halt method was found on `JointTwin` or `GripperTwin` (`classes.py:430`, `:1100`); `emergency_stop` is defined inside `FlyingTwin` (`classes.py:567`, `:753`) and `stop` on the locomotion handle (`twin/capabilities/locomotion.py:97`). A negative search, not proof that none exists |
| `move_forward`, locomotion | **not offered** on this class | n/a | `can_locomote` is false (Twin init, [R1]). Locomotion is defined on `LocomoteTwin` (`twin/classes.py:454`), which `GripperJointTwin` does not extend. SDK `client.py:1073-1075`: locomotion commands "raise `NotSimulatedError` in any simulation runtime" |

Consequence for the draft action types in `src/types/draft.ts`
(`set_joint | move_forward | stop`): `set_joint` maps to an SDK call that exists but has
not been tested. `move_forward` does not apply to a stationary arm. `stop` has no direct
SDK call found yet. This is input for open question 1 in docs/schema-draft.md, not a
decision.

## 6. Joint limits (schema limits)

**Source label: Schema ([R1]: printed output of the 2026-11-13 connection test, not stored
in the repo).** Read from the Cyberwave universal schema for our twin. These are the limits the platform's model of the asset declares. They are
static schema values. They are not measurements, and they were not checked by moving the
arm, because nothing has been moved.

| Joint | Type | Lower (rad) | Upper (rad) | Velocity (rad/s) | Effort (N·m) |
|---|---|---|---|---|---|
| `_1` | revolute | -1.91986 | 1.91986 | 10.0 | 10.0 |
| `_2` | revolute | -1.74533 | 1.74533 | 10.0 | 10.0 |
| `_3` | revolute | -1.74533 | 1.5708 | 10.0 | 10.0 |
| `_4` | revolute | -1.65806 | 1.65806 | 10.0 | 10.0 |
| `_5` | revolute | -2.79253 | 2.79253 | 10.0 | 10.0 |
| `_6` | revolute | -0.174533 | 1.74533 | 10.0 | 10.0 |

The same position limits in degrees, for reading only. These are derived by converting
the radian values above and rounding; the radian values are the source of truth.

| Joint | Lower (deg, approx.) | Upper (deg, approx.) |
|---|---|---|
| `_1` | -110 | 110 |
| `_2` | -100 | 100 |
| `_3` | -100 | 90 |
| `_4` | -95 | 95 |
| `_5` | -160 | 160 |
| `_6` | -10 | 100 |

Notes for the safety validator:

- **Position limits** differ per joint, and `_3` and `_6` are not symmetric. They can be
  used as the hard position envelope: reject any target outside `[lower, upper]` for that
  joint name.
- **Velocity and effort** are 10.0 for every joint. Identical values across all six
  joints may be a default in the schema rather than a limit set per joint. That is an
  observation, not a confirmed fact. Treat them as the schema's upper bounds, and do not
  assume they are safe operating speeds or torques.
- The limits are keyed by joint name. Because the physical meaning of the names is
  unknown (section 2), the validator must look limits up by name and must not reason about
  which part of the arm a joint is.
- No acceleration or jerk limits are present.
- The position limits in the first table are copied into `worker/robot_profile.py`, and
  `worker/safety_validator.py` treats both ends as inclusive. A unit test in
  `worker/tests/test_safety_validator.py` fails if that file and this table disagree, so
  change them together.
- The validator checks position limits only. It is not a physical safety envelope: see
  "Safety validator" in `worker/README.md` for what it does not check.

Other possible sources of limits, not queried:

1. Joint calibration data (`twin.joints.calibration.get()`), which has `range_min` per
   joint in the SDK docstring examples (`twin.py:1701`, `:1716`, `:1729`).
2. The SO-101 manufacturer documentation.

## 7. What remains unverified

Everything here carries the **Unverified** label. None of it should be relied on by the
planner, the validator or the critic until it is confirmed.

Behaviour, never tested:

- **Robot movement.** No joint command has been sent, so whether `set_joints` moves the
  twin, and how, is unknown.
- **Gripper operation.** `grip()` and `release()` have never been called.
- Whether a joint command sent in simulation mode has any effect with no simulation
  running.
- Whether `set_joints` requires a controller policy to be attached to the twin. The SDK has
  code that auto-attaches one before outbound commands, skipped when
  `CYBERWAVE_SDK_AUTO_ATTACH_CONTROLLER=0` (`twin/_helpers.py:105-106`, `twin/base.py:269`).
  Effect in simulation: TBD.
- Why no joint update arrived in the 2026-11-13 test (section 3), and whether a running
  simulation is needed for this twin to publish one.
- What the platform does to prevent collisions or out-of-range commands (the API reports
  `navigation_obstacle_avoidance: false`, which says nothing about the arm).
- The unit of the schema limits in section 6 (assumed radians, rad/s and N·m).
- The history of the twin before the worker's scripts: nothing in this repo shows that no
  command was ever sent to it by other means.
- Whether the platform enforces the schema limits itself, or accepts out-of-range targets.
- How to stop a motion in progress. No stop command has been found for arm twins.

State, never received:

- Current joint positions (section 3). Unavailable until a simulation publishes telemetry.
- Joint velocity and effort telemetry, and their units.

Meaning and limits, not provided by any source so far:

- Physical meaning of joints `_1` to `_6`, including which one, if any, is the gripper.
- Whether the schema's velocity and effort values (10.0 for every joint) are real per-joint
  limits or defaults.
- Acceleration limits. None are in the schema.
- Workspace reach. The API reports `reach_m: null`, so no figure is available.
- Self-collision and table-collision behaviour.
- Valid range of the gripper's `max_effort_nm` for the SO-101.

API-reported but not physically verified (section 1.3):

- Every capability field. They are declarations, and none has been exercised. That
  includes `can_actuate`, `can_grip` and `payload_capacity_kg: 0.4`.
- What `maximum_frequency_hz: 0` and `minimum_frequency_hz: 0` mean.
- What `power_capacity_wh: -1` means.

Simulation:

- Which backend to use. The asset supports `playground` and `mujoco` (Catalog, section
  1.1). The worker's scripts started neither ([S]). MuJoCo is billed: the SDK's own
  constants are 0.6 credits per hour and 0.01 per minute (`managers/simulations.py:30-31`),
  and the simulation page describes a MuJoCo simulation as a billable cloud instance of about
  0.6 credits per hour (https://docs.cyberwave.com/overview/features/simulation.md, read 2026-11-14). The same page describes the `playground` runtime as having no
  MuJoCo instance and no cost (https://docs.cyberwave.com/overview/features/simulation.md, read 2026-11-14); whether that holds for this account is unverified.

## 8. Safety notes for anyone writing worker code

These come from reading the SDK source (cited per item) and the documentation pages, and
they are easy to get wrong:

1. **The SDK defaults to live mode.** `Cyberwave()` with no `mode` defaults to `"live"`
   (`client.py:315`); the troubleshooting page says live is the default when `cw.affect(...)` is never called (https://docs.cyberwave.com/support/troubleshooting.md, read 2026-11-14).
   The worker always constructs the client with `mode="simulation"` and refuses to continue
   unless the resulting config says simulation.
2. **`cw.affect("simulation")` starts a billable MuJoCo instance.** So do `"sim"` and
   `"mujoco"` (`client.py:1040-1062`; the simulation page says `cw.affect("simulation")` starts or reuses a MuJoCo
   run (https://docs.cyberwave.com/overview/features/simulation.md, read 2026-11-14)). The worker never calls `affect()`. Passing `mode="simulation"` to the
   constructor selects simulation without starting anything.
3. **Attribute assignment sends commands.** `twin.joints.some_joint = 0.5` calls
   `self.set(value, joint=name)` (`twin/capabilities/joints.py:575-590`), which is a joint
   command. Read with `get_joints()`, and never assign on `twin.joints`.
4. **Missing telemetry looks like zeros.** See section 3 (`joints.py:327-341`).
5. **Instantiate the asset by UUID, not by registry id.** This is what made the first
   connection test fail. In SDK 0.7.5:
   - `cw.twin()` resolves the environment before it looks up the asset
     (`client.py:999`, then `client.py:1018`). With no environment configured, that step
     stores the workspace on the client (`client.py:868-889`), and from then on the SDK
     adds `workspace_uuid` to requests (`client.py:471`, `resources.py:199-221`).
   - `assets.get_by_registry_id()` only tries the direct fetch when the identifier has no
     slash (`resources.py:2578`). A registry id has a slash, so it goes to
     `list_by_registry_id`; that this returned nothing under the workspace filter is
     reported by [R1] (Catalog table in section 1.2), not shown by the source.
   - The steps there are wrapped in `except Exception: pass` (`resources.py:2583-2584`,
     `:2594-2595`), so a failure is swallowed.
   - A UUID has no slash, so it takes the direct fetch (`resources.py:2578`); that this
     works with the workspace filter is reported by [R1] (section 1.2), not shown by the
     source.
6. **"Asset not found" from the SDK is not reliable.** Because errors are swallowed
   (`resources.py:2583-2584`, `:2594-2595`), run `worker/discover_cyberwave_assets.py` to see what each lookup returns.

## 9. How to update this file

From `worker/`:

```
.venv\Scripts\python.exe discover_cyberwave_assets.py     # Windows
.venv/bin/python discover_cyberwave_assets.py              # macOS / Linux
.venv\Scripts\python.exe test_cyberwave_connection.py
.venv/bin/python test_cyberwave_connection.py
```

These run against the network. Save the printed output next to the date you update a section,
or the values stay [R1]-style: reported, not re-checkable from the repo.

The first re-checks the asset identity in section 1. The second prints the twin, joint
names, types and schema limit fields (sections 1, 2 and 6). Both are read-only and start
no simulation.

Section 3 can only be filled in once a simulation is running and the connection test
reports "Joint update received over MQTT". Record positions there only in that case, with
the date, and never copy the zero-fill placeholders.
