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

The current release is a safe simulation vertical slice. It is not connected to ignition hardware.

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
