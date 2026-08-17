from __future__ import annotations

import os

from flask import Flask, redirect, url_for

from .control import control, init_control_db

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.environ.get("STELLAR_OPS_SECRET", "development-only-change-me")
app.register_blueprint(control)


@app.get("/")
def home():
    return redirect(url_for("control.console"))


@app.get("/health")
def health():
    init_control_db()
    return {"status": "ok", "service": "smtcs-static-test-control", "database": "ready", "mode": "SIMULATION"}


if __name__ == "__main__":
    init_control_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5001")), debug=os.environ.get("FLASK_DEBUG") == "1")
