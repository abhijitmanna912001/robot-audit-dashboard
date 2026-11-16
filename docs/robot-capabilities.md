# Robot capabilities: SO-101 (Cyberwave digital twin)

**Status: identity, twin and schema verified. Behaviour unverified.** Every value in this
file carries one of four source labels:

| Label | Meaning | How it was obtained |
|---|---|---|
| **Catalog** | Returned by the Cyberwave asset catalog | `worker/discover_cyberwave_assets.py`, run 2026-11-13 |
| **Twin init** | Returned or observed while the twin was successfully initialized. Written **Twin init (API-reported)** for values in the API's twin response, such as the capability fields | `worker/test_cyberwave_connection.py`, run 2026-11-13 |
| **Schema** | Read from the twin's universal schema | same connection test run, `twin.get_schema("/joints")` and `twin.joints.list()` |
| **Unverified** | Not observed against the platform | either only read from the installed SDK 0.7.5 source ("SDK source"), or unknown ("TBD") |

Nothing in this file is guessed. An unknown value stays TBD.

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

- **Robot movement is untested.** No joint command has been sent.
- **Gripper execution is untested.** `can_grip: true` is a capability field in the twin's
  API response. It says the platform classes the asset as having a gripper. It is not
  evidence that `grip()` or `release()` works, and neither has been called.
- **Current joint positions are unavailable.** No telemetry has been received (section 3).
- **Zero-fill values are not measurements.** The 0.0 values `get_joints()` returned are
  SDK placeholders.
- **No simulation was started**, and no live hardware has been used.

Two kinds of joint data appear in this file, and they must not be confused:

- **Schema limits** (section 6): static numbers the Cyberwave universal schema declares
  for the asset. They describe what the model allows, not where the arm is.
- **Measured state** (section 3): joint positions reported by a running simulation or
  robot. None has been received yet.

Simulation mode only. No simulation was started, no live hardware was used, and **no
movement command of any kind has been executed**: no joint, gripper or motion command has
been sent to this twin.

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
| Twin UUID | `100cb801-daf0-46eb-90ca-7103f5fc5b1d` | Twin init |
| Twin class | `GripperJointTwin` | Twin init. See the note below on how the SDK picks it |
| Environment | the existing "Quickstart Environment" was reused, none was created. Its UUID is not recorded here | Twin init |
| Runtime mode of the client | `simulation` | Twin init. No simulation instance was started |

Note on the twin class: `GripperJointTwin` is the Python class the SDK instantiated for
our twin. The SDK chooses it in `create_twin()` from the capability data bundled in its
own package for this registry id, preferring that over the twin's response
(`twin/factory.py:143`). So the class confirms which SDK methods exist on our twin object
(`get_joints`, `set_joints`, `grip`, `release`). It does not confirm that those methods
do anything on the platform.

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
`worker/.venv/Lib/site-packages/cyberwave/`. These are facts about the client code. By the
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
  `mode="live"` (`client.py:315`), and no key scope or hardware restriction was found in
  the client source.
- That simulation mode is a permanent, server-side restriction on live hardware. It is a
  setting of the client (`client.config.runtime_mode`), checked by the worker before it
  continues. Nothing shows that the platform enforces it.
- That `sim.stop()` cancels in-flight SO-101 joint movements. In the source it requests
  the stop of a simulation run and sets the local status to `stopped` without re-reading
  it (`managers/simulations.py:212-217`, `:635-653`).
- That the SDK's joint-state view (`get_joints()`) is measured simulator state. It returns
  zeros when no update arrived, and a joint command writes its target into the same cache
  (`twin/capabilities/joints.py:327-341`, `:497-502`). See section 3.
- That telemetry source metadata proves a real measurement. A message labelled
  `source_type: "sim"` says what the sender declared. It is not evidence that the positions
  were measured.

Lookup behaviour (Catalog):

| Lookup | No workspace filter | With `workspace_uuid` filter |
|---|---|---|
| `assets.list_by_registry_id("the-robot-studio/so101")` | 1 asset | **0 assets** |
| `assets.get("89696429-d5d7-4243-85be-ac0d3c148044")` | 1 asset | 1 asset |

### 1.3 Capability flags

Twenty capability fields from the twin's API response are documented here. The connection
test printed them in its step 4, "Twin capabilities (as reported by the API)". All twenty
are **Twin init (API-reported)**.

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

For reference only: the same twenty fields in the SDK's bundled data
(`assets_capabilities.json`) hold the same values. That agreement was checked after the
API values were recorded. It is not where the values above come from.

Reading the fields:

- `has_legs`, `has_wheels`, `can_locomote` and `can_fly` are all false, `locomotion_mode`
  is `stationary`, and every entry in `locomotion_config` is 0. The platform declares a
  fixed arm with no way to move its base, which is why `move_forward` does not apply
  (section 5).
- `navigation_autonomy_level: none` and `navigation_obstacle_avoidance: false`: the
  platform declares no autonomous navigation and no obstacle avoidance. Nothing on the
  platform side is declared to stop the arm hitting something.
- `maximum_frequency_hz: 0` and `minimum_frequency_hz: 0`: the meaning of zero here is
  not documented in what has been read. It is not taken to be a control rate.
- `power_source: tethered`: the platform declares the arm as tethered.
- `power_capacity_wh: -1`: API returned -1; meaning not confirmed. No meaning is inferred
  here without confirmation from Cyberwave.
- `reach_m: null` is not a measurement of the arm. A null reach means the platform gives
  no reach figure, so workspace reach stays unknown (section 7).
- `sensors: []` means the platform lists no sensors for this twin, so there is no camera
  or other sensor feed to rely on.
- `payload_capacity_kg: 0.4` is a declared figure. No payload has been tested.

## 2. Joints

| Item | Value | Source |
|---|---|---|
| Joint count | 6 | Schema |
| Joint names | `_1`, `_2`, `_3`, `_4`, `_5`, `_6` | Schema |
| Joint types | all six are `revolute` | Schema |
| Physical meaning of each joint | **TBD** | Unverified. Not provided by the schema |

The names are exactly as the schema gives them: an underscore and a digit. **Which
physical joint each one is (base, shoulder, elbow, wrist, gripper) is not known and is not
assumed here.** Nothing in the data retrieved so far maps `_1` to `_6` onto parts of the
arm. Until that mapping is confirmed, worker code must address joints only by these names
and must not attach meanings to them.

The SDK's calibration docstrings use `shoulder_pan` as an example joint name. That is an
example in a docstring. It is not a name on our twin and is not a mapping.

How names are discovered: `twin.joints.list()` returns the joints of type revolute,
prismatic or continuous from the twin's universal schema, **sorted by name**. Match
joints by name, never by index.

## 3. Current joint positions (measured state)

**Unverified. Unavailable.** No joint position has been measured.

What happened in the connection test on 2026-11-13:

| Step | Result | Source |
|---|---|---|
| MQTT connection to the Cyberwave broker | succeeded | Twin init |
| Subscription to the twin's joint update topic | succeeded | Twin init |
| Joint update received within the wait window | **none** | Twin init |
| Values returned by `twin.get_joints()` | 0.0 for all six joints | placeholders, not data |

So MQTT connectivity is confirmed for connecting and subscribing only. The worker
published nothing, and no message was received.

Those zeros are **zero-fill placeholders from the SDK, not measurements**. When no joint
update arrives within the timeout (3 seconds by default), `get_joints()` returns 0.0 for
every joint instead of raising. No simulation was running for the environment, so there
was nothing to publish joint state, and no update was expected.

So the zeros do not mean the arm is at its zero pose, and they must not be used as a
starting position by the planner or the validator.

Getting real positions needs a running simulation for the environment. That has not been
done: starting one is a separate, deliberate step (MuJoCo is billed, see section 7).

## 4. Units

| Quantity | Unit | Source |
|---|---|---|
| Schema position limits (`lower`, `upper`) | radians | Schema. Units as reported with the connection test results; they match the URDF convention for revolute joints |
| Schema velocity limit (`velocity`) | radians per second | Schema. Same note |
| Schema effort limit (`effort`) | newton metres | Schema. Same note |
| Joint position, read (`get_joints`) | radians | Unverified (SDK source, docstring). No telemetry has been received to confirm it |
| Joint position, write (`set_joints`) | radians by default, degrees only if `degrees=True` | Unverified (SDK source). Never called |
| Joint velocity and effort telemetry | **TBD** | Unverified. The SDK exposes `velocity`, `acceleration`, `effort` kinds but states no units for them |
| Gripper `force` | unitless, clamped to 0.0 to 1.0 | Unverified (SDK source). Never called |
| Gripper `max_effort_nm` | newton metres, valid range is driver-specific | Unverified (SDK source). Range for SO-101: **TBD** |
| Cartesian pose (`set_pose`) | metres, and radians or a quaternion (never degrees) | Unverified (SDK source). Never called |

Open decision for the action schema (docs/schema-draft.md, open question 3): the worker
should pick one joint unit and convert at one boundary. Radians match the SDK default.

## 5. Supported actions

What the SDK exposes on the `GripperJointTwin` class. Only the two read-only calls have
been run. **No action that moves the robot or operates the gripper has been executed or
tested.** That these methods exist on the class, and that the twin reports
`can_actuate: true` and `can_grip: true`, shows the actions are offered. It does not show
that they work.

| Action | SDK call | Executed? | Source |
|---|---|---|---|
| List joints | `twin.joints.list()` | yes, read-only | Schema. Returned the six names in section 2 |
| Read joint positions | `twin.get_joints()` | yes, read-only | Twin init. Returned placeholders only, see section 3 |
| Set joint positions | `twin.set_joints({name: value})` | **never** | Unverified (SDK source) |
| Close gripper | `twin.grip(force)` or `twin.gripper.grip(force)` | **never** | Unverified (SDK source) |
| Open gripper | `twin.release()` | **never** | Unverified (SDK source) |
| Move tool to a Cartesian pose | `twin.set_pose(pose)` | **never** | Unverified (SDK source). Needs a driver or planner |
| Named movements | `twin.list_movements()`, `twin.run_movement(...)` | **never** | Unverified (SDK source) |
| Stop | **TBD** | **never** | Unverified. No stop or halt command found for manipulator twins. `emergency_stop` exists only on flying twins |
| `move_forward`, locomotion | **not supported** | n/a | Twin init: `can_locomote` is false. The SDK only defines these on locomotion twins |

Consequence for the draft action types in `src/types/draft.ts`
(`set_joint | move_forward | stop`): `set_joint` maps to an SDK call that exists but has
not been tested. `move_forward` does not apply to a stationary arm. `stop` has no direct
SDK call found yet. This is input for open question 1 in docs/schema-draft.md, not a
decision.

## 6. Joint limits (schema limits)

**Source label: Schema.** Read from the Cyberwave universal schema for our twin by the
connection test on 2026-11-13. These are the limits the platform's model of the asset declares. They are
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

1. Joint calibration data (`twin.joints.calibration.get()`), which has `range_min` and
   `range_max` per joint in the SDK examples.
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
  code that auto-attaches one before outbound commands. Effect in simulation: TBD.
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
  1.1). Neither has been started. MuJoCo is billed by the platform: the SDK's own
  constants are 0.6 credits per hour, 0.01 per minute. Cost and behaviour of `playground`
  for this account: TBD.

## 8. Safety notes for anyone writing worker code

These come from reading the SDK source, and they are easy to get wrong:

1. **The SDK defaults to live mode.** `Cyberwave()` with no `mode` targets the real robot.
   The worker always constructs the client with `mode="simulation"` and refuses to continue
   unless the resulting config says simulation.
2. **`cw.affect("simulation")` starts a billable MuJoCo instance.** So do `"sim"` and
   `"mujoco"`. The worker never calls `affect()`. Passing `mode="simulation"` to the
   constructor selects simulation without starting anything.
3. **Attribute assignment sends commands.** `twin.joints.some_joint = 0.5` publishes a
   joint command. Read with `get_joints()`, and never assign on `twin.joints`.
4. **Missing telemetry looks like zeros.** See section 3.
5. **Instantiate the asset by UUID, not by registry id.** This is what made the first
   connection test fail. In SDK 0.7.5:
   - `cw.twin()` resolves the environment before it looks up the asset
     (`client.py:999`, then `client.py:1018`). With no environment configured, that step
     stores the workspace on the client, and from then on the SDK adds `workspace_uuid`
     to every request.
   - `assets.get_by_registry_id()` only tries the direct fetch when the identifier has no
     slash (`resources.py:2578`). A registry id has a slash, so it goes to
     `list_by_registry_id`, which returns nothing under the workspace filter.
   - Every step there is wrapped in `except Exception: pass`, so the outcome is always
     "Asset not found", whatever really happened.
   - A UUID has no slash, so it takes the direct fetch, which works with the filter.
6. **"Asset not found" from the SDK is not reliable.** Because of the swallowed errors
   above, run `worker/discover_cyberwave_assets.py` to see what each lookup returns.

## 9. How to update this file

From `worker/`:

```
.venv\Scripts\python.exe discover_cyberwave_assets.py
.venv\Scripts\python.exe test_cyberwave_connection.py
```

The first re-checks the asset identity in section 1. The second prints the twin, joint
names, types and schema limit fields (sections 1, 2 and 6). Both are read-only and start
no simulation.

Section 3 can only be filled in once a simulation is running and the connection test
reports "Joint update received over MQTT". Record positions there only in that case, with
the date, and never copy the zero-fill placeholders.
