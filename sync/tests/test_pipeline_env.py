"""pipeline environment configuration."""

from __future__ import annotations

from bifrost_sync import pipeline


def test_configure_env_hard_pins_load_workers(monkeypatch):
    env = {"LOAD__WORKERS": "8"}
    monkeypatch.setattr(pipeline.os, "environ", env)

    pipeline._configure_env()

    assert env["LOAD__WORKERS"] == "1"
