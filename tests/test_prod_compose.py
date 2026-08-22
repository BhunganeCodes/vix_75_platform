"""Production compose safety tests (pure YAML parsing - no Docker needed).

Spec assertions:

* NO service other than caddy maps a port to the host.
* Caddy exposes exactly 80 + 443.
* Every service declares resource limits and restart: unless-stopped.
* DRY_RUN_MODE stays pinned to "true" for the execution path
  (go-live is a deliberate edit of /opt/vix75/.env, not a compose flip).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.prod.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    return yaml.safe_load(text)


class TestPortExposure:
    def test_only_caddy_maps_host_ports(self, compose: dict) -> None:
        services = compose["services"]
        offenders = {
            name: svc["ports"]
            for name, svc in services.items()
            if "caddy" not in name and svc.get("ports")
        }
        assert offenders == {}, f"internal services must not publish ports: {offenders}"

    def test_caddy_exposes_exactly_80_and_443(self, compose: dict) -> None:
        caddy = compose["services"]["caddy"]
        host_ports = set()
        for entry in caddy.get("ports", []):
            # short syntax "80:80" or long syntax {target, published}
            if isinstance(entry, str):
                published = entry.split(":")[0]
            else:
                published = str(entry.get("published", ""))
            host_ports.add(published)
        assert {"80", "443"} <= host_ports
        assert host_ports == {
            "80",
            "443",
        }, f"caddy must expose only 80/443: {host_ports}"

    def test_no_database_or_admin_ports_anywhere(self, compose: dict) -> None:
        raw = COMPOSE_PATH.read_text(encoding="utf-8")
        for banned in ("5432:", "6379:", "9090:", "3000:", "8000:", "8080:"):
            assert banned not in raw, f"prod overlay must not publish {banned} to the host"


class TestResourceLimitsAndRestarts:
    def test_every_service_has_mem_and_cpu_limits(self, compose: dict) -> None:
        missing = [
            name
            for name, svc in compose["services"].items()
            if "mem_limit" not in svc or "cpus" not in svc
        ]
        assert missing == [], f"services without limits: {missing}"

    def test_every_service_restarts_unless_stopped(self, compose: dict) -> None:
        bad = [
            name
            for name, svc in compose["services"].items()
            if svc.get("restart") != "unless-stopped"
        ]
        assert bad == [], f"services without restart policy: {bad}"


class TestSafetyDefaults:
    def test_dry_run_mode_pinned_true_for_execution_path(self, compose: dict) -> None:
        exec_env = compose["services"]["execution-service"].get("environment", {})
        env_list = (
            exec_env if isinstance(exec_env, list) else [f"{k}={v}" for k, v in exec_env.items()]
        )
        dry = [e for e in env_list if str(e).startswith("DRY_RUN_MODE")]
        assert dry, "execution-service must define DRY_RUN_MODE"
        value = dry[0].split("=", 1)[1].strip().strip('"').lower()
        assert value == "true", "prod overlay ships DRY_RUN_MODE=true; go-live is an .env edit"

    def test_secrets_required_via_interpolation(self, compose: dict) -> None:
        gateway_env = compose["services"]["api-gateway"]["environment"]
        joined = json_safe(gateway_env)
        assert "${JWT_SECRET:?" in joined, "JWT_SECRET must be required at deploy time"

    def test_env_file_points_to_server_secret_store(self, compose: dict) -> None:
        base = compose.get("x-app-base", {})
        env_files = base.get("env_file", [])
        assert "/opt/vix75/.env" in env_files


def json_safe(value: object) -> str:
    import json

    return json.dumps(value)
