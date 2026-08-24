# Stellar Ops V2

Stellar Mission & Test Control System is the active operations service for controlled preparation, mission-control workspaces, telemetry, camera evidence, displays and broadcast coordination.

Version: `2.0.0-alpha.1`  
Branch: `development/stellar-ops-v2`  
Current status: development and training baseline; not yet production-authorized.

## Run

```bash
git fetch origin
git switch development/stellar-ops-v2
python -m pip install -r requirements.txt
STELLAR_OPS_ENV=DEVELOPMENT PORT=5001 python -m stellar_ops.app
```

Open port 5001. Use `/health/live` for liveness and `/health` for readiness, build, database, disk, recording, run and edge status.

## Environment identity

Set one of:

```bash
STELLAR_OPS_ENV=DEVELOPMENT
STELLAR_OPS_ENV=TRAINING
STELLAR_OPS_ENV=PRODUCTION
```

Unknown values fall back to `DEVELOPMENT`. A deployment may also provide `STELLAR_OPS_COMMIT` so the UI and health response identify the exact build.

## Current service areas

- `/ops` — controlled operation lifecycle and preparation records.
- `/workspace` — Mission Control workspace and synchronized operator panels.
- `/control` — System Configuration for devices, channels, cameras and diagnostics.
- `/media` — display layouts, screen routing and broadcast preparation.

Mission Control is the primary execution workspace. System Configuration exposes engineering setup and diagnostics; legacy execution markup remains temporarily preserved but is no longer presented as an operator workspace.

## Execution runtime binding

An approved Operations record is the authority for Mission Control. Issuing an Execution Release now creates and activates a pinned Test Run carrying the registry operation ID, release ID, release SHA-256, and approved procedure revision. Mission Control exposes that context in its synchronized snapshot and health response.

While a context is `RELEASED`, operators cannot create or activate an unrelated run, change away from the released telemetry source, or reset the execution as a simulation. Closing execution from the Operations record closes the pinned context and run. A clearly labeled `DEVELOPMENT` context remains available only for the seeded local workflow before an operation is released.

## Execution command safety

Every Mission Control command now receives a command ID and is written to an append-only command journal with its original state, resulting state, outcome, rejection reason and HTTP status. Repeating the same command ID returns the original result without executing the transition twice.

A server process restart during `COUNTDOWN` or `FIRING` is treated as an interrupted execution. On startup the runtime enters a fail-safe `HOLD`, clears the process-local firing clock and records a critical recovery event. Resuming from that recovery hold returns to `CHECKOUT`; it never resumes firing automatically. The health response exposes boot reconciliation and command-rejection status.

## Alarm and incident governance

Active P1 alarms are promoted into controlled operational incidents tied to the active Test Run. Each incident has a unique code, severity, category, owner, description and an audited lifecycle: `OPEN` → `CONTAINED` → `RESOLVED` → `CLOSED`, with controlled reopening when required.

Terminal countdown is blocked while any P1 alarm remains active. If a new P1 alarm opens during `COUNTDOWN`, Mission Control enters an automatic fail-safe `HOLD` and records the event. Manual incidents and their required action notes are managed from the Mission Control Incident Center.

## Audit integrity

Operational events, Mission Control commands, alarm actions and incident actions are sealed into a SHA-256 hash chain. Every ledger entry includes the previous entry hash, record identity, payload fingerprint, operation, Test Run and UTC timestamp. Reordering, rewriting or deleting a sealed entry breaks verification.

The source event and action tables are protected by SQLite append-only triggers. Mission Control, `/health` and `/api/control/integrity` expose the current verification state and chain head. Readiness degrades when ledger verification fails.

## Telemetry modes

- `SIMULATION` — generated engineering and training signals.
- `LIVE` — quality-aware Ethernet edge telemetry.
- `REPLAY` — controlled historical CSV playback.

LIVE countdown is blocked unless recording is active, required channels are GOOD and the trusted Ethernet session has no sequence gaps. The FIRE command remains simulated and is not connected to ignition hardware.

## Camera and evidence behavior

Camera discovery uses ONVIF and video uses RTSP. Preview should use a substream while native H.264 evidence recording remuxes the main stream without transcoding. Credentials are stored outside the operational database.

Starting a recording opens a run-scoped evidence directory. Stopping exports telemetry, finalizes camera segments, writes a manifest and seals evidence metadata with SHA-256.

## Ethernet development

Gateway:

```bash
python -m stellar_ops.edge_gateway --port 9100
```

Simulator:

```bash
python -m stellar_ops.edge_simulator --host 127.0.0.1 --port 9100
```

A LIVE Ethernet session is trusted only when its `device_id` is registered with the `SMTCS_EDGE_TCP` adapter.

## Data

SQLite with WAL is the development baseline. PostgreSQL and dedicated telemetry/object storage remain required before production authorization. Runtime databases, evidence, recordings, replay data and generated exports are excluded from Git.

## Safety boundary

This software currently supports planning, simulation, monitoring, evidence and state-guarded operator workflows. It is not authorized to command physical ignition hardware. Authentication, individual users and final role enforcement are intentionally scheduled for the final development phase; until then the system operates as an explicitly identified single-operator development environment.
