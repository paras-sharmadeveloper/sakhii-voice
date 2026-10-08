"""deploy/setup.sh and deploy/update.sh against fake git/systemctl/curl/
journalctl/python, so the control flow (rollback, old-unit cleanup, never
overwriting shared/.env) is checked without root or a server."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
OLD, NEW = "a" * 40, "b" * 40

FAKES = {
    "id": '#!/usr/bin/env bash\n[ "$1" = -u ] && echo 0\nexit 0\n',
    "useradd": "#!/usr/bin/env bash\nexit 0\n",
    # install -o/-g need root; drop them and use the real install.
    "install": """#!/usr/bin/env bash
args=(); while [ $# -gt 0 ]; do case "$1" in -o|-g) shift 2;; *) args+=("$1"); shift;; esac; done
/usr/bin/install "${args[@]}"
""",
    # HEAD lives in $FAKE_STATE/head; `pull` moves it to $FAKE_REMOTE_HEAD.
    "git": """#!/usr/bin/env bash
echo "git $*" >> "$FAKE_LOG"
dir=.; [ "$1" = -C ] && { dir="$2"; shift 2; }
case "$1" in
  clone) dest="${@: -1}"; mkdir -p "$dest/.git" "$dest/deploy"
         cp "$SRC/pyproject.toml" "$SRC/.env.example" "$dest/"; cp "$SRC/deploy/sakhii-voice.service" "$dest/deploy/"
         echo "$FAKE_REMOTE_HEAD" > "$FAKE_STATE/head";;
  pull) echo "$FAKE_REMOTE_HEAD" > "$FAKE_STATE/head";;
  rev-parse) cat "$FAKE_STATE/head";;
  reset) echo "$3" > "$FAKE_STATE/head";;
  log) h=$(cat "$FAKE_STATE/head"); [ -n "${@: -1}" ] && [ "${#4}" = 40 ] && h="$4"; echo "${h:0:7} commit";;
esac
""",
    "systemctl": '#!/usr/bin/env bash\necho "systemctl $*" >> "$FAKE_LOG"\nexit 0\n',
    "journalctl": '#!/usr/bin/env bash\necho "journal line from sakhii-voice"\n',
    # Healthy unless the checked-out commit is the bad one.
    "curl": """#!/usr/bin/env bash
[ "$(cat "$FAKE_STATE/head" 2>/dev/null)" = "$FAKE_BAD" ] && exit 22
echo '{"ok":true}'
""",
    # python3.12 -m venv DIR  -> DIR/bin/python (pip/tomllib calls logged)
    "python3.12": """#!/usr/bin/env bash
if [ "$1 $2" = "-m venv" ]; then
  mkdir -p "$3/bin"; cp "$0" "$3/bin/python"; exit 0
fi
echo "python $*" >> "$FAKE_LOG"
[ "$1" = -c ] && echo "fastapi>=0.115"
exit 0
""",
}


@pytest.fixture
def box(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in FAKES.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    root = tmp_path / "opt"
    units = tmp_path / "units"
    units.mkdir()

    def run(script: str, *, remote=NEW, bad="", env_file="KEEP=me\n", existing_app=False):
        if env_file is not None:
            (root / "shared").mkdir(parents=True, exist_ok=True)
            (root / "shared" / ".env").write_text(env_file)
        if existing_app:
            app = root / "app"
            (app / ".git").mkdir(parents=True, exist_ok=True)
            (app / "deploy").mkdir(exist_ok=True)
            shutil.copy(REPO / "pyproject.toml", app)
            shutil.copy(REPO / "deploy" / "sakhii-voice.service", app / "deploy")
            (state / "head").write_text(OLD + "\n")
            venv_bin = root / "venv" / "bin"
            venv_bin.mkdir(parents=True, exist_ok=True)
            shutil.copy(bin_dir / "python3.12", venv_bin / "python")
        (root / "releases").mkdir(parents=True, exist_ok=True)  # old layout leftover
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "SAKHII_ROOT": str(root),
            "PYTHON": str(bin_dir / "python3.12"),
            "UNIT_DIR": str(units),
            "HEALTH_TRIES": "2",
            "SRC": str(REPO),
            "FAKE_LOG": str(tmp_path / "calls.log"),
            "FAKE_STATE": str(state),
            "FAKE_REMOTE_HEAD": remote,
            "FAKE_BAD": bad,
        }
        proc = subprocess.run(["bash", str(REPO / "deploy" / script)], env=env, capture_output=True, text=True)
        log = tmp_path / "calls.log"
        calls = log.read_text().splitlines() if log.exists() else []
        head = (state / "head").read_text().strip() if (state / "head").exists() else None
        return proc, calls, head

    return run, root, units


def test_setup_replaces_old_units_and_keeps_env(box):
    run, root, units = box
    proc, calls, head = run("setup.sh")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any(c.startswith("git clone --branch main https://github.com/") for c in calls)
    for unit in ("sakhii-voice.service", "sakhii-voice@8000.service", "sakhii-voice@8001.service"):
        assert f"systemctl disable --now {unit}" in calls
    assert "systemctl enable --now sakhii-voice" in calls
    assert (units / "sakhii-voice.service").read_text() == (REPO / "deploy/sakhii-voice.service").read_text()
    assert (root / "shared" / ".env").read_text() == "KEEP=me\n"
    assert not (root / "releases").exists()
    assert "sakhii-voice healthy" in proc.stdout
    # Code and deps are ready before the old units are stopped.
    first_stop = next(i for i, c in enumerate(calls) if c.startswith("systemctl disable"))
    assert any("pip install" in c for c in calls[:first_stop])


def test_setup_without_env_file_stops_before_touching_services(box):
    run, root, _ = box
    proc, calls, _ = run("setup.sh", env_file=None)
    assert proc.returncode == 1
    assert (root / "shared" / ".env").read_text() == (REPO / ".env.example").read_text()
    assert not [c for c in calls if c.startswith("systemctl")]
    assert "Fill in the API keys" in proc.stdout


def test_setup_rerun_pulls_instead_of_cloning(box):
    run, _, _ = box
    proc, calls, head = run("setup.sh", existing_app=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not any(c.startswith("git clone") for c in calls)
    assert any(c.endswith("pull --ff-only") for c in calls)
    assert head == NEW


def test_update_healthy(box):
    run, _, _ = box
    proc, calls, head = run("update.sh", existing_app=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert head == NEW
    assert calls.count("systemctl restart sakhii-voice") == 1
    assert "sakhii-voice healthy" in proc.stdout


def test_update_unhealthy_rolls_back_and_shows_logs(box):
    run, _, _ = box
    proc, calls, head = run("update.sh", existing_app=True, bad=NEW)
    assert proc.returncode == 1
    assert head == OLD  # back on the previous commit
    assert f"git reset --hard {OLD}" in calls
    assert calls.count("systemctl restart sakhii-voice") == 2
    assert "rolled back; sakhii-voice healthy on the previous commit" in proc.stdout
    assert "journal line from sakhii-voice" in proc.stdout
