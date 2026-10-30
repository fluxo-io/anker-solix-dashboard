#!/usr/bin/env python3
"""Resolve Compose only with synthetic configuration; never open private .env."""

import json
import subprocess
import tempfile
from pathlib import Path

from check_webapp import validate

root = Path(__file__).resolve().parents[1]
web = "web"
with tempfile.TemporaryDirectory(prefix="compose-contract-") as directory:
    envfile = Path(directory) / "synthetic.env"
    envfile.write_text(
        "SECRET_KEY=synthetic-secret-aaaaaaaaaaaaaaaaaaaaaaaa\nAPI_TOKEN=synthetic-token-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\nCOLLECTOR_API_TOKEN=synthetic-collector-cccccccccccccccccccccccccccccccc\nINIT_ADMIN_USERNAME=admin\nINIT_ADMIN_PASSWORD=synthetic-password-cccccccccccc\nANKER_USERNAME=synthetic@example.invalid\nANKER_PASSWORD=synthetic-password\nSESSION_HOURS=8\n"
    )
    configs = {}
    for target in ("base", "local", "webserver"):
        command = [
            "docker",
            "compose",
            "--env-file",
            str(envfile),
            "-f",
            "compose.yaml",
        ]
        if target != "base":
            command += ["-f", f"compose.{target}.yaml"]
        config = json.loads(
            subprocess.check_output(command + ["config", "--format", "json"], cwd=root)
        )
        errors = validate(
            json.loads((root / "webapp-profile.json").read_text()), config
        )
        if errors:
            raise SystemExit("; ".join(errors))
        services = config["services"]
        assert "COLLECTOR_API_TOKEN" not in services[web]["environment"]
        assert "API_TOKEN" not in services["collector"]["environment"]
        assert (
            services["api"]["environment"]["API_TOKEN"]
            != services["api"]["environment"]["COLLECTOR_API_TOKEN"]
        )
        assert (
            services["api"]["environment"]["COLLECTOR_API_TOKEN"]
            == services["collector"]["environment"]["COLLECTOR_API_TOKEN"]
        )
        assert bool(services[web].get("ports")) == (target == "local"), (
            target,
            "Webports",
        )
        assert not services["api"].get("ports"), "API must remain private"
        if target == "webserver":
            assert "reverse-proxy" in services[web]["networks"]
            assert all(
                "reverse-proxy" not in service.get("networks", {})
                for name, service in services.items()
                if name != web
            )
        else:
            assert "reverse-proxy" not in config["networks"]
        configs[target] = config
    assert len({config["name"] for config in configs.values()}) == 1
    assert (
        configs["base"]["services"]["api"]["volumes"]
        == configs["webserver"]["services"]["api"]["volumes"]
    )
print(
    "Neutrale Basis, lokale Ports, private API, Servernetz "
    "und stabile Datenmounts geprüft."
)
