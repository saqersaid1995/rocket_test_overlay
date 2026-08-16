# Stellar Ops

Independent engineering and mission operations service. It does not import or modify Rocket Overlay Studio.

## Run in GitHub Codespaces

```bash
git fetch origin
git switch agent/stellar-ops-system
pip install -r requirements.txt
PORT=5001 python -m stellar_ops.app
```

Open forwarded port 5001. Health check: `/health`.
