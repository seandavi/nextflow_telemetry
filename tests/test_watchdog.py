"""config/nf_tel_watchdog.sh: the run-directory sweep (#240, run-dir cleanup)."""
import os
import subprocess
import time
from pathlib import Path

WATCHDOG = Path(__file__).resolve().parents[1] / "config" / "nf_tel_watchdog.sh"


def _setup(tmp_path, squeue_out: str, squeue_rc: int = 0):
    home, scratch, daemon, bindir = (tmp_path / d for d in ("home", "scratch", "daemon", "bin"))
    for d in (home, scratch, daemon, bindir):
        d.mkdir()
    (home / ".nf_tel.env").write_text(
        f"export NF_TEL_SCRATCH={scratch} NF_TEL_DAEMON={daemon} NF_TEL_REPO={tmp_path}\n")
    stubs = {"squeue": f"printf '{squeue_out}'; exit {squeue_rc}", "pgrep": "exit 0", "tmux": "exit 0"}
    for name, body in stubs.items():
        (bindir / name).write_text(f"#!/bin/bash\n{body}\n")
        (bindir / name).chmod(0o755)
    old = time.time() - 3 * 3600
    for name in ("111", "222", "333", "444", "555", "work"):
        (scratch / name).mkdir()
    (scratch / "444" / ".keep_failed").touch()
    (scratch / "555" / ".keep_failed").touch()
    os.utime(scratch / "555" / ".keep_failed", (time.time() - 50 * 3600,) * 2)
    for name in ("111", "222", "444", "555", "work"):
        os.utime(scratch / name, (old, old))      # 333 stays fresh (just created)
    env = {"HOME": str(home), "USER": "u", "PATH": f"{bindir}:/usr/bin:/bin"}
    return scratch, daemon, env


def test_sweep_removes_only_dead_aged_unkept_jobid_dirs(tmp_path):
    scratch, daemon, env = _setup(tmp_path, "111\\n")
    subprocess.run(["bash", str(WATCHDOG)], env=env, check=True)
    left = sorted(p.name for p in scratch.iterdir())
    # 111 live, 333 too new, 444 kept (fresh marker), work non-numeric; 222 dead, 555 keep expired
    assert left == ["111", "333", "444", "work"]
    assert "swept run directory" in (daemon / "watchdog.log").read_text()


def test_sweep_does_nothing_when_squeue_fails(tmp_path):
    scratch, daemon, env = _setup(tmp_path, "", squeue_rc=1)
    subprocess.run(["bash", str(WATCHDOG)], env=env, check=True)
    assert sorted(p.name for p in scratch.iterdir()) == ["111", "222", "333", "444", "555", "work"]
    assert "sweep skipped" in (daemon / "watchdog.log").read_text()
