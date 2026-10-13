"""The text `ssher agent-guide` prints. Kept in code so it ships with the wheel."""

GUIDE = r"""# ssher — run commands on remote servers

    ssher --compact <host> <command>

Prints one JSON object on stdout. Use this instead of building an `ssh` command
line yourself. Always pass `--compact`.

## 0. If you have ssher's MCP tools, use those instead

If your tool list includes `run`, `job_start` and `job_wait` from an `ssher`
server, call those and skip this page. They take the command as a plain string,
so nothing below about quoting, `--b64` or `--cmd-file` applies to them.

## 1. Pick the input form. Getting this wrong corrupts commands silently.

| Your command | Use |
| --- | --- |
| plain words only: no quotes, no `$`, no backticks, no `(`, no `\` | plain text |
| single line with any of those characters | `--b64` |
| multi-line, or a script | `--cmd-file` |

On Linux and macOS plain text is always safe. **On Windows PowerShell it is safe
ONLY for plain words.** Any of `"` `'` `$` `` ` `` `$(...)` `\` in the command is
eaten by PowerShell before ssher ever sees it, so when in doubt use `--b64` or
`--cmd-file`.

Two ways this bites, both silent:

  * `"echo $HOME"` — PowerShell expands `$HOME` to the local Windows path, then
    the remote shell never sees it. You get the wrong value, no error.
  * `"grep x $(cat f)"` — PowerShell runs `$(cat f)` LOCALLY, substitutes the
    result (often empty, or an error), and only then calls ssher. Your command
    reaches the server with that part blanked out. It can even return
    `ok: true` with a plausible-looking but wrong result.

Backslash does NOT escape in PowerShell — its escape character is the backtick.
So `\$(...)` does not protect the `$(...)`; PowerShell still runs it locally.
Do not try to escape. Use `--b64` or `--cmd-file` and stop thinking about it.

**Plain text**

    ssher --compact gpu1 uptime
    ssher --compact gpu1 'df -h /data'

**Base64.** Let the shell encode it; do not compute base64 yourself.

    # PowerShell: the here-string needs no escaping at all
    ssher --compact gpu1 --b64 ([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(@'
    echo "it's $HOME"
    '@)))

    # bash
    ssher --compact gpu1 --b64 "$(base64 -w0 <<'EOF'
    echo "it's $HOME"
    EOF
    )"

**Command file.** Write the script with your file-writing tool, then point at
it. Nothing passes through a shell and there is no length limit. This is never
wrong, so prefer it whenever you are unsure.

    ssher --compact gpu1 --cmd-file ./deploy.sh

## 2. Read the reply in two steps

    {"ok": true, "host": "gpu1", "exit_code": 0, "stdout": "...", "stderr": "",
     "duration_ms": 171, "truncated": false, "timed_out": false}

`ok: false` means ssher failed and the process exits 2. `ok: true` with a
non-zero `exit_code` means your command failed, which is a normal result and
still exits 0. Add `--check` if you want a failing command to become
`ok: false`.

`truncated: true` means output hit 64 KB and the middle was dropped. Narrow the
command, do not raise the cap. `timed_out: true` means ssh was killed; you still
get the output that arrived.

## 3. Flags

    --cwd DIR            run here; a leading ~ works
    --env NAME=VALUE     repeatable
    --timeout DURATION   90, 90s, 5m or 2h. A bare number is SECONDS. default 60
    --sudo               uses a stored sudo password if there is one
    --check              non-zero exit becomes ok:false
    --accept-new         record an unknown host key on first connect
    --raw                print stdout only, no JSON
    --max-output BYTES   default 65536

Flags go before the host or right after it. After the first non-flag word,
everything is part of the command.

**A bare `--timeout` number is SECONDS.** Not milliseconds. Write units and
there is nothing to get wrong:

    --timeout 90s
    --timeout 10m

Anything over two hours is refused, with a note on what you probably meant:
`--timeout 240000` is 2.8 days, not four minutes. Do not set a huge timeout to
"be safe" for long work. Start it as a job instead (section 7).

## 4. Start here on a host you have not used

    ssher --compact hosts        # every host, and where each came from
    ssher --compact test gpu1    # connect once; reports uname, home, shell

`host` is an alias from ~/.ssh/config, or `user@hostname:port`.

## 5. Files

    ssher --compact upload gpu1 ./dist /srv/app/dist -r --mkdir
    ssher --compact download gpu1 /var/log/app.log ./app.log

Source comes before destination in both. Directories need `-r`. Uploading into
a directory that does not exist yet needs `--mkdir`; scp will not create it.

To change a remote file: read it with `cat`, edit locally, `upload` it back. Do
not try to write files with here-docs on the command line.

## 6. Every call is a new shell

`cd`, `export` and activated environments do not survive between calls. Put
them in the same command, or use `--cwd` and `--env`.

    ssher --compact gpu1 'source ~/venv/bin/activate && python -V'

## 7. Long work: jobs

Anything that may take more than a minute: start it as a job, then wait for it.
Never poll on a timer you picked yourself.

    ssher --compact job start gpu1 --name train -- python train.py
    ssher --compact job wait gpu1 <job_id>

`job start` returns a job_id at once; the job runs detached on the server and
survives a dropped connection. It takes `--cwd`, `--env`, `--b64` and
`--cmd-file` like a normal command.

`job wait` blocks on the server and returns within a second of the job ending,
or after `--max-wait` (default 50s) with `state: running`. Then call it again,
passing the `log_offset` it returned so you get only what is new:

    ssher --compact job wait gpu1 <job_id> --since 18234

`output` is the new log, with progress-bar redraws collapsed to their last
frame. `exit_code` is set once `state` is `exited`. Other states: `killed`, and
`gone` (stopped without recording an exit, e.g. the server rebooted).

Return early when something appears in the log, for example an error or a
server coming up:

    ssher --compact job wait gpu1 <job_id> --until 'Traceback|Error|listening on'

If your harness can run a command in the background and notify you when it
exits, use that and wait in one go; you are woken the moment the job ends:

    ssher --compact job wait gpu1 <job_id> --max-wait 12h

Also: `job status` (no waiting), `job list`, `job kill` (stops everything the
job started). Logs live at `~/.ssher/jobs/<job_id>/log`; finished jobs are
removed after seven days.

## 8. Errors

    {"ok": false, "error": {"code": "AUTH_FAILED", "message": "...", "stderr": "..."}}

    HOST_NOT_FOUND      not in ~/.ssh/config; run `ssher hosts`
    AUTH_FAILED         no key accepted; ssh runs in batch mode and never
                        prompts, so a password will not help
    HOST_KEY_UNKNOWN    first connect; retry once with --accept-new
    HOST_KEY_MISMATCH   the key changed; report it, do not work around it
    CONNECT_FAILED      unreachable or unresolvable
    TIMEOUT             raise --timeout, or start it as a job
    JOB_NOT_FOUND       no such job; `job list` shows what is there
    TRANSFER_FAILED     scp failed; the message has its output
    BAD_REQUEST         bad arguments, config, or base64
    NO_SSH              the OpenSSH client is not on PATH

The `message` says what to do. Read it before retrying.

## 9. Two things that look like bugs

Output is UTF-8. On Windows, decode it as UTF-8 explicitly if you pipe ssher's
output into another program, or non-ASCII will look corrupted.

ANSI colour codes are passed through untouched. If a program colours its output,
turn that off at the program, for example with `--env TERM=dumb`.
"""
