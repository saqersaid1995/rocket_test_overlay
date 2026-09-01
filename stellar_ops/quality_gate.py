from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_until_live(url: str, process: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("browser acceptance server exited before becoming live")
        try:
            with urllib.request.urlopen(f"{url}/health/live", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("browser acceptance server did not become live")


def browser_acceptance() -> None:
    with tempfile.TemporaryDirectory(prefix="stellar-ops-acceptance-") as temporary:
        root = Path(temporary)
        port = available_port()
        base_url = f"http://127.0.0.1:{port}"
        server_env = os.environ.copy()
        server_env.update(
            STELLAR_OPS_ENV="DEVELOPMENT",
            STELLAR_OPS_SECRET="quality-gate-only-not-for-deployment",
            STELLAR_OPS_DATA=str(root / "data"),
            PORT=str(port),
        )
        log_path = root / "server.log"
        with log_path.open("w", encoding="utf-8") as log:
            server = subprocess.Popen(
                [sys.executable, "-m", "stellar_ops.app"],
                cwd=ROOT,
                env=server_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                wait_until_live(base_url, server)
                browser_env = os.environ.copy()
                browser_env.update(
                    RUN_BROWSER_E2E="1",
                    STELLAR_OPS_BASE_URL=base_url,
                    BROWSER_ARTIFACT_DIR=str(root / "browser"),
                )
                run(
                    [
                        sys.executable,
                        "-m",
                        "unittest",
                        "stellar_ops.tests.test_browser_smoke",
                        "-v",
                    ],
                    env=browser_env,
                )
            except Exception:
                log.flush()
                print(log_path.read_text(encoding="utf-8"), file=sys.stderr)
                raise
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


def main() -> int:
    run([sys.executable, "-m", "compileall", "-q", "stellar_ops"])
    run(["node", "--check", "stellar_ops/static/control.js"])
    run(["node", "--check", "stellar_ops/static/workspace.js"])
    browser_acceptance()
    run(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "stellar_ops/tests",
            "-p",
            "test_*.py",
            "-v",
        ]
    )
    print("Stellar Ops commercial quality gate passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
