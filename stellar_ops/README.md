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

## Backup and disaster recovery

Mission Control can create transactionally consistent SQLite backups only during `CHECKOUT` or `HOLD` and only while recording is stopped. Every backup is immediately checked with SQLite `quick_check`, SHA-256, and a complete audit-ledger verification. The default retention is 20 verified backups and may be configured with `STELLAR_OPS_BACKUP_RETENTION`. A separate storage path may be set with `STELLAR_OPS_BACKUPS`.

Restore is intentionally unavailable from the live web interface. Stop Stellar Ops first, then run the offline command with the exact confirmation string:

```bash
python -m stellar_ops.recovery restore \
  --database stellar_ops/data/control.db \
  --backup stellar-ops-YYYYMMDDTHHMMSS.sqlite3 \
  --confirm "RESTORE stellar-ops-YYYYMMDDTHHMMSS.sqlite3"
```

The restore tool verifies the manifest, checksum, database structure and audit chain before replacement, and preserves the current database as a timestamped rollback copy.

## Operational diagnostics and observability

Every HTTP response carries an `X-Request-ID` correlation identifier and a `Server-Timing` application-duration value. Mission Control includes a controlled self-test panel that checks database integrity, the audit chain, active Test Run, runtime binding, disk capacity, verified recovery backups and schema level.

Self-tests are only permitted during `CHECKOUT` or `HOLD` while recording is stopped, and every result is retained in the operational database and audit event stream. Detailed route, alarm and incident metrics are intentionally not exposed through a public Prometheus endpoint until authentication and monitoring-network controls are implemented in the final security phase.

## Deployment guard

Every deployment is assessed against its declared environment. The checks cover application-secret strength, immutable build identity, HTTPS origin, writable data storage, off-host backup location, debug mode and the production datastore requirement. Development remains usable with explicit warnings; Production fails closed and rejects operational mutations while any blocking check remains.

Set `STELLAR_OPS_MAINTENANCE=1` to place the service in read-only maintenance mode. Safe backup, integrity verification and diagnostic actions remain available. Responses include CSP, frame, MIME-sniffing, referrer and browser-permission security headers; API and health responses are marked `no-store`.

SQLite remains approved only for Development and Training. The deployment guard intentionally prevents Production authorization until PostgreSQL plus dedicated telemetry/object storage are implemented.

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

## Commercial quality gate

Install development dependencies and Chromium once, then run the same gate used by CI:

```bash
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python -m stellar_ops.quality_gate
```

The gate compiles Python, validates browser JavaScript syntax, starts a fresh-database server, runs Chromium journey/accessibility/responsive checks, and then runs the complete unit and integration suite.
