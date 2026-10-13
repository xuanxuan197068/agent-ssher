"""Long-running work, run apart from the connection that started it.

An agent's tool call is synchronous and every client puts a ceiling on how long
one may block. So a long task cannot simply be a long call. Instead `start`
launches the task detached on the server and returns at once, and `wait`
blocks on the server side until the task ends or a short ceiling passes,
whichever comes first. Nothing is guessed: `wait` returns within a second of
the task finishing, and a `wait` that hits its ceiling or loses its connection
costs nothing, because the next one picks up where it left off.

Everything a job has is a file under ~/.ssher/jobs/<id>/ on the server:

    cmd.sh     the script, exactly as submitted
    meta.json  name, command, working directory
    pid        process id of the wrapper, which leads its own process group
    started    epoch seconds when it began
    log        stdout and stderr, interleaved
    exit       exit status, written last and atomically
    finished   epoch seconds when it ended
    killed     present if `kill` stopped it

so a job survives the agent, the connection, and the laptop being switched off.
"""

from __future__ import annotations

import base64
import json
import re
import secrets as pysecrets
import shlex
import time
from datetime import datetime, timezone
from typing import Any

from . import output as outmod
from .ssh import (
    BAD_REQUEST, CONNECT_FAILED, EXEC_FAILED, TIMEOUT,
    SshError, Target, build_script, run_command,
)

JOB_ROOT = "$HOME/.ssher/jobs"
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
DEFAULT_OUTPUT_BYTES = 8192
PRUNE_AFTER_DAYS = 7

JOB_NOT_FOUND = "JOB_NOT_FOUND"


# ------------------------------------------------------------------- helpers


def new_job_id(name: str | None = None) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = pysecrets.token_hex(2)
    label = ""
    if name:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")[:24]
        if safe:
            label = f"-{safe}"
    return f"{stamp}-{suffix}{label}"


def check_job_id(job_id: str) -> str:
    """Refuse anything that could reach outside the job directory."""
    job_id = (job_id or "").strip()
    if not JOB_ID_RE.match(job_id) or ".." in job_id:
        raise SshError(
            f"{job_id!r} is not a job id. Job ids look like 20261013-142530-a1b2-train; "
            f"`job list` shows them.",
            BAD_REQUEST,
        )
    return job_id


def job_dir(job_id: str) -> str:
    return f"{JOB_ROOT}/{check_job_id(job_id)}"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _iso(epoch: str | None) -> str | None:
    if not epoch or not epoch.strip().isdigit():
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat(timespec="seconds")


def _fields(text: str) -> tuple[dict[str, str], str]:
    """Split a status report into its __KEY__value lines and the log after them."""
    head, sep, body = text.partition("__SSHER_LOG__\n")
    fields: dict[str, str] = {}
    for line in head.splitlines():
        match = re.match(r"^__([A-Z_]+)__(.*)$", line)
        if match:
            fields[match.group(1)] = match.group(2).strip()
    return fields, body if sep else ""


def _remote(target: Target, script: str, *, timeout: int, **kwargs: Any) -> dict[str, Any]:
    """Run a job-management script. It is ours, so a failure is ours too.

    Carriage returns are left alone (normalize=False) so that progress-bar
    frames in a log can be collapsed properly later, rather than each one
    turned into a line of its own here.
    """
    result = run_command(target, script, timeout=timeout, normalize=False, **kwargs)
    if result.get("timed_out"):
        raise SshError(
            f"{target.alias} did not answer within {timeout}s while managing a job",
            TIMEOUT,
        )
    return result


# The test for "still running" checks more than that the pid is alive. After a
# reboot the pid may belong to something else entirely, so we also require the
# process's command line to mention this job's directory, which the wrapper
# carries as an argument precisely so it can be recognised.
STATE_FUNCTION = r"""
job_state() {
  if [ -e "$D/killed" ]; then echo killed; return; fi
  if [ -e "$D/exit" ]; then echo exited; return; fi
  P=$(cat "$D/pid" 2>/dev/null)
  if [ -n "$P" ] && ps -p "$P" -o args= 2>/dev/null | grep -qF "$D"; then
    echo running
  else
    echo gone
  fi
}
"""


# ---------------------------------------------------------------------- start


def start(
    target: Target,
    cmd: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    name: str | None = None,
    connect_timeout: int = 20,
    accept_new: bool = False,
) -> dict[str, Any]:
    if not cmd or not cmd.strip():
        raise SshError("nothing to run", BAD_REQUEST)

    job_id = new_job_id(name)
    script = build_script(target, cmd, cwd, env) + "\n"
    meta = json.dumps(
        {"name": name, "cmd": cmd, "cwd": cwd or target.cwd},
        ensure_ascii=True,
    )
    d = job_dir(job_id)

    # The script and its metadata cross as base64, which no shell between here
    # and the file can alter. The wrapper gets the job directory as $0 so that
    # nothing has to be quoted inside the quoted -c string.
    launcher = f"""
set -e
D="{d}"
mkdir -p "$D"
printf %s {shlex.quote(_b64(script))} | base64 -d > "$D/cmd.sh"
printf %s {shlex.quote(_b64(meta))} | base64 -d > "$D/meta.json"
for old in "{JOB_ROOT}"/*/; do
  [ -e "$old/exit" ] || [ -e "$old/killed" ] || continue
  [ -n "$(find "$old" -maxdepth 0 -mtime +{PRUNE_AFTER_DAYS} 2>/dev/null)" ] && rm -rf "$old"
done
if command -v setsid >/dev/null 2>&1; then L=setsid; else L=nohup; fi
$L bash -c 'echo $$ > "$0/pid"; date +%s > "$0/started"; \\
  bash -l "$0/cmd.sh" > "$0/log" 2>&1 < /dev/null; s=$?; \\
  date +%s > "$0/finished"; echo $s > "$0/exit.tmp"; mv "$0/exit.tmp" "$0/exit"' \\
  "$D" > /dev/null 2>&1 < /dev/null &
for _ in $(seq 1 50); do [ -s "$D/pid" ] && break; sleep 0.1; done
echo "__PID__$(cat "$D/pid" 2>/dev/null)"
"""
    result = _remote(
        target, launcher, timeout=60,
        connect_timeout=connect_timeout, accept_new=accept_new,
    )
    fields, _ = _fields(result["stdout"])
    pid = fields.get("PID", "")
    if result["exit_code"] != 0 or not pid.isdigit():
        raise SshError(
            f"could not start the job on {target.alias}: "
            f"{result['stderr'].strip() or result['stdout'].strip() or 'no pid was recorded'}",
            EXEC_FAILED,
        )

    return {
        "ok": True,
        "host": target.alias,
        "job_id": job_id,
        "name": name,
        "pid": int(pid),
        "state": "running",
        "log_path": f"~/.ssher/jobs/{job_id}/log",
        "hint": (
            f"started. Call job wait with job_id {job_id}; it returns as soon as the "
            f"job ends, or after its ceiling with state running, in which case call "
            f"it again with since_offset set to the log_offset it returned."
        ),
    }


# ----------------------------------------------------------------------- wait


def wait(
    target: Target,
    job_id: str,
    *,
    max_wait: int = 50,
    since_offset: int = 0,
    until: str | None = None,
    max_output: int = DEFAULT_OUTPUT_BYTES,
    connect_timeout: int = 20,
    accept_new: bool = False,
    retries: int = 1,
) -> dict[str, Any]:
    """Block on the server until the job ends, `until` matches, or `max_wait` passes.

    max_wait 0 is a plain status check. Output is the log written since
    `since_offset`; when there is more than `max_output` bytes of it, the most
    recent `max_output` bytes are returned and the rest is counted as skipped.
    """
    d = job_dir(job_id)
    max_wait = max(0, int(max_wait))
    since_offset = max(0, int(since_offset))
    max_output = max(256, int(max_output))
    if until is not None:
        try:
            re.compile(until)
        except re.error as exc:
            raise SshError(f"until is not a valid regular expression: {exc}", BAD_REQUEST) from exc

    pattern = shlex.quote(until) if until else "''"
    script = f"""
D="{d}"
[ -d "$D" ] || {{ echo "__MISSING__1"; exit 0; }}
{STATE_FUNCTION}
OFF={since_offset}
PAT={pattern}
MATCH=0
END=$(( $(date +%s) + {max_wait} ))
while :; do
  S=$(job_state)
  [ "$S" = running ] || break
  if [ -n "$PAT" ] && tail -c +$((OFF + 1)) "$D/log" 2>/dev/null | grep -qE -- "$PAT"; then
    MATCH=1; break
  fi
  [ "$(date +%s)" -ge "$END" ] && break
  sleep 1
done
if [ -n "$PAT" ] && [ "$MATCH" = 0 ] && \\
   tail -c +$((OFF + 1)) "$D/log" 2>/dev/null | grep -qE -- "$PAT"; then MATCH=1; fi
SIZE=$(wc -c < "$D/log" 2>/dev/null || echo 0)
SIZE=$(( SIZE + 0 ))
[ "$OFF" -gt "$SIZE" ] && OFF=$SIZE
NEW=$(( SIZE - OFF ))
MAXB={max_output}
echo "__STATE__$(job_state)"
echo "__EXIT__$(cat "$D/exit" 2>/dev/null)"
echo "__STARTED__$(cat "$D/started" 2>/dev/null)"
echo "__FINISHED__$(cat "$D/finished" 2>/dev/null)"
echo "__NOW__$(date +%s)"
echo "__SIZE__$SIZE"
echo "__MATCH__$MATCH"
echo "__META__$(cat "$D/meta.json" 2>/dev/null)"
echo "__SSHER_LOG__"
if [ "$NEW" -gt "$MAXB" ]; then
  echo "__SKIPPED__$(( NEW - MAXB ))" >&2
  tail -c +$(( SIZE - MAXB + 1 )) "$D/log" | head -c "$MAXB"
else
  echo "__SKIPPED__0" >&2
  tail -c +$(( OFF + 1 )) "$D/log" | head -c "$NEW"
fi
"""
    started = time.monotonic()
    attempt = 0
    while True:
        try:
            # Generous headroom over max_wait: the remote loop is what keeps
            # time, the local timeout only catches a server that stopped talking.
            result = _remote(
                target, script, timeout=max_wait + 30,
                connect_timeout=connect_timeout, accept_new=accept_new,
                max_output=max_output * 4 + 4096,
            )
            break
        except SshError as exc:
            # A busy sshd sometimes drops a connection during the handshake.
            # Nothing is lost by asking again: the job's state is on disk.
            if exc.code == CONNECT_FAILED and attempt < retries:
                attempt += 1
                time.sleep(2)
                continue
            raise

    fields, raw_log = _fields(result["stdout"])
    if fields.get("MISSING"):
        raise SshError(
            f"no job {job_id} on {target.alias}. `job list` shows the jobs there; "
            f"finished jobs are removed after {PRUNE_AFTER_DAYS} days.",
            JOB_NOT_FOUND,
        )

    skipped = 0
    match = re.search(r"__SKIPPED__(\d+)", result["stderr"])
    if match:
        skipped = int(match.group(1))

    try:
        meta = json.loads(fields.get("META") or "{}")
    except json.JSONDecodeError:
        meta = {}

    state = fields.get("STATE", "gone")
    exit_raw = fields.get("EXIT", "")
    exit_code = int(exit_raw) if exit_raw.lstrip("-").isdigit() else None
    started_at = fields.get("STARTED")
    finished_at = fields.get("FINISHED")
    now = fields.get("NOW")
    elapsed = None
    if started_at and started_at.isdigit():
        end = finished_at if finished_at and finished_at.isdigit() else now
        if end and end.isdigit():
            elapsed = int(end) - int(started_at)
    size = int(fields["SIZE"]) if fields.get("SIZE", "").isdigit() else 0

    reply: dict[str, Any] = {
        "ok": True,
        "host": target.alias,
        "job_id": job_id,
        "name": meta.get("name"),
        "state": state,
        "exit_code": exit_code,
        "elapsed_seconds": elapsed,
        "started_at": _iso(started_at),
        "finished_at": _iso(finished_at),
        "waited_seconds": round(time.monotonic() - started, 1),
        "output": outmod.render_terminal(outmod.decode(raw_log)),
        "log_offset": size,
        "output_skipped_bytes": skipped,
        "log_path": f"~/.ssher/jobs/{job_id}/log",
    }
    if until is not None:
        reply["matched"] = fields.get("MATCH") == "1"
    if skipped:
        reply["skipped_hint"] = (
            f"{skipped} bytes of new output were skipped and only the most recent "
            f"{max_output} shown. Read the rest with: sed -n '1,200p' {reply['log_path']}"
        )
    if state == "running":
        reply["hint"] = (
            f"still running. Call job wait again with since_offset {size} to get "
            f"only what it prints from here on."
        )
    elif state == "gone":
        reply["hint"] = (
            "the job is not running and never recorded an exit status. It was "
            "killed from outside, or the server restarted. The log shows how far it got."
        )
    return reply


def status(target: Target, job_id: str, **kwargs: Any) -> dict[str, Any]:
    return wait(target, job_id, max_wait=0, **kwargs)


# ----------------------------------------------------------------------- kill


def kill(
    target: Target,
    job_id: str,
    *,
    connect_timeout: int = 20,
    accept_new: bool = False,
) -> dict[str, Any]:
    d = job_dir(job_id)
    script = f"""
D="{d}"
[ -d "$D" ] || {{ echo "__MISSING__1"; exit 0; }}
{STATE_FUNCTION}
S=$(job_state)
if [ "$S" != running ]; then echo "__RESULT__not-running:$S"; exit 0; fi
P=$(cat "$D/pid")
# setsid made the wrapper a group leader, so the whole tree goes with it.
kill -TERM -- "-$P" 2>/dev/null || {{ pkill -TERM -P "$P" 2>/dev/null; kill -TERM "$P" 2>/dev/null; }}
for _ in $(seq 1 25); do [ "$(job_state)" = running ] || break; sleep 0.2; done
HOW=terminated
if [ "$(job_state)" = running ]; then
  kill -KILL -- "-$P" 2>/dev/null || {{ pkill -KILL -P "$P" 2>/dev/null; kill -KILL "$P" 2>/dev/null; }}
  HOW=killed
fi
date +%s > "$D/killed"
[ -e "$D/finished" ] || date +%s > "$D/finished"
echo "__RESULT__$HOW"
"""
    result = _remote(
        target, script, timeout=60, connect_timeout=connect_timeout, accept_new=accept_new
    )
    fields, _ = _fields(result["stdout"])
    if fields.get("MISSING"):
        raise SshError(f"no job {job_id} on {target.alias}", JOB_NOT_FOUND)

    outcome = fields.get("RESULT", "unknown")
    was_running = outcome in ("terminated", "killed")
    reply = {
        "ok": True,
        "host": target.alias,
        "job_id": job_id,
        "was_running": was_running,
        "result": outcome if was_running else "not running",
    }
    if not was_running:
        reply["state"] = outcome.partition(":")[2] or "unknown"
    return reply


# ----------------------------------------------------------------------- list


def listing(
    target: Target,
    *,
    connect_timeout: int = 20,
    accept_new: bool = False,
) -> dict[str, Any]:
    script = f"""
echo "__NOW__$(date +%s)"
[ -d "{JOB_ROOT}" ] || exit 0
{STATE_FUNCTION}
for D in "{JOB_ROOT}"/*/; do
  D=${{D%/}}
  [ -e "$D/meta.json" ] || continue
  printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' \\
    "$(basename "$D")" "$(job_state)" "$(cat "$D/exit" 2>/dev/null)" \\
    "$(cat "$D/started" 2>/dev/null)" "$(cat "$D/finished" 2>/dev/null)" \\
    "$(wc -c < "$D/log" 2>/dev/null || echo 0)" "$(cat "$D/meta.json")"
done
"""
    result = _remote(
        target, script, timeout=60, connect_timeout=connect_timeout, accept_new=accept_new
    )
    now = None
    jobs = []
    for line in result["stdout"].splitlines():
        if line.startswith("__NOW__"):
            now = line[len("__NOW__"):].strip()
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        job_id, state, exit_raw, started_at, finished_at, size, meta_raw = parts
        try:
            meta = json.loads(meta_raw)
        except json.JSONDecodeError:
            meta = {}
        jobs.append({
            "job_id": job_id,
            "name": meta.get("name"),
            "cmd": (meta.get("cmd") or "")[:200],
            "state": state,
            "exit_code": int(exit_raw) if exit_raw.lstrip("-").isdigit() else None,
            "started_at": _iso(started_at),
            "finished_at": _iso(finished_at),
            "_started": int(started_at) if started_at.isdigit() else 0,
            "_finished": int(finished_at) if finished_at.isdigit() else None,
            "log_bytes": int(size) if size.strip().isdigit() else 0,
        })

    for job in jobs:
        end = job.pop("_finished") or (int(now) if now and now.isdigit() else None)
        begun = job["_started"]
        job["elapsed_seconds"] = (end - begun) if (end and begun) else None
    jobs.sort(key=lambda job: job.pop("_started"), reverse=True)
    return {"ok": True, "host": target.alias, "jobs": jobs, "count": len(jobs)}
