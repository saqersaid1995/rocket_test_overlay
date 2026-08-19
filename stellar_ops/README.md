# Stellar Ops

Independent Static Motor Test Control service. It does not import or modify Rocket Overlay Studio.

## Run in GitHub Codespaces

```bash
git fetch origin
git switch agent/stellar-ops-system
pip install -r requirements.txt
PORT=5001 python -m stellar_ops.app
```

Open forwarded port 5001. Health check: `/health`.

The root route opens the Mission Control Workspace. The original engineering setup console remains available at `/control`.

## Mission Control Workspace

The workspace provides role-based console presets for Test Director, Instrumentation, Propulsion, Data & Video, and Observer. Panels can be reordered, widened, removed, added, saved, shown in kiosk mode, or opened in synchronized pop-out windows for additional monitors. Included panels cover mission state, multi-channel plots, derived propulsion values, procedure position, station poll, alarms, events, camera wall, channel quality, network health, and evidence recording.

The global Time Conductor supports live view, display pause without stopping recording, replay context, configurable visible time windows, and ignition positioning. Camera tiles use virtual optical feeds only in `SIMULATION`; `LIVE` never fabricates a connected stream.

Test Runs and saved workspaces persist in SQLite. Alarm acknowledgements, shelving, run changes, and workspace changes are audited. A live alarm condition cannot be manually closed until its source recovers.

## Run-scoped evidence and reliability

Operational events, alarms, recording sessions, and Ethernet batches are linked to the active Test Run. Starting a recording opens a run-specific evidence directory; stopping it exports the received telemetry batches to canonical JSON Lines, writes a manifest, and seals both with SHA-256 integrity metadata. The Evidence panel shows open and sealed packages.

Database startup applies numbered migrations and configures SQLite WAL, foreign keys, normal synchronous mode, and a 10-second busy timeout for safer concurrent Flask/gateway development. Production deployment should still migrate operational metadata to PostgreSQL and large telemetry/video to dedicated time-series/object storage.

The workspace consumes a server-sent event stream rather than repeatedly fetching a complete snapshot. `/health` reports database latency, disk capacity, active run, recording, operation and edge status; `/health/live` is the lightweight process liveness endpoint.

The control console supports three explicit telemetry source modes:

- `SIMULATION`: generated training signals.
- `LIVE`: quality-aware data from the Ethernet edge gateway.
- `REPLAY`: operator-controlled CSV playback with seek and speed control.

Recording sessions, source changes, alarms and commands are audited in SQLite. `LIVE` countdown is blocked unless recording is active, all required channels are `GOOD`, and the Ethernet session has no sequence gaps. The system remains read-only toward field ignition hardware; the FIRE command is deliberately simulated.

## Ethernet telemetry development

Start the inbound telemetry gateway in a second terminal:

```bash
python -m stellar_ops.edge_gateway --port 9100
```

Run the ESP/DAQ simulator in a third terminal:

```bash
python -m stellar_ops.edge_simulator --host 127.0.0.1 --port 9100
```

Device Setup shows the edge session, boot identity, sequence, total samples and detected gaps. Protocol definition: `docs/smtcs/06_ETHERNET_EDGE_PROTOCOL.md`.

In the console, choose **LIVE ETHERNET**, start recording, and verify each required channel reports `GOOD`. Channel names do not require code changes: map the incoming device field to the canonical channel under **Device Setup → Channel Mapping & Calibration**.

## Device lifecycle

Use **Device Setup** to create or edit devices and channels. The registry separates live health from the last configuration connection test. Assets are archived rather than silently erased so their audit history remains intact. Archive or reassign active channels before archiving their source device; restore the device before restoring its channels. Configuration changes are blocked while recording and outside `CHECKOUT` or `HOLD`.

An Ethernet session is not trusted as a LIVE source until its `device_id` is registered with the `SMTCS_EDGE_TCP` adapter. Unregistered sessions remain visible in the gateway table for diagnosis but do not feed operational telemetry.

## Controlled preparation documents

Each operation includes a Document Export Center at `/ops/<operation-id>/documents`. It creates master-operation, department, or individual work packages from the controlled preparation plan.

Every package contains a paginated PDF, a filterable Excel workbook, and a ZIP bundle with a JSON manifest. The register records revision, state, generator, creation time, file size, document SHA-256 values, and a package manifest fingerprint. Draft copies may expose open work for coordination; released copies are blocked until the operation has a planned start, named assignments, independent safety-critical verification, accepted tasks, and evidence records.

Generated packages are stored under `stellar_ops/data/exports/` and are intentionally excluded from source control. Install `reportlab` and `openpyxl` through `requirements.txt` before generating documents.

## Safety and procedure assurance

Procedure Control now has a dedicated Safety & Procedure Assurance workspace at `/ops/<operation-id>/safety`. The workspace manages operation hazards, preventive and mitigating controls, residual risk, control verification, required equipment, tools, PPE, emergency equipment, controlled documents and formal HOLD points.

Hazards and resources are linked to controlled procedure step codes. A safety-critical step cannot pass procedure approval without a linked hazard, verified controls and independent verification. Formal HOLD points identify the trigger, immediate safe state, call authority, release criteria and release authority. Mandatory resources must be READY and carry a certification, calibration or controlled-reference identity.

The safety case is approved separately and protected by SHA-256. Procedure approval pins that approved safety-case fingerprint so later changes cannot silently alter the execution basis. The `DEMO-SF-001` training operation includes representative ignition, pressure, exclusion-zone and evidence-loss hazards with controls, PPE and HOLD logic.
