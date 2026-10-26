"""Run pipeline commands in the background, one job at a time, and keep the last job's state on disk."""
import json
import os
import subprocess
import threading
from datetime import datetime, timezone

from django.conf import settings

_lock = threading.Lock()
_job = None
LOG_LINES = 400


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _state_file():
    return settings.JOB_DIR / "job.json"


def _save():
    settings.JOB_DIR.mkdir(parents=True, exist_ok=True)
    _state_file().write_text(json.dumps(_job), encoding="utf-8")


def command(*args):
    return [*settings.PIPELINE_PYTHON, "-m", "redispatch_es", *args]


def run_now(*args, timeout=600):
    """Run one pipeline command and wait for it, returning its exit code and output."""
    done = subprocess.run(
        command(*args), cwd=settings.PROJECT_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=_env(), creationflags=_flags(),
    )
    return done.returncode, done.stdout, done.stderr


def _env():
    return {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}


def _flags():
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def busy():
    with _lock:
        return bool(_job and _job["status"] == "running")


def current():
    """Return the running or last job, or None."""
    with _lock:
        if _job:
            return json.loads(json.dumps(_job))
    path = _state_file()
    if not path.exists():
        return None
    job = json.loads(path.read_text(encoding="utf-8"))
    if job["status"] == "running":
        job["status"] = "stopped"
    return job


def start(kind, title, steps, days):
    """Start a job of (label, arguments) steps, unless another job is running."""
    global _job
    with _lock:
        if _job and _job["status"] == "running":
            return False
        _job = {
            "kind": kind, "title": title, "days": days, "status": "running", "started": _now(), "finished": None,
            "steps": [{"label": label, "state": "waiting"} for label, _ in steps], "progress": {}, "log": [],
        }
        _save()
    threading.Thread(target=_run, args=([args for _, args in steps],), daemon=True).start()
    return True


def _update(**fields):
    with _lock:
        _job.update(fields)
        _save()


def _line(text):
    with _lock:
        if text.startswith("PROGRESS "):
            fields = json.loads(text[9:])
            if "dataset" in fields:
                _job["progress"][fields["dataset"]] = fields
            else:
                _job["progress"]["_final"] = fields
        else:
            _job["log"] = (_job["log"] + [text])[-LOG_LINES:]
        _save()


def _step(index, state):
    with _lock:
        _job["steps"][index]["state"] = state
        _save()


def _run(commands):
    status = "done"
    for index, args in enumerate(commands):
        _step(index, "running")
        _line("> redispatch-es " + " ".join(args))
        try:
            process = subprocess.Popen(
                command(*args), cwd=settings.PROJECT_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=_env(), creationflags=_flags(),
            )
            for text in process.stdout:
                _line(text.rstrip())
            code = process.wait()
        except OSError as error:
            _line(f"Could not start the pipeline: {error}")
            code = 1
        _step(index, "done" if code == 0 else "failed")
        if code != 0:
            status = "failed"
            break
    _update(status=status, finished=_now())
