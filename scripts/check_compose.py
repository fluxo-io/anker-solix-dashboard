#!/usr/bin/env python3
"""Resolve Compose only with synthetic configuration; never open private .env."""

import json
import os
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
        for service in ("api",):
            mount = services[service]["volumes"][0]
            assert mount["type"] == "bind" and mount["bind"]["create_host_path"] is False
        assert not services["web"].get("volumes"), "Web must not mount business data"
        assert not services["collector"].get("volumes"), "Collector only uses the API"
        assert services["api"]["volumes"][0]["target"] == "/data"
        if target == "local":
            assert services["api"]["volumes"][0]["source"] == str(root / "data")

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
    isolated = {key: value for key, value in os.environ.items()
                if key not in {"HOST_DATA_DIR", "DATA_DIR", "COMPOSE_FILE", "COMPOSE_ENV_FILES"}}
    for values, selected in [({"DATA_DIR": "./legacy"}, root / "legacy"),
                             ({"HOST_DATA_DIR": "./canonical"}, root / "canonical"),
                             ({"HOST_DATA_DIR": str(Path(directory) / "absolute")}, Path(directory) / "absolute"),
                             ({"DATA_DIR": "./legacy", "HOST_DATA_DIR": "./canonical"}, root / "canonical")]:
        command = ["docker", "compose", "--env-file", str(envfile), "-f", "compose.yaml", "config", "--format", "json"]
        resolved = json.loads(subprocess.check_output(command, cwd=root, env={**isolated, **values}))
        assert resolved["services"]["api"]["volumes"][0]["source"] == str(selected)
print(
    "Neutrale Basis, lokale Ports, private API, Servernetz "
    "und stabile Datenmounts geprüft."
)
