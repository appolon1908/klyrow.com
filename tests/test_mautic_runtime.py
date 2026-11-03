import datetime as dt
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docker/mautic-runtime/runtime.py"
spec = importlib.util.spec_from_file_location("mautic_runtime", SOURCE)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def environment(role="mautic_web"):
    return {
        "DOCKER_MAUTIC_ROLE": role,
        "KLYROW_MAUTIC_JOBS_ENABLED": "true",
        "MAUTIC_DB_PASSWORD_FILE": "/run/secrets/db",
        "MAUTIC_MAILER_DSN_FILE": "/run/secrets/mailer",
        "MAUTIC_SECRET_KEY_FILE": "/run/secrets/key",
    }


@pytest.mark.parametrize("role", sorted(runtime.ROLES))
def test_roles_accept_file_authority_and_bounded_consumers(role):
    actual, counts = runtime.validate_environment(environment(role), 33)
    assert actual == role and counts == {"email": 2, "hit": 2}
    assert "failed" not in counts


@pytest.mark.parametrize(
    "key,value",
    [
        ("DOCKER_MAUTIC_ROLE", "arbitrary-command"),
        ("DOCKER_MAUTIC_RUN_MIGRATIONS", "true"),
        ("MAUTIC_RUN_MIGRATIONS", "true"),
        ("DOCKER_MAUTIC_LOAD_TEST_DATA", "true"),
        ("DEBUG", "true"),
        ("MAUTIC_DB_PASSWORD", "ci-only-sensitive-value"),
        ("MAUTIC_MAILER_DSN", "smtp://ci-only-sensitive-value"),
        ("MAUTIC_SECRET_KEY", "ci-only-sensitive-value"),
        ("MAUTIC_DB_PASSWORD_FILE", ""),
        ("MAUTIC_MAILER_DSN_FILE", "/tmp/unapproved"),
        ("DOCKER_MAUTIC_WORKERS_CONSUME_EMAIL", "0"),
        ("DOCKER_MAUTIC_WORKERS_CONSUME_HIT", "9"),
        ("DOCKER_MAUTIC_WORKERS_CONSUME_EMAIL", "1; echo injected"),
        ("DOCKER_MAUTIC_WORKERS_CONSUME_EMAIL", "١"),
    ],
)
def test_unsafe_startup_is_rejected_without_sensitive_diagnostics(key, value):
    with pytest.raises(ValueError) as error:
        runtime.validate_environment(environment() | {key: value}, 33)
    assert not value or value not in str(error.value)


def test_root_and_disabled_background_roles_are_rejected():
    with pytest.raises(ValueError):
        runtime.validate_environment(environment(), 0)
    for role in ("mautic_cron", "mautic_worker"):
        env = environment(role)
        env.pop("KLYROW_MAUTIC_JOBS_ENABLED")
        with pytest.raises(ValueError):
            runtime.validate_environment(env, 33)


def test_cron_preserves_upstream_utc_order_without_extra_side_effects():
    day = dt.datetime(2026, 9, 7, tzinfo=dt.UTC)
    found = [(minute, runtime.cron_slot(day.replace(minute=minute))) for minute in range(60)]
    actual = [(minute, value[1]) for minute, value in found if value]
    expected = [
        (minute, command)
        for minute, command in (
            (0, "mautic:segments:update"),
            (5, "mautic:campaigns:update"),
            (10, "mautic:campaigns:trigger"),
        )
    ]
    assert actual == [
        (minute + offset, command) for offset in (0, 15, 30, 45) for minute, command in expected
    ]


def test_stop_terminates_real_child_process_group_and_reaps_it():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    runtime.stop_children([child], timeout=2)
    assert child.returncode == -signal.SIGTERM


def test_long_cron_command_does_not_drop_the_next_due_command(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "STATE", tmp_path)
    calls = []
    children = []
    minute = [0]
    dates = iter([dt.datetime(2026, 9, 7, 0, value, tzinfo=dt.UTC) for value in (0, 5, 6, 6, 0, 5)])

    class Clock:
        @staticmethod
        def now(zone):
            try:
                value = next(dates)
                minute[0] = value.minute
                return value
            except StopIteration:
                raise RuntimeError("end of fixture")

    class Child:
        def __init__(self, command, **kwargs):
            self.pid = 100 + len(children)
            children.append(self)
            calls.append((minute[0], command))

        def poll(self):
            return 0 if self is children[0] and minute[0] >= 6 else None

    monkeypatch.setattr(
        runtime, "dt", type("Dates", (), {"datetime": Clock, "timezone": dt.timezone})
    )
    monkeypatch.setattr(runtime.subprocess, "Popen", Child)
    monkeypatch.setattr(runtime.time, "sleep", lambda seconds: None)
    stopped = []
    monkeypatch.setattr(runtime, "stop_children", lambda items: stopped.extend(items))
    with pytest.raises(RuntimeError, match="end of fixture"):
        runtime.run_jobs("mautic_cron", {})
    assert [(value, command[-1]) for value, command in calls] == [
        (0, "mautic:segments:update"),
        (6, "mautic:campaigns:update"),
    ]
    assert stopped == children[1:]  # only the unfinished queued command needs shutdown


def test_uncooperative_child_is_killed_after_deadline(tmp_path):
    ready = tmp_path / "ready"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)",
        ],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        runtime.stop_children([child], timeout=0.05)
        assert child.returncode == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_health_rejects_stale_heartbeat_and_dead_children(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "STATE", tmp_path)
    path = tmp_path / "health.json"
    path.write_text(
        json.dumps({"role": "mautic_worker", "pid": os.getpid(), "children": [os.getpid()]})
    )
    os.utime(path, (0, 0))
    assert runtime.healthcheck("mautic_worker") == 1
    path.write_text(json.dumps({"role": "mautic_worker", "pid": os.getpid(), "children": []}))
    assert runtime.healthcheck("mautic_worker") == 1


def test_web_health_never_follows_redirects_to_an_external_server(monkeypatch):
    calls = []

    class Connection:
        def __init__(self, host, port, timeout):
            calls.append((host, port))

        def request(self, method, path):
            calls.append((method, path))

        def getresponse(self):
            return type("Redirect", (), {"status": 302})()

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(runtime.http.client, "HTTPConnection", Connection)
    assert runtime.healthcheck("mautic_web") == 1
    assert calls == [("127.0.0.1", 8080), ("GET", "/s/login"), "closed"]


def test_worker_failure_stops_siblings_and_removes_health(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "STATE", tmp_path)
    monkeypatch.setattr(
        runtime, "console", lambda *args: [sys.executable, "-c", "raise SystemExit(2)"]
    )
    with pytest.raises(RuntimeError, match="worker exited"):
        runtime.run_jobs("mautic_worker", {"email": 2})
    assert not (tmp_path / "health.json").exists()


def test_worker_sigterm_shuts_down_and_clears_health(tmp_path):
    # Run the real supervisor in a child process, replacing only the external
    # Mautic command with a synthetic consumer that cannot send anything.
    code = f"""import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location('runtime', {str(SOURCE)!r})
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.STATE=pathlib.Path({str(tmp_path)!r})
m.console=lambda *args: [sys.executable, '-c', 'import time; time.sleep(60)']
sys.exit(m.run_jobs('mautic_worker', {{'email': 1, 'hit': 1}}))
"""
    child = subprocess.Popen([sys.executable, "-c", code])
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "health.json").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        state = json.loads((tmp_path / "health.json").read_text())
        child.terminate()
        assert child.wait(timeout=5) == 0
        assert not (tmp_path / "health.json").exists()
        for pid in state["children"]:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_candidate_has_no_implicit_volume_or_production_cutover():
    dockerfile = (ROOT / "docker/mautic.Dockerfile").read_text()
    final = dockerfile.split("FROM scratch AS mautic-runtime", 1)[1]
    assert "USER 33:33" in final and "\nVOLUME" not in final
    assert "KLYROW_MAUTIC_JOBS_ENABLED=false" in final
    assert "composer update" not in dockerfile and " as 7." not in dockerfile
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "mautic_data:/var/www/html" in compose  # unchanged pending actual migration
    authority = (ROOT / "scripts/verify-release-authority").read_text()
    assert "klyrow-mautic" not in authority  # candidate cannot self-promote
