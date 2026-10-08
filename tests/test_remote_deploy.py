"""deploy/remote_deploy.sh against fake sudo/uv/curl.

The unit(s) to restart come from the server's own `sudo -n -l` output, so a
deploy works with either bootstrap's rule without a root step. Needs GNU
coreutils (Linux, or `brew install coreutils` on macOS)."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "deploy" / "remote_deploy.sh"
GNUBIN = Path("/opt/homebrew/opt/coreutils/libexec/gnubin")

# Exactly what the production server printed on 2026-10-08.
SERVER_RULES_TWO_INSTANCE = """Matching Defaults entries for sakhii on server1:
    !visiblepw, always_set_home, env_reset, secure_path=/sbin\\:/bin\\:/usr/sbin\\:/usr/bin

User sakhii may run the following commands on server1:
    (root) NOPASSWD: /bin/systemctl restart sakhii-voice@8000, /bin/systemctl restart sakhii-voice@8001, /bin/systemctl start sakhii-voice@8000, /bin/systemctl start sakhii-voice@8001
"""
RULES_SINGLE = """User sakhii may run the following commands on server1:
    (root) NOPASSWD: /usr/bin/systemctl restart sakhii-voice, /usr/bin/systemctl start sakhii-voice, /bin/systemctl restart sakhii-voice, /bin/systemctl start sakhii-voice
"""

FAKES = {
    # `sudo -n -l` prints $FAKE_RULES; `sudo -n <cmd>` runs only commands the
    # rules list, word for word (like sudo), and logs them.
    "sudo": """#!/usr/bin/env bash
[ "$1" = -n ] && shift
if [ "$1" = -l ]; then
  [ -n "$FAKE_RULES" ] || { echo "sudo: a password is required" >&2; exit 1; }
  printf '%s' "$FAKE_RULES"; exit 0
fi
case "$FAKE_RULES" in *"$*"*) ;; *) echo "sudo: a password is required" >&2; exit 1;; esac
echo "$*" >> "$FAKE_LOG"
[ "$FAKE_RESTART_OK" = 1 ]
""",
    "uv": """#!/usr/bin/env bash
[ "$1" = venv ] && mkdir -p .venv
exit 0
""",
    "curl": """#!/usr/bin/env bash
case " $FAKE_HEALTHY_PORTS " in *" ${@: -1} "*) exit 0;; esac
for p in $FAKE_HEALTHY_PORTS; do case "${@: -1}" in *":$p/"*) exit 0;; esac; done
exit 1
""",
}


def _gnu_path() -> str | None:
    if subprocess.run(["mv", "-T", "--help"], capture_output=True).returncode == 0:
        return ""
    return str(GNUBIN) if GNUBIN.is_dir() else None


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

    def run(rules, *, restart_ok=True, healthy_ports="8000 8001"):
        env = {
            **os.environ,
            "PATH": os.pathsep.join(p for p in (str(bin_dir), gnu, os.environ["PATH"]) if p),
            "SAKHII_ROOT": str(root),
            "UV": str(bin_dir / "uv"),
            "HEALTH_TRIES": "2",
            "REJOIN_SECS": "0",
            "FAKE_LOG": str(tmp_path / "calls.log"),
            "FAKE_RULES": rules,
            "FAKE_RESTART_OK": "1" if restart_ok else "0",
            "FAKE_HEALTHY_PORTS": healthy_ports,
        }
        proc = subprocess.run(["bash", str(SCRIPT), str(new)], env=env, capture_output=True, text=True)
        log = tmp_path / "calls.log"
        calls = log.read_text().splitlines() if log.exists() else []
        return proc, Path(os.readlink(root / "current")).name, calls

    return run, new


def test_deploys_with_the_servers_current_two_instance_rule(server):
    """The rule on the server today: /bin/systemctl, @8000 and @8001."""
    run, new = server
    proc, current, calls = run(SERVER_RULES_TWO_INSTANCE)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert current == new.name
    assert calls == [
        "/bin/systemctl restart sakhii-voice@8000",
        "/bin/systemctl restart sakhii-voice@8001",
    ]
    assert ">> will restart: sakhii-voice@8000 sakhii-voice@8001" in proc.stdout


def test_deploys_with_the_single_unit_rule(server):
    run, new = server
    proc, current, calls = run(RULES_SINGLE)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert current == new.name
    assert calls == ["/bin/systemctl restart sakhii-voice"]


def test_no_rule_fails_early_without_touching_current(server):
    run, new = server
    proc, current, calls = run("")
    assert proc.returncode == 1
    assert current == "20260101000000-old"
    assert calls == []
    assert not (new / ".venv").exists()
    assert f"sudo bash {new}/deploy/bootstrap.sh --no-webserver" in proc.stdout
    assert "20260101000000-old is still live" in proc.stdout


def test_refused_restart_rolls_back(server):
    run, _ = server
    proc, current, _ = run(SERVER_RULES_TWO_INSTANCE, restart_ok=False)
    assert proc.returncode == 1
    assert current == "20260101000000-old"
    assert "could not restart sakhii-voice@8000, rolling back to 20260101000000-old" in proc.stdout


def test_unhealthy_instance_rolls_back(server):
    run, _ = server
    proc, current, calls = run(SERVER_RULES_TWO_INSTANCE, healthy_ports="8000")
    assert proc.returncode == 1
    assert current == "20260101000000-old"
    assert "sakhii-voice@8001 unhealthy on 127.0.0.1:8001, rolling back" in proc.stdout
