# Cyberwave SDK 0.7.5: source verification

Read-only check of the installed `cyberwave==0.7.5` Python SDK. It covers the six areas
(A–F) we need settled before writing the SO-101 simulation adapter.

All paths below are relative to the SDK package directory:

```
SDK = worker/.venv/Lib/site-packages/cyberwave/
```

`CONTRACTS` means `worker/.venv/Lib/site-packages/cyberwave_contracts/`.

Status labels:

- **VERIFIED**: the behaviour is read directly from the client-side source. "Verified" only
  covers what this Python process does. It never covers what the Cyberwave backend,
  broker, simulator or web frontend does.
- **UNVERIFIED**: depends on server-side behaviour, on account or twin data, or on an
  inference not shown by code. Each one says what evidence would settle it.

---

## 1. Scope and safety constraints

The only method was reading source code. During this investigation:

- No SDK method was called and the `cyberwave` package was never imported. Python was not run.
- No network request, MQTT connection, REST call or Cyberwave login was made.
- No `.env` file was opened or read (`worker/.env` exists and was not touched).
- No worker, simulation or background process was started.
- No twin, environment or simulation run was created.
- No application code or dependency was changed, and no existing branch was modified. This
  document was added on a new branch, `docs/sdk-verification`, created from `main`.

Sources read: the installed SDK `.py` files, the generated REST client
(`SDK/rest/api/default_api.py`, read only for HTTP method and path),
`CONTRACTS/source_type_axes.yml`, and our own `docs/robot-capabilities.md` and
`worker/cyberwave_client.py` (for context only).

Repo references (`docs/`, `worker/`) give line numbers on `main`, which this branch is based
on. Where a file or line range exists only on `feature/b1-reconcile-wip`, the text says so.

Not used as evidence:

- `SDK/twin.py`. It sits next to the `SDK/twin/` package, which has an `__init__.py`. Under
  Python's import rules the package wins, so `cyberwave.twin` loads `twin/` and `twin.py`
  is unreachable at that name. Excluded from every trace.
- `SDK/test/`. It is a driver-testing harness, not unit tests of the client.

## 2. SDK version confirmation

| Item | Value | Evidence |
|---|---|---|
| Installed package | `cyberwave` 0.7.5 | `worker/.venv/Lib/site-packages/cyberwave-0.7.5.dist-info/METADATA` (`Version: 0.7.5`) |
| Related packages | `cyberwave_contracts` 0.1.4, `cyberwave_geometry` 0.2.5 | `*.dist-info` directories in the same `site-packages` |
| Location | `worker/.venv` (the worker's virtualenv) | directory listing |

**VERIFIED**: the source read is the source installed in the worker venv. This does not
show which version is installed anywhere else (CI, a teammate's machine, deploy).

---

## 3. Findings

### A. Runtime mode

**A1. Is the default `live` when `affect()` is never called?** **VERIFIED: yes.**

- `Cyberwave.__init__` has `mode: Optional[str] = "live"` (`SDK/client.py:315`) and
  resolves it with `_resolve_runtime_mode` (`client.py:328`).
- `_resolve_runtime_mode` turns `None` into `"live"` and maps aliases through
  `_RUNTIME_MODE_MAP` (`client.py:225-236`, `:258-264`). An unknown string raises `ValueError`.
- The `CyberwaveConfig` dataclass field is `runtime_mode: str = "live"` (`SDK/config.py:123`),
  and `__post_init__` falls back to `"live"` if it is empty (`config.py:215-216`).
- No environment variable sets the runtime mode. None of the `os.getenv` calls in
  `config.py` (lines 132–219) or `client.py` reads a mode variable.
- The compact module API (`cyberwave.twin(...)`, `cyberwave.configure(...)`) builds its
  global client without passing `mode` (`SDK/compact.py:25-43`), so it is **always live**.
  Even `cyberwave.configure(runtime_mode="simulation")` only changes the global config
  object (`compact.py:96-98`), and `_get_client()` does not pass that field on.

**A2. Is `Cyberwave(mode="simulation")` the same as `affect("simulation")`?**
**VERIFIED: no.** Both set `config.runtime_mode = "simulation"` through the same resolver,
but they differ in four ways:

| # | `Cyberwave(mode="simulation")` | `cw.affect("simulation")` | Evidence |
|---|---|---|---|
| 1 | Starts nothing | MuJoCo profiles (`sim`, `simulation`, `sim_tele`, `mujoco`) call `_autostart_simulation`: `get_active(env)`, and if nothing is active, `simulations.start(env, backend="mujoco")` (a **billable** cloud instance per the docstring), then blocks in `wait_until_active()` | `client.py:245`, `:248-255`, `:1099-1103`, `:1107-1165` |
| 2 | Sets `config.source_type = "sim"` **only if** no `source_type` argument was passed **and** `CYBERWAVE_SOURCE_TYPE` is unset; otherwise keeps that value | Always forces `config.source_type = "sim"` | `client.py:369-370` vs `:1086`, `:1093`; `config.py:194-195` |
| 3 | No MQTT client exists yet | If mode or source changes, disconnects and drops the existing MQTT client (`self._mqtt_client = None`) | `client.py:1088-1097` |
| 4 | — | With no environment ID, logs a warning and starts nothing, but the mode is still changed | `client.py:1128-1137` |

Why difference 2 matters: outbound joint commands take their `source_type` from
`_default_control_source_type` (`SDK/twin/_helpers.py:87-96`). If the config holds
`"tele"` (for example from `CYBERWAVE_SOURCE_TYPE=tele`), commands go out as **`tele`, a
hardware command**, even with `runtime_mode == "simulation"`. Our wrapper already checks
`runtime_mode == "simulation"` and `source_type == "sim"` after construction
(`worker/cyberwave_client.py:63-83` on `main`, `:86-104` on `feature/b1-reconcile-wip`),
which catches this.

Why difference 3 matters: a `twin.joints` handle subscribes once, on whichever MQTT client
exists at that moment, then sets `_joint_listeners_attached = True` and never subscribes
again (`SDK/twin/capabilities/joints.py:310-325`, `SDK/mqtt/state.py:37-60`). If
`affect()` replaces the MQTT client later, the new client has none of those subscriptions.
**VERIFIED code path**. The effect at runtime (the handle stops getting updates) is
**UNVERIFIED**, because nothing was run.

**A3. Can a later SDK call reset the mode to `live`?** **VERIFIED: only an explicit call
does. No SDK method resets it implicitly.**

These are every writer of `config.runtime_mode` in the package (found by searching for
`.runtime_mode =`, `runtime_mode=` and `setattr` on config objects, excluding the
generated `rest/` code and the unreachable `twin.py`):

| Writer | Effect | Evidence |
|---|---|---|
| `Cyberwave.__init__` | sets it once at construction | `client.py:328`, `:351-367` |
| `Cyberwave.affect(mode)` | sets it to the resolved mode (`"live"` for `live`/`real-world`/`real`/`tele`/`teleoperation`) | `client.py:1085-1093`, map at `:225-236` |
| `Cyberwave.configure(**kwargs)` | `setattr(self.config, key, value)` for any existing field, so `configure(runtime_mode=...)` works. **The value is not validated**, and the MQTT client is dropped | `client.py:1204-1213` |
| `cyberwave.configure(**kwargs)` (compact) | same `setattr`, but only on the global config (see A1) | `compact.py:96-98` |
| Direct assignment `client.config.runtime_mode = ...` | plain mutable dataclass field | `config.py:123` |

No code inside the SDK calls `affect(...)`. Every match is in a docstring or an error
message (`exceptions.py:282`, `twin/base.py:900`, `twin/classes.py:619-620`, and others).
`Twin.disconnect()` reads the mode but does not change it (`twin/base.py:247-264`).

Hazard: an unvalidated `configure(runtime_mode="sim")` stores `"sim"`.
`active_runtime_mode` treats anything other than exactly `"simulation"` as **live**
(`SDK/twin/runtime_state.py:51-57`), and so does `_default_control_source_type`
(`_helpers.py:95-96`). The failure mode points toward live.

**A4. How is the state stored and read?** **VERIFIED.**

- Storage: one `CyberwaveConfig` dataclass instance per `Cyberwave` client
  (`client.py:351`). Nothing is shared between clients. The only module-level state is the
  compact API's `_global_config` (`config.py:263-278`).
- Reads: done each time, not cached. `active_runtime_mode(client)` reads
  `client.config.runtime_mode` and turns it into `"live"` or `"simulation"`
  (`runtime_state.py:51-57`). Other call sites read the field with `getattr(..., "live")`,
  e.g. `twin/base.py:254-257`, `managers/policies.py:201-204`, `_helpers.py:95`.
- The MQTT client ID includes a `sim`/`live` token taken from the mode when the MQTT client
  is built (`SDK/mqtt/__init__.py:253-264`, passed in at `SDK/mqtt_client.py:73`).

### B. MQTT communication

**B1. Which `source_type` do joint commands use in simulation? Is it `sim_tele`?**
**VERIFIED (client side): yes, `sim_tele`, given the defaults.**

- `JointsHandle.set` takes `source_type` from `_default_control_source_type(client)` when
  the caller passes none (`joints.py:470-472`).
- `_default_control_source_type` (`_helpers.py:87-96`): config `"sim"` → `"sim_tele"`;
  config `"sim_tele"` or `"tele"` → kept as is; anything else → `"sim_tele"` if
  `runtime_mode == "simulation"`, otherwise `"tele"`.
- On the transport side, `_resolve_outbound_source_type` (`SDK/twin/transport.py:325-331`)
  and `_normalize_locomotion_source_type` (`_helpers.py:61-65`) also turn an explicit
  `"sim"` into `"sim_tele"`.
- Payload (`transport.py:270-323`): an absolute command carrying only positions is sent
  **flat**, `{"source_type": "sim_tele", "<joint>": <value>, ...}` (`:323`). With a
  timestamp, velocities or efforts it is sent aggregated, `{"source_type", "positions",
  "timestamp", ...}` (`:307-321`). Non-absolute modes add `update_mode` (`:289-302`).
  `publish()` also adds `session_id = client_id` (`mqtt/__init__.py:1439`).
- The contracts file defines `sim_tele` as `direction: command`, `substrate: sim`
  (`CONTRACTS/source_type_axes.yml:89-93`).
- A caller can override this. Passing `source_type="edge"` (or `edge_leader`/`edge_follower`)
  skips joint-name validation (`joints.py:476-480`) **and** the controller-policy gate
  (`transport.py:119-121`), and labels the command as hardware state. The adapter must
  never do this.

**B2. Which `source_type` does incoming simulator state carry? Is it `sim`?**
**Client side VERIFIED. Server side UNVERIFIED.**

- What the SDK accepts: the joint listener puts a message in the **simulation** bucket when
  its `source_type` is `sim`, `sim_tele` or `edit` (`runtime_state.py:32-38`, `:60-79`).
  Live values are `tele`, `edge`, `edge_leader`, `edge_follower` (`:41-48`). A missing or
  unknown `source_type` falls back to the client's configured mode (`:77-79`).
- The contracts file defines `sim` as `direction: state`, `substrate: sim`
  (`source_type_axes.yml:77-81`). The SDK's driver-side default for state published against
  a simulator is `"sim"` (`SDK/driver/interface/source_type_policy.py:55-56`).
- **The SDK does not separate state from commands inside the simulation bucket.** A
  `sim_tele` command published by another client or session on the same topic is parsed as
  measured positions. The flat parser treats every numeric key that is not metadata as a
  joint position (`SDK/data/state_representation/space/joint.py:127-136`, `:243-251`,
  metadata keys at `:31-59`), and the result is stored as simulation state
  (`joints.py:255-301`). The client's **own** publishes are suppressed only by MQTT v5
  `no_local` (`mqtt/state.py:54`). On MQTT 3.1.1, `no_local` is silently dropped and only
  logged at debug level (`mqtt/__init__.py:782-793`).
- `joints.set` also writes the commanded target into the same local cache
  (`joints.py:497-502`). If no update arrives within 3 s, `get()` fills in zeros
  (`joints.py:327-341`).
- **UNVERIFIED:** that the Cyberwave cloud simulator (MuJoCo or playground) actually
  publishes joint state with `source_type: "sim"`. This is server behaviour. To confirm it,
  capture real messages on the topic with a read-only subscriber while a simulation runs,
  or get it in writing from Cyberwave.

**B3. Which MQTT topics?** **VERIFIED.**

| Direction | Topic | Evidence |
|---|---|---|
| Outbound joint command | `{topic_prefix}cyberwave/joint/{twin_uuid}/update` | slug `SDK/manifest/driver_config.py:21`; channel map `:75-79`; built at `transport.py:83-85` |
| Inbound joint state | the same topic, `{topic_prefix}cyberwave/joint/{twin_uuid}/update` | `joints.py:303-308` |
| (other) twin catalog commands | `{topic_prefix}cyberwave/twin/{twin_uuid}/command` | `driver_config.py:23` |

- `topic_prefix` comes from `CYBERWAVE_MQTT_TOPIC_PREFIX`, then `CYBERWAVE_TOPIC_PREFIX`,
  then `CYBERWAVE_ENVIRONMENT` (unless it is `production`), otherwise empty
  (`config.py:197-210`).
- If the twin's MQTT catalog lists topics but not the canonical joint slug, the outbound
  call raises `ValueError` (`driver_config.py:280-295`).

**B4. Publish and receive code paths.** **VERIFIED.**

Publishing (`twin.set_joints(...)`):

1. `JointsCapableMixin.set_joints` → `self.joints.set(...)` (`SDK/twin/mixins.py:133-161`)
2. `JointsHandle.set`: argument normalization, `what_joints` filter, degrees→radians,
   source type, name validation (`joints.py:391-484`)
3. `Twin._resolve_topic_and_payload(command="joint_update", channel="joint_update")`: reads
   the twin's MQTT catalog (`driver.get_mqtt_schema()`, `SDK/twin/driver.py:64-66`), picks
   the topic, builds the payload (`transport.py:67-115`)
4. `Twin._publish_resolved` → `_admit_resolved` → `motion_outbound_requires_policy("joint_update")`
   is `True` (`_helpers.py:80-84`) → `_prepare_outbound_command()` →
   `TwinPolicyHandle.ensure_attached()` (`transport.py:117-127`, `mixins.py:24-26`,
   `managers/policies.py:157-207`). See section C.
5. `_ensure_mqtt_connected()` connects if needed (`transport.py:61-65`), then
   `CyberwaveMQTTClient.publish` (`mqtt/__init__.py:1413-1478`). Its `True` only means
   **the local client accepted the message**. It is not a broker acknowledgement and not
   proof of execution (docstring `:1421-1423`). A `False` raises `CyberwaveMQTTError`
   (`transport.py:135-146`).
6. The local cache is updated with the commanded values (`joints.py:497-502`).

Receiving:

1. Just reading `twin.joints` builds the `JointsHandle` (`mixins.py:86-91`), and its
   constructor calls `_start_listening()` (`joints.py:200`, `:310-325`).
2. `attach_topic_listener` → `ensure_mqtt_connected` (**opens the MQTT connection**) →
   `subscribe(topic, no_local=True)` (`mqtt/state.py:27-60`).
3. `_on_joint_payload`: picks the bucket from `source_type`, parses
   (`joint.py:193-251`), drops names not in `joints.list()` (all names are kept when the
   schema is empty), merges into the per-mode cache, and notifies subscribers
   (`joints.py:255-301`).

### C. Controller auto-attach

**C1. Where does it happen on the first joint command?** **VERIFIED.**

- Active path: `TwinPolicyHandle.ensure_attached()` (`managers/policies.py:157-207`). It is
  reached through `_publish_resolved` → `_admit_resolved` (`transport.py:117-127`) for every
  motion command whose `source_type` is not edge state (`constants.py:28-34`).
- It runs once per `Twin` Python object. `_controller_ensured` starts `False`
  (`twin/base.py:92`) and is set `True` at the end (`policies.py:207`). Later calls only
  check that a teleop policy is still attached (`policies.py:159-164`). The flag is not
  persisted, so each new process or `Twin` object runs the check again.
- `Twin._ensure_controller_ready` (`twin/base.py:266-297`) is a near-duplicate with **no
  callers** in the package (only one comment points to it, `_helpers.py:100`). It is not on
  the active path.

**C2. What exactly does it write or publish?** **VERIFIED (client side).**
When auto-attach is enabled and the twin's **cached** data shows no teleop policy
(`policies.py:166`, `:179`; `attached` reads `self._twin._data` locally, `:95-117`):

| Step | Call | HTTP | Evidence |
|---|---|---|---|
| 1 | Look up workspace (non-fatal) | `GET /api/v1/environments/{id}` via `environments.get` | `twin/base.py:112-120` |
| 2 | List controller policies for this asset and workspace | `GET /api/v1/controller-policies` | `policies.py:40-53`; `rest/api/default_api.py:25789` (serializer) |
| 3 | Choose one: keep the current policy if it is visible and teleop, else a teleop policy with `metadata.input_device` of `sdk` (preferred) or `keyboard`, tie broken by UUID. Raises `CyberwaveError` if there is none | — | `policies.py:232-253`; `_helpers.py:101`, `:110-134` |
| 4 | If the policy foreign key changes: create an operator alert "Assigning controller" (best effort, errors swallowed) | `POST /api/v1/alerts` | `twin/base.py:191-192`; `_helpers.py:196-262`; `client.py:1575-1616`; `alerts.py:437-441` |
| 5 | Write the policy onto the twin: `controller_policy_uuid` plus metadata `{controller_policy_uuid, controller_policy_name, controller_type, control_mode: "joint_control"}` (or metadata only if the foreign key already matches) | `PUT /api/v1/twins/{uuid}` | `twin/base.py:177-221`; `_helpers.py:265-282`; `resources.py:3130-3146`; `default_api.py:155040` (serializer) |
| 6 | Log a warning that the current command "may have been lost", then publish the joint command anyway | — | `policies.py:192-197` |

- Attaching publishes **nothing over MQTT**. MQTT is used only for the joint command that
  follows.
- In live mode it also calls `_check_controller_ready_live()`, which is a stub that always
  returns `True` (`_helpers.py:137-146`).
- **UNVERIFIED:** what the backend does after this `PUT` (for example whether it starts a
  controller session, or whether that matters in simulation).

**C3. How does `CYBERWAVE_SDK_AUTO_ATTACH_CONTROLLER=0` turn it off?** **VERIFIED.**

- `_sdk_auto_attach_controller_enabled()` reads
  `os.environ.get("CYBERWAVE_SDK_AUTO_ATTACH_CONTROLLER", "1")` **on every call** and
  returns `False` when the stripped, lower-cased value is `0`, `false`, `no` or `off`
  (`_helpers.py:104-107`). **Default: enabled.**
- When disabled, `ensure_attached` skips steps 1–5 (`policies.py:169-173`).
- **It does not remove the gate.** After the skip, `ensure_attached` still raises
  `CyberwaveError("Cannot send motion commands without an attached teleop controller policy.")`
  unless the twin's **cached** `_data` already shows a teleop policy (`policies.py:187-190`).
  So with the variable set to `0`, a teleop controller must already be assigned (in the UI
  or by an explicit `twin.policy.assign(...)`), and the twin object must have been fetched
  or refreshed after that. Otherwise `set_joints` raises **before** anything is published.

**C4. Summary.** Condition: motion command, non-edge `source_type`, `_controller_ensured`
is false. Default: auto-attach on. Implementation: `managers/policies.py:157-207`. Disabling
removes the REST writes but keeps the requirement that a teleop policy is attached.

### D. Joint naming

**D1. How does `joints.list()` get names for the SO-101 twin?**
**VERIFIED algorithm. The SO-101 names themselves are UNVERIFIED from the SDK.**

- `list()` → `_names()`. Only a non-empty result is cached (`joints.py:214-229`).
- `controllable_joint_names(twin)` (`joints.py:77-95`) takes `schema["joints"]` entries with
  a `name` and `type` in `{revolute, prismatic, continuous}` (`:37`), **sorted by name**.
- Schema source (`_local_universal_schema`, `joints.py:51-74`): the twin's own
  `universal_schema` if the twin payload has one. Otherwise **REST `assets.get(asset_id)`**
  and the **asset's** schema. Any exception gives `None`, so `list()` returns `[]` with no
  error.
- The SDK ships no SO-101 joint names. `assets_capabilities.json:2-33` holds only
  capability flags for `the-robot-studio/so101`. `"_1"`/`"_2"` appear in the SDK only as
  docstring examples (`mqtt/__init__.py:2384-2410`).
- **UNVERIFIED from the SDK:** that our SO-101 twin's schema has names `_1`…`_6`. That
  claim comes from an earlier live read recorded in `docs/robot-capabilities.md:168`
  (`:238` on `feature/b1-reconcile-wip`). To
  confirm it, re-read `twin.joints.list()` / `twin.get_schema("/joints")` for the exact twin
  UUID the adapter will use.

**D2. Does `set_joints()` accept names like `_1`?** **VERIFIED: yes, if and only if
`joints.list()` contains them.**

- Names are passed through **unchanged**. There is no suffix, prefix, case or alias
  handling anywhere on the path (`joints.py:449-468`, `transport.py:270-323`).
- Validation (`joints.py:474-484`): `ValueError` for names not in `list()`, **but only if**
  `list()` is non-empty **and** the source type is not edge. **If the schema lookup fails
  (`[]`), any name is published with no error (fail open).**
- `what_joints=` silently drops values that are not allowed instead of raising
  (`joints.py:461-463`).
- `degrees=True` converts positions to radians (`joints.py:465-468`). Otherwise values go
  out as given. **UNVERIFIED:** the units and limits the simulator applies (server side).

Edge cases specific to names starting with an underscore:

- `twin.joints._1` (reading an attribute) **raises `AttributeError`**, because `__getattr__`
  rejects every name starting with `_` (`joints.py:560-564`). Use `twin.joints["_1"]` or
  `get(what_joints=["_1"])`.
- `twin.joints._1 = 0.3` (setting an attribute) **does publish**, through `__setattr__` →
  `set()` (`joints.py:575-589`). Reading and writing behave differently, so avoid attribute
  access for joints.
- An integer index uses the sorted `list()` order (`joints.py:539-551`). Use names.

**D3/D4. Verified vs assumed.** Verified: the resolution algorithm, the lack of name
transformation, the validation conditions, and the fail-open case on an empty schema.
Assumed (from our own earlier observation, not the SDK): the SO-101 twin's names are
`_1`…`_6` and all are revolute. Unknown: what each joint physically is (already marked TBD
in `docs/robot-capabilities.md:170-176`; `:240-246` on `feature/b1-reconcile-wip`).

### E. Playground behaviour

**E1. Does `affect("playground")` start anything on the server?**
**VERIFIED: the SDK makes no call that starts anything. Server-side reactions UNVERIFIED.**

- `"playground"` maps to runtime mode `"simulation"` (`client.py:235`), so
  `source_type = "sim"`.
- `_affect_autostart_backend("playground")` returns `None` because playground is not in
  `_AFFECT_MUJOCO_PROFILES` (`client.py:238-255`). The only effects are the local config
  change and dropping the MQTT client (`client.py:1088-1103`). No REST call is made.
- After the call, the config **cannot tell playground from MuJoCo**. Both are just
  `"simulation"`. `Cyberwave(mode="playground")` produces the same config as
  `Cyberwave(mode="simulation")`.
- **UNVERIFIED:** whether the backend or broker reacts on its own to `sim_tele` traffic for
  a twin (for example by starting a playground runtime).

**E2. Who publishes joint state while playground runs?** **UNVERIFIED (server or frontend).**

- Nothing in the client publishes simulated joint state in playground mode. `affect()`
  starts no publisher, and `joints.set` only sends commands and updates the local cache.
- `simulation_support.py:51-53` says in a docstring that `twin.joints` state is something
  "which `playground` and the physics backends all publish". That is a docstring statement
  about remote components, not code.
- `managers/policies.py:119-129` says the **web frontend's**
  `PlaygroundLocomotionCommandDrivers` renders playground commands. That *suggests*
  playground runs in the browser, so state may exist only while a browser tab has the
  environment open. **This is an inference and UNVERIFIED.**
- `SimLevel.PLAYGROUND = 0` means "no running-simulation requirement at all"
  (`twin/simulation_support.py:20`), and `_ensure_simulation_support` returns early for it
  (`twin/base.py:879-880`). A `Simulation` record can report backend `"playground"`
  (`simulation_support.py:30-36`). Whether and when the server creates one is unknown.
- **`joints.set` / `set_joints` have no `@simulation_level` decorator** (no match in
  `twin/capabilities/joints.py` or `twin/mixins.py`). In any simulation mode, joint
  commands are published **whether or not anything is running**, and `get()` returns zeros
  after 3 s.

**E3. Evidence needed.** A read-only MQTT capture of
`cyberwave/joint/{uuid}/update` while (a) a MuJoCo simulation runs, (b) the playground is
open in a browser, (c) nothing runs. Record each message's `source_type` and `session_id`.
Or written confirmation from Cyberwave of which component publishes `sim` state in each case.

### F. Side-effect inventory

Method: searched all SDK callers of `twins.create`, `environments.create`,
`workspaces.create`/`projects.create`, `simulations.start`, `_autostart_simulation`, and
every hand-written `resource_path=` / `POST` outside the generated `rest/` package
(excluding `twin.py`). The generated `cyberwave.rest.DefaultApi` exposes every backend
endpoint directly to anyone who calls it. Those raw calls are not listed one by one.

**How `Cyberwave.twin` picks a path** (**VERIFIED**, `client.py:942-1038`)

- Signature: `def twin(self, asset_key=None, environment_id=None, twin_id=None, **kwargs)`
  (`client.py:942-948`). Positional arguments bind in that order: the **first positional
  argument is always `asset_key`**, the second `environment_id`, the third `twin_id`.
  `client.twin(x)` therefore means `client.twin(asset_key=x)`, whatever `x` is.
- The only branch is `if twin_id:` (`client.py:984`). When it is truthy, the method does
  `twins.get_raw(twin_id)` and returns `create_twin(...)`. It returns at `:986` and never
  reaches the create code.
- When `twin_id` is `None` **or any falsy value such as `""`**, execution continues to the
  asset-key path (`client.py:988-1036`). That path may create an environment and a twin,
  or raises `CyberwaveError` if `asset_key` is also empty (`:989-992`).
- The docstring examples show the same split: `client.twin("unitree/go2")` is "Create by
  registry ID" and `client.twin(twin_id="uuid")` is "Fetch by UUID" (`client.py:978-981`).

**Creates a twin**

| Public entry | Internal chain | Side effect | Status |
|---|---|---|---|
| `Cyberwave.twin(asset_key, environment_id=None, **kw)` with no `twin_id`, **including the positional form `client.twin(x)`** | if no env: `get_or_create_quickstart_environment()` (see below) → `assets.get_by_registry_id` → `twins.list(env)`; returns the **first** twin with the same asset (and the same `name`, only if `name=` is given); otherwise `twins.create(...)` → `POST /api/v1/twins` | Get-or-**create** a twin. With no env it may also create a workspace, project and environment, and it sets `config.environment_id` | VERIFIED. `client.py:942-1038` (`:996-1014`, `:1025-1036`); `resources.py:3110-3116`; `default_api.py:137908` |
| `cyberwave.twin(asset_key, environment=None)` (compact) | `_get_client().twin(...)` (a **live-mode** client, see A1) | as above | VERIFIED. `compact.py:108-127` |
| `client.twins.create(asset_id, environment_id, **kw)` | `api.src_app_api_twins_create_twin` → `POST /api/v1/twins` | creates a twin | VERIFIED. `resources.py:3110-3116` |
| `Twin.add_to_environment(environment_uuid)` | `twins.create` in the target env → **`twins.delete` of the original** → **deletes the source environment if no twins are left** | creates a twin, **deletes a twin, may delete an environment** | VERIFIED. `twin/base.py:983-1010` |
| `Cyberwave.twin(twin_id=<non-empty id>)`, **named argument only** | `twins.get_raw(twin_id)` → `_resolve_twin`: a UUID is fetched with `GET /api/v1/twins/{uuid}`; anything else is looked up with `GET /api/v1/twins/by-slug`, and only if that finds nothing is it tried with `GET /api/v1/twins/{uuid}` → `create_twin`, which picks the twin class from the bundled capabilities JSON or the fetched twin data, with no further REST or MQTT call | read only | VERIFIED. `client.py:984-986`; `resources.py:2950-2999`; `default_api.py:140671` (serializer); `twin/factory.py:116-160`; `twin/base.py:82-100` |
| `client.twins.get_raw(twin_id)` + `cyberwave.twin.create_twin(client, record)` | the same two steps called directly, without `Cyberwave.twin` | read only | VERIFIED. `resources.py:2950-2970`; `twin/factory.py:116-160` |

**Creates an environment (or its containers)**

| Public entry | Internal chain | Side effect | Status |
|---|---|---|---|
| `Cyberwave.get_or_create_quickstart_environment()` (also called by the asset-key path of `twin()` when no environment is set) | finds "Quickstart Workspace" / "Quickstart Project" / quickstart env by name; creates each one missing via `workspaces.create`, `projects.create`, `environments.create`; may set `config.workspace_id` | may create a workspace, a project and an environment | VERIFIED. `client.py:857-940` (creates at `:885`, `:918`, `:936`) |
| `client.environments.create(name, project_id=None)` | with no `project_id`: uses the first project, or **creates "Default Folder"** (`POST` project) → `POST /api/v1/projects/{uuid}/environments` | creates an environment, maybe a project | VERIFIED. `resources.py:781-807`; `default_api.py:40193` |
| `client.workspaces.create(...)`, `client.projects.create(...)` | REST create | creates a workspace or project | VERIFIED (client). `resources.py:625`, `:656` |
| `client.agents.create(prompt, ...)` | `POST /api/v1/agents/environments` with the prompt **and the API key in the request body** (`agents.py:240`) | docstring: "Create an environment from an agent prompt" | Client call VERIFIED (`agents.py:228-253`). **That an environment is created is server side, UNVERIFIED** |

**Creates or starts a simulation run**

| Public entry | Internal chain | Side effect | Status |
|---|---|---|---|
| `Cyberwave.affect("sim" \| "simulation" \| "sim_tele" \| "mujoco", environment_id=None, duration=None)` | `_autostart_simulation(backend="mujoco")` → `environments.simulations.get_active(env)` (reuse) else `simulations.start(env, backend="mujoco", duration)` → `POST /api/v1/environments/{uuid}/simulations` → `wait_until_active()` | starts a **billable** MuJoCo instance (per the docstring and log text) and blocks until it runs | VERIFIED client chain. `client.py:1040-1165`; `managers/simulations.py:409-482` (`:464-465`). Billing is server side, UNVERIFIED |
| `client.environments.simulations.start(env, backend="mujoco", duration=..., execution_profile=...)` | `POST /api/v1/environments/{uuid}/simulations` | starts a simulation (`mujoco`, `mujoco_warp`, `isaac`) | VERIFIED. `managers/simulations.py:409-482` |
| `client.environments.simulations.request_scene_bundle(env)` | `POST /api/v1/environments/{uuid}/simulation/scene-bundles` | starts a background Isaac scene build (not a run) | VERIFIED client call. `managers/simulations.py:528-566` |
| `TwinControlHandle(twin)` methods (e.g. `set_velocity`, `forward`) with a non-live dispatch mode. No `Twin` property in this package exposes the handle; it is built directly | builds a `controller_policy_execute` action with `runtime_kind: "simulation"` and optional `simulation_backend` → `_dispatch` → agents control `dispatch()` → `POST /api/v1/agents/environments/{uuid}/control/actions/dispatch`. The same dispatch is reachable as `client.control` / `client.agents.control` (`client.py:550-551`) | **may** make the backend run something in simulation | Client VERIFIED (`twin_control.py:125-193`, `:250-274`; `agents.py:175-204`). **Whether a run is started is UNVERIFIED** |
| `RLTaskClient(cw).launch_inference(...)` (a separate client exported as `cyberwave.RLTaskClient`, not a `Cyberwave` attribute) | "Dispatch a backend inference workload" | **may** start server-side compute | **UNVERIFIED** (`rl_tasks.py:98-104`, `:892-911`, endpoint not traced) |
| `affect("playground")`, `Cyberwave(mode=...)` | config only | **none on the client side** | VERIFIED. See A2, E1 |

**Confirmed never to start a simulation**: `running_simulation(twin)` (it says "Never
starts a simulation", and the code only calls `get_active`) (`managers/simulations.py:297-325`),
and the camera-streaming simulation branch (`twin/classes.py:154-198`).

**Other REST writes on the joint-command path** (no creation, but they change state):
controller auto-attach `PUT /api/v1/twins/{uuid}` and `POST /api/v1/alerts` (C2), and in
live mode `Twin.disconnect()` un-assigns the controller with a `PUT` (`twin/base.py:233-264`).

---

## 4. Evidence table

| ID | Claim | File | Lines | Status |
|---|---|---|---|---|
| A1 | Constructor default `mode="live"` | `SDK/client.py` | 315, 328 | VERIFIED |
| A1 | Mode alias map and resolver | `SDK/client.py` | 225-236, 258-264 | VERIFIED |
| A1 | Config default and fallback `"live"` | `SDK/config.py` | 123, 215-216 | VERIFIED |
| A1 | Compact client never passes `mode` | `SDK/compact.py` | 25-43, 96-98 | VERIFIED |
| A2 | `affect` sets mode and source, drops MQTT client | `SDK/client.py` | 1085-1097 | VERIFIED |
| A2 | MuJoCo profiles auto-start; playground does not | `SDK/client.py` | 238-255, 1099-1103 | VERIFIED |
| A2 | Auto-start: get_active / start / wait | `SDK/client.py` | 1107-1165 | VERIFIED |
| A2 | Constructor `source_type` respects arg/env | `SDK/client.py`; `SDK/config.py` | 369-370; 194-195 | VERIFIED |
| A2 | Joint listener subscribes once, on the MQTT client of that moment | `SDK/twin/capabilities/joints.py`; `SDK/mqtt/state.py` | 310-325; 37-60 | VERIFIED (runtime effect UNVERIFIED) |
| A3 | `configure(**kwargs)` writes any field without validation | `SDK/client.py` | 1204-1213 | VERIFIED |
| A3 | Mode is `simulation` only for exactly `"simulation"` | `SDK/twin/runtime_state.py` | 51-57 | VERIFIED |
| B1 | Default control source `sim_tele` in simulation | `SDK/twin/_helpers.py` | 61-65, 87-96 | VERIFIED |
| B1 | Joint payload shapes | `SDK/twin/transport.py` | 270-323 | VERIFIED |
| B1 | `session_id` added on publish | `SDK/mqtt/__init__.py` | 1439 | VERIFIED |
| B1 | Edge source skips policy gate | `SDK/twin/transport.py`; `SDK/constants.py` | 117-123; 28-34 | VERIFIED |
| B2 | Inbound bucketing: `sim`, `sim_tele`, `edit` → simulation | `SDK/twin/runtime_state.py` | 32-48, 60-79 | VERIFIED |
| B2 | `sim` = state, `sim_tele` = command (contract) | `CONTRACTS/source_type_axes.yml` | 77-81, 89-93 | VERIFIED (document) |
| B2 | Driver default sim publish source `sim` | `SDK/driver/interface/source_type_policy.py` | 55-56 | VERIFIED |
| B2 | Flat parser treats numeric keys as positions | `SDK/data/state_representation/space/joint.py` | 31-59, 127-136, 193-251 | VERIFIED |
| B2 | `no_local` only on MQTT v5 | `SDK/mqtt/state.py`; `SDK/mqtt/__init__.py` | 54; 782-793 | VERIFIED |
| B2 | Cloud simulator publishes `source_type: "sim"` | — | — | **UNVERIFIED** |
| B3 | Joint topic slug | `SDK/manifest/driver_config.py` | 21, 75-79, 280-295 | VERIFIED |
| B3 | Topic prefix sources | `SDK/config.py` | 197-210 | VERIFIED |
| B4 | `set_joints` → `joints.set` | `SDK/twin/mixins.py` | 133-161 | VERIFIED |
| B4 | Resolve, admit, publish | `SDK/twin/transport.py` | 61-152 | VERIFIED |
| B4 | `publish()` is local acceptance only | `SDK/mqtt/__init__.py` | 1413-1478 | VERIFIED |
| B4 | Accessing `twin.joints` connects and subscribes | `SDK/twin/mixins.py`; `SDK/twin/capabilities/joints.py` | 86-91; 190-200 | VERIFIED |
| C1 | Active gate `ensure_attached` | `SDK/managers/policies.py` | 157-207 | VERIFIED |
| C1 | `_ensure_controller_ready` has no callers | `SDK/twin/base.py` | 266-297 | VERIFIED |
| C2 | List policies, pick candidate | `SDK/managers/policies.py`; `SDK/twin/_helpers.py` | 40-53, 232-253; 101-134 | VERIFIED |
| C2 | Apply: alert + `PUT` twin | `SDK/twin/base.py`; `SDK/twin/_helpers.py` | 177-221; 196-282 | VERIFIED |
| C2 | Live readiness check is a stub returning `True` | `SDK/twin/_helpers.py` | 137-146 | VERIFIED |
| C3 | Env flag parse, default on | `SDK/twin/_helpers.py` | 104-107 | VERIFIED |
| C3 | Gate still raises when disabled with no teleop policy | `SDK/managers/policies.py` | 169-173, 187-190 | VERIFIED |
| D1 | Names from universal schema, filtered and sorted | `SDK/twin/capabilities/joints.py` | 37, 51-95, 214-229 | VERIFIED |
| D1 | SDK ships no SO-101 joint names | `SDK/assets_capabilities.json` | 2-33 | VERIFIED |
| D1 | SO-101 twin names are `_1`…`_6` | `docs/robot-capabilities.md` | 168 on `main` (238 on `feature/b1-reconcile-wip`) | **UNVERIFIED** (earlier live observation, not SDK) |
| D2 | No name transformation; validation conditions | `SDK/twin/capabilities/joints.py` | 449-484 | VERIFIED |
| D2 | `_`-prefixed attribute read fails, attribute write publishes | `SDK/twin/capabilities/joints.py` | 560-589 | VERIFIED |
| E1 | Playground maps to simulation, no auto-start | `SDK/client.py` | 235, 238-255 | VERIFIED |
| E2 | `PLAYGROUND` level has no running-sim requirement | `SDK/twin/simulation_support.py`; `SDK/twin/base.py` | 16-36; 858-880 | VERIFIED |
| E2 | Who publishes playground joint state | `SDK/twin/simulation_support.py` (docstring); `SDK/managers/policies.py` (docstring) | 51-53; 119-129 | **UNVERIFIED** |
| F | `twin()` signature: first positional argument is `asset_key`; only a truthy `twin_id` takes the read-only branch | `SDK/client.py` | 942-948, 978-992 | VERIFIED |
| F | `twin()` asset-key path is get-or-create, with quickstart env | `SDK/client.py` | 857-1038 | VERIFIED |
| F | `twins.get_raw` is read only (UUID GET, else slug GET) | `SDK/resources.py` | 2950-2999 | VERIFIED |
| F | `twins.create`, `environments.create` | `SDK/resources.py` | 781-807, 3110-3116 | VERIFIED |
| F | `add_to_environment` creates and deletes | `SDK/twin/base.py` | 983-1010 | VERIFIED |
| F | `simulations.start` / scene bundle | `SDK/managers/simulations.py` | 409-482, 528-566 | VERIFIED |
| F | `agents.create` env from prompt | `SDK/agents.py` | 228-253 | client VERIFIED, effect UNVERIFIED |
| F | Twin control sim dispatch, RL inference launch | `SDK/twin_control.py`; `SDK/agents.py`; `SDK/rl_tasks.py` | 125-193, 250-274; 175-204; 98-104, 892-911 | **UNVERIFIED** effect |
| F | `running_simulation` never starts a simulation | `SDK/managers/simulations.py` | 297-325 | VERIFIED |
| — | HTTP method/path of generated calls | `SDK/rest/api/default_api.py` | 25789, 40193, 137908, 155040 (serializers) | VERIFIED |

## 5. Unverified assumptions and open questions

1. **Simulator state `source_type`.** Does the cloud simulator publish joint state on
   `cyberwave/joint/{uuid}/update` with `source_type: "sim"`, and does each message include a
   timestamp? *Needed:* a read-only MQTT capture during a running simulation, or Cyberwave
   documentation.
2. **Playground publisher.** Does anything publish joint state in playground mode when no
   browser has the environment open? *Needed:* captures for the three cases in E3.
3. **SO-101 joint names.** Are they still `_1`…`_6` on the exact twin the adapter will use,
   and do they come from the twin or from the asset schema? *Needed:* a fresh
   `joints.list()` / `get_schema("/joints")` read for that twin UUID.
4. **Joint units and limits** the simulator applies (radians assumed; clamping behaviour
   unknown). *Needed:* Cyberwave docs or a controlled test.
5. **Controller attach server effects.** What does the backend do after `PUT /twins/{uuid}`
   with a new `controller_policy_uuid`, and is a teleop policy actually needed for
   simulation joint commands, or only enforced by the SDK? *Needed:* Cyberwave
   confirmation. The client-side gate is verified.
6. **Is a teleop policy already assigned** to our twin (decides whether auto-attach would
   write)? *Needed:* a read of the twin's `controller_policy_uuid` and metadata.
7. **MQTT protocol in use.** `no_local` only works on MQTT v5. *Needed:* confirm the broker
   and client run v5 (the client default is v5 per the `client.py:292-294` docstring;
   `CYBERWAVE_MQTT_PROTOCOL` can override it).
8. **Other publishers on the topic.** Can other sessions (UI, teammates) publish `sim_tele`
   on our twin's topic during a run? If they can, the SDK cache would mix their commands in
   as state.
9. **Billing.** "Billable" and "~credits/hour" come from SDK docstrings and log messages
   (`client.py:1053-1056`, `:1153-1161`), not from verified pricing.
10. **Server effect of `agents.create`, twin-control simulation dispatch and
    `RLTaskClient.launch_inference`.** Not traced past the client call.

## 6. What this means for the adapter

These follow from the verified findings. They are proposals, not changes made here.

1. **Construct, don't `affect()`.** Use `Cyberwave(mode="simulation", source_type="sim", environment_id=...)`.
   Passing `source_type` explicitly removes the dependence on `CYBERWAVE_SOURCE_TYPE`.
   Keep the existing check that `runtime_mode == "simulation"` and `source_type == "sim"`.
   Never call `affect()`, `configure()` or the compact `cyberwave.*` API: `affect` with a
   MuJoCo profile starts a billable run, `configure` can change the mode without validation,
   and the compact client is always live.
2. **Pin the outbound source.** Pass `source_type="sim_tele"` explicitly to `set_joints`,
   and reject any edge or `tele` value in the adapter, so a config change cannot turn a
   command into a hardware command or skip the policy gate.
3. **Open existing twins by twin ID only, never by asset.** Two read-only forms are
   acceptable:
   - `client.twin(twin_id=<twin id>)`, with **`twin_id` always passed by name**, after
     checking that the ID is a non-empty string. An empty `twin_id` falls through to the
     asset-key path.
   - `client.twins.get_raw(<twin id>)` followed by `create_twin(client, record)`, which
     skips `Cyberwave.twin` entirely (what `feature/b1-reconcile-wip` does).

   **Never call `client.twin(<anything>)` positionally.** A single positional argument is
   `asset_key`, so it takes the get-or-create path even when the value is a UUID. Also never
   call `twins.create`, `environments.create`, `get_or_create_quickstart_environment`,
   `add_to_environment`, `simulations.start`, `request_scene_bundle`, `agents.create`,
   `TwinControlHandle`, `client.control` / `client.agents.control.dispatch`, or
   `RLTaskClient`. A static test that bans these names, and bans any `.twin(` call without
   `twin_id=`, would enforce this.

   **Must be reviewed before the adapter is built:** on `main`, `SimulatedRobot.load_twin()`
   calls `client.twin(identifier)` (`worker/cyberwave_client.py:115`). That is the
   positional asset-key form. Its own docstring (`:96-106`) says it is get-or-create and
   expects an **asset** UUID from the `robot_asset_identifier` setting (`:109-113`). So on
   `main` this call can create a twin (and, with no environment set, a quickstart
   environment). It is not enough to rename it to `twin_id=identifier`, because the
   configured value is an asset UUID, not a twin UUID. The fix has to change the input to an
   existing twin's ID as well. `feature/b1-reconcile-wip` already does this: it reads
   `settings.twin_id` and uses `client.twins.get_raw(twin_id)` plus `create_twin`
   (`worker/cyberwave_client.py:152-173`, `:333-346` on that branch).
4. **Decide about the controller policy deliberately.** The default auto-attach does a REST
   `PUT` on the twin and creates an alert, and these writes are outside our audit trail.
   Options: (a) assign a teleop policy once, by hand, then run with
   `CYBERWAVE_SDK_AUTO_ATTACH_CONTROLLER=0`, fetching the twin fresh before commands; or
   (b) allow auto-attach and record it as an audit event. Either way, `set_joints` can raise
   `CyberwaveError` before publishing, and that must count as a failed step, never a success.
5. **Validate joint names in the adapter.** Require `joints.list()` to be non-empty and to
   contain every planned joint **before** publishing, because the SDK skips validation when
   the schema is empty. Use `twin.joints["_1"]` / `set_joints({...})`. Never use attribute
   access or integer indices.
6. **Never treat `get_joints()` as measured state.** Its cache mixes commanded targets,
   3-second zero-fill and any `sim_tele` traffic on the topic. Measured state should come
   from a raw subscription that accepts only `source_type == "sim"` with a fresh timestamp
   (what `worker/sim_telemetry.py` on `feature/b1-reconcile-wip` already requires; that
   file is not on `main`), and only once open question 1 is answered.
7. **A successful publish is not execution.** `publish()` returning `True` means the local
   client queued the message. Completion needs separate evidence (measured `sim` state).
8. **Don't rely on the SDK to check that a simulation is running.** `joints.set` has no
   running-simulation check. The adapter should confirm an active simulation
   (`environments.simulations.get_active(env)`, read only) before sending commands, and fail
   closed if there is none. Playground cannot be confirmed this way until open question 2 is
   answered.
9. **Create `twin.joints` once, after the client is fully configured**, and never change the
   mode afterwards. Otherwise the handle's subscription stays on an MQTT client that has
   been thrown away.
