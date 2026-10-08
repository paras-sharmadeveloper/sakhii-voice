"""deploy/remote_deploy.sh against fake sudo/systemctl/uv/curl: the sudo
preflight fails early without touching `current`, and a refused restart or an
unhealthy engine rolls back. Needs GNU coreutils (Linux, or `brew install
coreutils` on macOS)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "deploy" / "remote_deploy.sh"
GNUBIN = Path("/opt/homebrew/opt/coreutils/libexec/gnubin")

FAKES = {
    # sudo -n -l <cmd>  -> allowed?   sudo -n <cmd> -> run it (logged)
    # FAKE_LIST_ALLOWED=0 mimics servers where `sudo -l` wants a password even
    # though the NOPASSWD command itself runs.
    "sudo": """#!/usr/bin/env bash
[ "$1" = -n ] && shift
if [ "$1" = -l ]; then
  [ "$FAKE_SUDO_ALLOWED" = 1 ] && [ "$FAKE_LIST_ALLOWED" != 0 ] && exit 0
  echo "sudo: a password is required" >&2; exit 1
fi
[ "$FAKE_SUDO_ALLOWED" = 1 ] || { echo "sudo: a password is required" >&2; exit 1; }
"$@"
""",
    "systemctl": """#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_LOG"
[ "$FAKE_RESTART_OK" = 1 ]
""",
    "uv": """#!/usr/bin/env bash
[ "$1" = venv ] && mkdir -p .venv
exit 0
""",
    "curl": """#!/usr/bin/env bash
[ "$FAKE_HEALTHY" = 1 ]
""",
}


def _gnu_path() -> str | None:
    probe = subprocess.run(["mv", "-T", "--help"], capture_output=True)
    if probe.returncode == 0:
        return ""
    if GNUBIN.is_dir():
        return str(GNUBIN)
    return None


@pytest.fixture
def server(tmp_path):
    gnu = _gnu_path()
    if gnu is None:
        pytest.skip("needs GNU coreutils (mv -T)")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in FAKES.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    root = tmp_path / "opt"
    (root / "releases").mkdir(parents=True)
    (root / "shared").mkdir()
    old = root / "releases" / "20260101000000-old"
    new = root / "releases" / "20260102000000-new"
    for rel in (old, new):
        rel.mkdir()
        (rel / "pyproject.toml").write_text("")
    (root / "current").symlink_to(old)

    def run(*, allowed=True, listable=True, restart_ok=True, healthy=True):
        env = {
            **os.environ,
            "PATH": os.pathsep.join(p for p in (str(bin_dir), gnu, os.environ["PATH"]) if p),
            "SAKHII_ROOT": str(root),
            "UV": str(bin_dir / "uv"),
            "HEALTH_TRIES": "2",
            "FAKE_LOG": str(tmp_path / "calls.log"),
            "FAKE_SUDO_ALLOWED": "1" if allowed else "0",
            "FAKE_LIST_ALLOWED": "1" if listable else "0",
            "FAKE_RESTART_OK": "1" if restart_ok else "0",
            "FAKE_HEALTHY": "1" if healthy else "0",
        }
        proc = subprocess.run(["bash", str(SCRIPT), str(new)], env=env, capture_output=True, text=True)
        log = tmp_path / "calls.log"
        calls = log.read_text().splitlines() if log.exists() else []
        return proc, Path(os.readlink(root / "current")).name, calls

    return run, new


def test_success_switches_current_and_restarts(server):
    run, new = server
    proc, current, calls = run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert current == new.name
    assert calls == ["systemctl restart sakhii-voice"]
    assert (new / ".venv").is_dir()


def test_preflight_fails_early_without_touching_current(server):
    run, new = server
    proc, current, calls = run(allowed=False)
    assert proc.returncode == 1
    assert current == "20260101000000-old"
    assert calls == []  # nothing started or restarted
    assert not (new / ".venv").exists()  # nothing built
    out = proc.stdout
    assert f"sudo bash {new}/deploy/bootstrap.sh --no-webserver" in out
    assert "20260101000000-old is still live" in out
    assert "sudo -n -l reports:" in out


def test_refused_restart_rolls_back(server):
    run, _ = server
    proc, current, calls = run(restart_ok=False)
    assert proc.returncode == 1
    assert current == "20260101000000-old"
    assert "could not restart sakhii-voice, rolling back to 20260101000000-old" in proc.stdout


def test_unhealthy_engine_rolls_back(server):
    run, _ = server
    proc, current, calls = run(healthy=False)
    assert proc.returncode == 1
    assert current == "20260101000000-old"
    assert "unhealthy on 127.0.0.1:8000, rolling back" in proc.stdout
    assert calls == ["systemctl restart sakhii-voice", "systemctl restart sakhii-voice"]


def test_preflight_passes_when_only_sudo_l_wants_a_password(server):
    """What the real server does: `sudo -n -l <cmd>` asks for a password, but
    the NOPASSWD rule lets the command itself run. The `start` probe (no-op on
    a running service) proves the rule, and the deploy goes through."""
    run, new = server
    proc, current, calls = run(listable=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert current == new.name
    assert calls == ["systemctl start sakhii-voice", "systemctl restart sakhii-voice"]
