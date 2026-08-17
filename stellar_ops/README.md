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
