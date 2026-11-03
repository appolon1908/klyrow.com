#!/usr/bin/env python3
"""Non-root Mautic roles. No installer, migrations, fixture loading, or failed replay."""

import datetime as dt
import http.client
import json
import os
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

APP = Path("/var/www/html")
STATE = Path("/tmp/klyrow-mautic")
ROLES = {"mautic_web", "mautic_cron", "mautic_worker"}
CRON = {0: "mautic:segments:update", 5: "mautic:campaigns:update", 10: "mautic:campaigns:trigger"}


def validate_environment(env, uid):
    role = env.get("DOCKER_MAUTIC_ROLE", "mautic_web")
    if role not in ROLES or uid == 0:
        raise ValueError("invalid runtime identity or role")
    for key in (
        "DOCKER_MAUTIC_RUN_MIGRATIONS",
        "MAUTIC_RUN_MIGRATIONS",
        "DOCKER_MAUTIC_LOAD_TEST_DATA",
        "DEBUG",
    ):
        if env.get(key, "false").lower() != "false":
            raise ValueError("automatic mutation and debug startup are forbidden")
    for key in ("MAUTIC_DB_PASSWORD", "MAUTIC_MAILER_DSN", "MAUTIC_SECRET_KEY"):
        if key in env or not env.get(key + "_FILE", "").startswith("/run/secrets/"):
            raise ValueError("file-based credentials required")
    if role != "mautic_web" and env.get("KLYROW_MAUTIC_JOBS_ENABLED") != "true":
        raise ValueError("background jobs disabled")
    counts = {}
    for queue in ("EMAIL", "HIT"):
        raw = env.get("DOCKER_MAUTIC_WORKERS_CONSUME_" + queue, "2")
        if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= 8:
            raise ValueError("invalid worker count")
        counts[queue.lower()] = int(raw)
    return role, counts


def console(*args):
    return ["php", str(APP / "bin/console"), "--no-interaction", "--no-debug", *args]


def cron_slot(now):
    """One upstream scheduled command per five-minute slot, in UTC."""
    command = CRON.get(now.minute % 15)
    return (int(now.timestamp()) // 60, command) if command else None


def stop_children(children, timeout=25):
    for child in children:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + timeout
    for child in children:
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()


def write_health(role, children):
    target = STATE / "health.json"
    temp = STATE / "health.tmp"
    temp.write_text(
        json.dumps(
            {"role": role, "pid": os.getpid(), "children": [child.pid for child in children]}
        )
    )
    temp.replace(target)


def run_jobs(role, counts):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    previous_handlers = {
        sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGWINCH)
    }
    children = []
    pending = deque()
    last_slot = None
    started = {}
    try:
        if role == "mautic_worker":
            for queue, count in counts.items():
                for _ in range(count):
                    children.append(
                        subprocess.Popen(
                            console("messenger:consume", queue),
                            start_new_session=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    )
        while not stopping:
            if role == "mautic_worker" and any(child.poll() is not None for child in children):
                raise RuntimeError("worker exited; container restart required")
            if role == "mautic_cron":
                for child in children[:]:
                    code = child.poll()
                    if code is not None:
                        children.remove(child)
                        started.pop(child.pid)
                        if code != 0:
                            raise RuntimeError("scheduled command failed")
                    elif time.monotonic() - started[child.pid] > 600:
                        raise RuntimeError("scheduled command exceeded deadline")
                slot = cron_slot(dt.datetime.now(dt.UTC))
                if slot is not None and (last_slot is None or slot[0] > last_slot):
                    last_slot, command = slot
                    pending.append(command)
                    if len(pending) > 12:
                        raise RuntimeError("scheduled command backlog exceeded limit")
                if pending and not children:
                    command = pending.popleft()
                    children.append(
                        subprocess.Popen(
                            console(command),
                            start_new_session=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    )
                    started[children[-1].pid] = time.monotonic()
            write_health(role, children)
            time.sleep(0.25)
        return 0
    finally:
        (STATE / "health.json").unlink(missing_ok=True)
        stop_children(children)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def healthcheck(role):
    if role == "mautic_web":
        connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=5)
        try:
            connection.request("GET", "/s/login")
            if connection.getresponse().status != 200:
                return 1
        finally:
            connection.close()
    else:
        path = STATE / "health.json"
        state = json.loads(path.read_text())
        if state["role"] != role or time.time() - path.stat().st_mtime > 10:
            return 1
        if role == "mautic_worker" and not state["children"]:
            return 1
        for pid in [state["pid"], *state["children"]]:
            os.kill(pid, 0)
    subprocess.run(
        console("doctrine:query:sql", "SELECT 1"),
        check=True,
        timeout=20,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0


def main():
    os.umask(0o077)
    try:
        role, counts = validate_environment(os.environ, os.getuid())
        if sys.argv[1:] == ["healthcheck"]:
            return healthcheck(role)
        if sys.argv[1:]:
            raise ValueError("unsupported runtime command")
        # Bootstrap is intentionally absent. A reviewed, offline installation or
        # migration must already exist before any long-running role is started.
        subprocess.run(
            console("doctrine:query:sql", "SELECT 1"),
            check=True,
            timeout=60,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if role == "mautic_web":
            Path("/tmp/apache2").mkdir(mode=0o700, exist_ok=True)
            os.execvp("apache2-foreground", ["apache2-foreground"])
        return run_jobs(role, counts)
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        http.client.HTTPException,
        subprocess.SubprocessError,
    ):
        print(
            "Klyrow Mautic runtime unavailable; inspect approved private diagnostics.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
