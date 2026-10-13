# agent-ssher

Run a command on a remote server over SSH. Prints JSON. Built for coding agents,
usable by hand.

```bash
ssher gpu1 'df -h /data'
```

```json
{
  "ok": true,
  "host": "gpu1",
  "exit_code": 0,
  "stdout": "Filesystem      Size  Used Avail Use% Mounted on\n/dev/sdc       1007G   51G  906G   6% /data\n",
  "stderr": "",
  "duration_ms": 171,
  "truncated": false,
  "timed_out": false
}
```

It exists because agents that build `ssh` command lines by hand keep hitting the
same walls: quoting that silently rewrites the command, output that floods the
context window, commands that hang with no timeout, long tasks that can only be
followed by polling on a guessed interval, and errors that say nothing you can
act on. There is no daemon of its own, no connection pool, no session state,
and nothing installed on your servers.

It comes two ways, sharing one implementation: a command line, and an
[MCP server](#using-it-over-mcp) for agents whose harness speaks MCP. Over MCP
the command arrives as a plain string field, so the quoting problems below do
not arise at all.

---

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Using it over MCP](#using-it-over-mcp)
- [Sending a command](#sending-a-command)
- [Long-running work: jobs](#long-running-work-jobs)
- [Command reference](#command-reference)
- [Output reference](#output-reference)
- [Exit codes and error codes](#exit-codes-and-error-codes)
- [Host resolution](#host-resolution)
- [Configuration](#configuration)
- [Host keys](#host-keys)
- [sudo](#sudo)
- [Recipes](#recipes)
- [Limits and behaviour worth knowing](#limits-and-behaviour-worth-knowing)
- [How it works](#how-it-works)
- [Development](#development)

---

## Install

```bash
uv tool install git+https://github.com/xuanxuan197068/agent-ssher
```

From a checkout:

```bash
uv tool install --editable .
```

Requires Python 3.11 or newer and the OpenSSH client on PATH. `ssh` and `scp`
ship with Windows 10 and 11, macOS, and every Linux distribution.

Two launchers are installed:

| Launcher | Use it when |
| --- | --- |
| `ssher` | a terminal or a shell script is calling it |
| `ssherw` | a windowed program is calling it, and a console flashing on every command would be unacceptable |

They behave identically. `ssherw` is built as a Windows GUI binary so Windows
allocates no console for it. Because it has no console, typing `ssherw` at a
prompt prints nothing; it is meant to be called with its output piped.

## Quick start

```bash
ssher hosts
```

```bash
ssher test gpu1
```

```bash
ssher gpu1 uptime
```

If you already use `~/.ssh/config`, there is no setup at all. Whatever works
with `ssh myhost` works with `ssher myhost`, including `IdentityFile`,
`ProxyJump`, `Port`, `User` and your host key policy, because ssher hands the
alias to `ssh` and lets `ssh` resolve it.

---

## Using it over MCP

`ssher mcp` serves the same operations over MCP on stdio. You do not run it
yourself: the agent's harness starts it when a session opens and stops it when
the session ends. Typed at a prompt it appears to hang, because it is waiting
for JSON-RPC on stdin.

Register it once with Claude Code, for every project:

```bash
claude mcp add --scope user ssher -- ssher mcp
```

With Codex, add to `~/.codex/config.toml`:

```toml
[mcp_servers.ssher]
command = "ssher"
args = ["mcp"]
tool_timeout_sec = 900

[mcp_servers.ssher.env]
SSHER_MCP_CALL_LIMIT = "840"
```

Nine tools appear in the agent's tool list:

| Tool | Does |
| --- | --- |
| `hosts` | lists every reachable host |
| `test` | connects once, reports uname, home, shell, round trip |
| `run` | runs a command and waits for it |
| `upload`, `download` | copy files and directories over scp |
| `job_start` | starts long work detached, returns a job id at once |
| `job_wait` | blocks until the job ends, or a ceiling; returns new output |
| `job_kill`, `job_list` | stop a job, list jobs |

**Why it is better than the CLI for an agent.** The command is a JSON string, so
no shell on this side parses it: quotes, `$VARS`, backticks, here-docs and
newlines all arrive as written, with no `--b64` or `--cmd-file` needed. The
timeout parameter is called `timeout_seconds`, so the unit is not a guess. The
tools are listed with their parameters, so there is no usage page to paste
anywhere. And each call skips starting Python.

**The per-call limit.** Every MCP client cuts off a tool call that runs too
long, and they disagree on how long that is: Codex defaults to 60 seconds,
configurable per server as `tool_timeout_sec`, and the Claude Code desktop app
has been reported to stop stdio calls at about 60 seconds regardless of
settings. So no ssher tool blocks longer than `SSHER_MCP_CALL_LIMIT` seconds,
50 by default, minus a few seconds kept back for the reply. A `run` that would
need longer stops at the limit and says to use a job; `job_wait` returns
`running` and is called again. For a client that allows longer calls, set the
variable a little under its limit, as in the Codex example above, and fewer
`job_wait` calls are needed.

The CLI is unaffected and stays the right tool for people, for scripts, and for
agents without MCP.

---

## Sending a command

This is the part that matters. There are three ways to get a command in, and
which one you pick decides whether your quotes survive.

### 1. Plain text

Fine when the command contains no quote characters.

```bash
ssher gpu1 'df -h /data'
```

```bash
ssher gpu1 ls -la /etc
```

Arguments after the host are joined with a space, so simple commands need no
quoting at all. Use `--` to separate flags from a command that starts with a
dash.

### 2. A command file

The best option for anything multi-line. Write the script with whatever writes
files, then point at it. The content never passes through a shell, so nothing
can rewrite it, and it is not subject to any command-line length limit.

```bash
ssher gpu1 --cmd-file ./deploy.sh
```

A leading byte-order mark is stripped and CRLF line endings are converted to LF,
so a file written by a Windows editor runs correctly on a Linux server. A stray
carriage return inside a here-doc is one of the least pleasant things to debug,
and this removes the possibility.

Pass `-` to read the command from standard input instead:

```bash
ssher gpu1 --cmd-file - < ./deploy.sh
```

Piping into it from PowerShell is **not** recommended: PowerShell prepends a
byte-order mark, converts newlines to CRLF, and on a default installation
encodes the pipe as ASCII, which destroys non-ASCII characters before ssher can
see them. Use a real file there.

### 3. Base64

For a one-liner that contains quotes. The encoded form is plain ASCII, so no
shell between you and the server can touch it.

```bash
ssher gpu1 --b64 ZWNobyAiaXQncyAkSE9NRSI=
```

You should rarely compute that by hand. Let the shell do it. In PowerShell a
here-string needs no escaping whatsoever, not even doubled single quotes, and
the encoding happens inside PowerShell before anything crosses into the
program's arguments:

```bash
ssher gpu1 --b64 ([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(@'
it's "quoted" $HOME `whoami` $(id -u) and 中文
'@)))
```

In bash:

```bash
ssher gpu1 --b64 "$(base64 -w0 <<'EOF'
it's "quoted" $HOME `whoami` $(id -u)
EOF
)"
```

### Why this matters, with numbers

Windows PowerShell parses your argument before ssher ever runs, and how much
damage it does depends on how you quoted it — which is exactly the judgement an
agent gets wrong. Two failure modes, both silent:

```bash
ssher gpu1 "echo $HOME"
```

Inside double quotes, PowerShell expands `$HOME` to the local Windows path
first, and the remote shell never sees the variable. Exit code 0, no warning.

```bash
ssher gpu1 "grep x $(cat f)"
```

Inside double quotes, `$(...)` is a PowerShell subexpression: it runs `cat f`
**locally**, substitutes the result, and only then calls ssher. Your command
reaches the server with that part replaced by local output — often empty, or a
PowerShell error like `head : not recognized`. It can still return `ok: true`
with a plausible but wrong result. Escaping does not save you: PowerShell's
escape character is the backtick, not the backslash, so `\$(...)` runs anyway.

A 30-case set covering every shell metacharacter, wrapped in PowerShell **single**
quotes, was compared byte for byte against what the remote received:

| Path | Result |
| --- | --- |
| Plain text, single-quoted, from PowerShell 5.1 | 27 / 30 |
| Plain text from Git Bash or Linux | 30 / 30 |
| Command file | 30 / 30 |
| Base64 | 30 / 30 |

The three PowerShell failures contained a `"`. Single quotes protect `$` and
`$(...)` — but only while the command has no single quote of its own, and only
if the agent remembers to use them. Double quotes protect nothing.

**Rule of thumb.** Agent on Windows: don't reason about quoting at all. Use a
command file for anything multi-line, base64 for one-liners. Agent on Linux or
macOS: plain text is fine. Anywhere: a command file is never wrong.

---

## Long-running work: jobs

An agent's call is synchronous, so a training run or a long build cannot simply
be a long call: it would block, hit some client's limit, and lose its output if
the connection dropped. And polling a log on a timer means guessing an interval
that is always either too short or too long.

So long work is split in two. `job start` launches it detached on the server and
returns at once. `job wait` blocks **on the server** and returns within a second
of the job ending, or after a ceiling with `state: running`, in which case you
call it again.

```bash
ssher job start gpu1 --name train -- python train.py
```

```bash
ssher job wait gpu1 20261013-142530-a1b2-train
```

```json
{
  "ok": true,
  "job_id": "20261013-142530-a1b2-train",
  "state": "exited",
  "exit_code": 0,
  "elapsed_seconds": 2394,
  "output": "epoch 40/40 loss 0.112\nsaved checkpoint\n",
  "log_offset": 184211,
  "output_skipped_bytes": 0,
  "log_path": "~/.ssher/jobs/20261013-142530-a1b2-train/log"
}
```

What makes it pleasant to follow:

- **Only new output.** Pass the `log_offset` from the last call as `--since`, and
  `output` holds only what was printed since. Nothing is repeated, so a forty
  minute run does not burn context re-reading the same tail.
- **Progress bars collapsed.** A tqdm-style bar redraws one line with carriage
  returns, hundreds of times. `output` keeps only the last frame of each, the
  way a terminal would have shown it.
- **Return on a pattern.** `--until 'Traceback|Error|listening on'` returns as
  soon as new output matches, even though the job is still running. A crash is
  noticed when it happens, not when training would have finished.
- **Bounded output.** If more than `--max-output` bytes (8 KB) are new, the most
  recent part is returned and the rest counted in `output_skipped_bytes`.
- **Nothing lost between calls.** A `job wait` that hits its ceiling, or whose
  connection is dropped by a busy sshd, costs nothing: the job's state is on the
  server's disk, and the next call carries on. A dropped handshake is retried
  once automatically.

`state` is one of `running`, `exited` (with `exit_code`), `killed`, or `gone`,
which means it stopped without recording an exit, typically because the server
rebooted.

**Wait in one go, where the harness allows it.** Claude Code can run a command
in the background and wake the agent when it exits. Run the wait that way with
a long ceiling, and the agent is notified the moment the job ends, with no
polling at all:

```bash
ssher job wait gpu1 20261013-142530-a1b2-train --max-wait 12h
```

Everything lives under `~/.ssher/jobs/<job_id>/` on the server: the script, its
log, its pid and its exit status. Finished jobs are removed after seven days.

---

## Command reference

### `ssher <host> <command>...`

Run a command. The word `run` is optional and only needed when your host is
named the same as a subcommand.

```bash
ssher gpu1 --cwd /srv/app --timeout 5m 'make build'
```

| Flag | Meaning |
| --- | --- |
| `--b64` | the command is base64 of UTF-8 text |
| `--cmd-file PATH` | read the command from a file, or from stdin when PATH is `-` |
| `--cwd DIR` | run in this directory; a leading `~` is expanded on the server |
| `--env NAME=VALUE` | set an environment variable; repeatable |
| `--timeout DURATION` | give up after this long: `90`, `90s`, `5m`, `2h`; default 60s, at most 2h |
| `--sudo` | run under sudo, using a stored password if there is one |
| `--tty` | allocate a terminal, for the few tools that insist on one |
| `--shell SHELL` | wrapper shell; default `bash -lc`, or `none` to skip it |
| `--stdin TEXT` | text to feed the command's stdin, or `-` to pass this process's |
| `--max-output BYTES` | cap per stream; default 65536 |
| `--check` | report `ok: false` when the exit code is non-zero |
| `--accept-new` | record an unknown host key on first connect |

Flags may appear before the host or immediately after it. Once a non-flag word
appears, everything from there on is the command, so `ssher gpu1 ls -la` works
and `ssher gpu1 ls --cwd /srv` sends `--cwd /srv` to `ls`.

A bare number is seconds. The two-hour ceiling is there to catch the commonest
mistake an agent makes with this flag, writing milliseconds:

```json
{"ok": false, "error": {"code": "BAD_REQUEST", "message": "--timeout 240000 is 2.8 days, past the 2.0 hours limit. --timeout counts seconds, not milliseconds: if you meant 240000 ms, pass 240. Work that long belongs in `ssher job start`, collected with `ssher job wait`."}}
```

### `ssher hosts`

Every host ssher can see, and where each came from.

```json
{
  "ok": true,
  "hosts": [
    {
      "alias": "gpu1",
      "hostname": "10.0.0.5",
      "user": "trainer",
      "port": 2222,
      "source": "ssh_config"
    }
  ],
  "count": 1,
  "ssh_config": "C:\\Users\\you\\.ssh\\config",
  "ssher_config": "C:\\Users\\you\\.ssher\\config.toml"
}
```

`source` is `ssh_config`, `ssher`, or `ssh_config+ssher`. A host that is only in
ssher's own config carries a `note` saying ssh cannot reach it, which is usually
a typo or a leftover. Hosts you have annotated also show `description`, `tags`,
`init`, `cwd` and `has_sudo_password`.

### `ssher test <host>`

Connect once and report what is on the other end. Run this first when a host is
new to you.

```json
{
  "ok": true,
  "host": "WSL",
  "ssh_host": "WSL",
  "round_trip_ms": 343,
  "uname": "Linux box 5.15.167.4 ... x86_64 GNU/Linux",
  "home": "/home/alice",
  "shell": "/bin/bash",
  "has_bash": true
}
```

Takes `--accept-new`.

### `ssher upload <host> <local> <remote>`

### `ssher download <host> <remote> <local>`

Copy files or directories, through `scp`. Note the argument order differs: local
first for upload, remote first for download, so in both cases the source comes
before the destination.

```bash
ssher upload gpu1 ./dist /srv/app/dist -r
```

```bash
ssher download gpu1 /var/log/app.log ./app.log
```

| Flag | Meaning |
| --- | --- |
| `-r`, `--recursive` | required for a directory |
| `--mkdir` | upload only; create the remote parent directory first |
| `--timeout DURATION` | default 600s |
| `--accept-new` | record an unknown host key on first connect |

A leading `~` in the remote path is expanded by the remote shell. Directories
without `-r` are refused rather than silently skipped.

scp will not create the destination's parent directory, and its own message does
not say which path it could not find. Uploading into a directory that does not
exist yet therefore fails with `TRANSFER_FAILED`, and the message names the
missing directory and tells you to pass `--mkdir`.

### `ssher job start|wait|status|kill|list`

See [Long-running work](#long-running-work-jobs) for how they fit together.

```bash
ssher job start gpu1 --name train --cwd /srv/app -- python train.py
```

| `job start` flag | Meaning |
| --- | --- |
| `--name LABEL` | shown in `job list`, and folded into the job id |
| `--cwd`, `--env` | as for a command |
| `--b64`, `--cmd-file` | as for a command; the script is stored on the server as given |

```bash
ssher job wait gpu1 <job_id> --since 184211 --until 'Traceback' --max-wait 5m
```

| `job wait` flag | Meaning |
| --- | --- |
| `--max-wait DURATION` | longest to block; default 50s, at most 24h |
| `--since OFFSET` | only output after this log offset; pass the last `log_offset` |
| `--until REGEX` | also return as soon as new output matches; reported as `matched` |
| `--max-output BYTES` | most new output returned, the most recent part; default 8192 |

`job status <host> <job_id>` is a wait that does not wait. `job kill` stops the
job's whole process group and records it as `killed`. `job list` shows every
job on the host, newest first.

### `ssher mcp`

Serves everything above over MCP on stdio. Started by an agent's harness, not
by hand; see [Using it over MCP](#using-it-over-mcp).

### `ssher secret set|rm|list <host>`

Store a sudo password in the OS keychain. See [sudo](#sudo).

### `ssher config init|path|show`

`init` writes a commented starter file, refusing to overwrite unless you pass
`--force`. `path` prints where both config files live. `show` prints the merged
configuration as JSON, and is the fastest way to find a typo.

### `ssher agent-guide`

Prints a short page written for an agent to read. Paste it into `CLAUDE.md`,
`AGENTS.md`, or a system prompt.

### Global flags

| Flag | Meaning |
| --- | --- |
| `--compact` | single-line JSON; saves tokens, use it from an agent |
| `--raw` | print stdout instead of JSON, and mirror the remote exit code |
| `--ascii` | escape non-ASCII in the JSON output |
| `--exit-code` | exit with the remote command's status |
| `--version` | print the version |

These work before or after the subcommand.

---

## Output reference

Every command prints exactly one JSON object. Success always carries
`"ok": true`.

**Running a command:**

| Field | Meaning |
| --- | --- |
| `exit_code` | the remote command's status, or `-1` when it was killed |
| `stdout`, `stderr` | captured separately, never merged |
| `duration_ms` | wall time for the whole call, including the handshake |
| `truncated` | true when output hit the cap and the middle was dropped |
| `timed_out` | true when the timeout fired; you still get partial output |
| `hint` | present on a timeout, suggesting what to do instead |

**Failure**, from any command:

```json
{
  "ok": false,
  "error": {
    "code": "AUTH_FAILED",
    "message": "gpu1: the server rejected every key offered. ...",
    "stderr": "gpu1: Permission denied (publickey)."
  }
}
```

`stderr` is present when ssh itself said something, and is the raw text, not a
paraphrase.

**Truncation** keeps the head and the tail and drops the middle, because the
head has the command echo and the tail has the error:

```
1
2
...
... [ssher: 23693 bytes dropped, 23893 total] ...
4999
5000
```

When `truncated` is true, narrow the command rather than raising the cap. `tail
-c 4000` beats a bigger `--max-output`.

---

## Exit codes and error codes

| Exit code | Meaning |
| --- | --- |
| 0 | ssher did its job; look at `exit_code` in the JSON for the command's own result |
| 1 | only with `--exit-code` or `--raw`, when the remote command was killed |
| 2 | ssher could not do its job |

A remote command exiting non-zero is **not** a tool failure. `grep` finding
nothing exits 1, and that is a result, not a malfunction. So ssher still exits 0
and reports `exit_code` in the JSON. Change that with `--check`, which turns it
into `ok: false`, or `--exit-code`, which mirrors the status at the process
level.

| Error code | What to do |
| --- | --- |
| `HOST_NOT_FOUND` | not in `~/.ssh/config`; run `ssher hosts` |
| `AUTH_FAILED` | no key was accepted; add a key, ssh runs in batch mode and never prompts |
| `HOST_KEY_UNKNOWN` | first time seeing this server; re-run with `--accept-new` |
| `HOST_KEY_MISMATCH` | the key changed; investigate, do not work around it |
| `CONNECT_FAILED` | unreachable or unresolvable |
| `TIMEOUT` | raise `--timeout`, or start the work as a job |
| `JOB_NOT_FOUND` | no such job on that host; `job list` shows what is there |
| `EXEC_FAILED` | with `--check`, the command exited non-zero |
| `TRANSFER_FAILED` | scp failed; the message carries its output |
| `BAD_REQUEST` | bad arguments, bad config, bad base64 |
| `SECRET_ERROR` | the keychain was unavailable |
| `NO_SSH` | the OpenSSH client is not on PATH |

---

## Host resolution

`host` accepts three forms, tried in this order:

1. **An alias**, matched case-insensitively against `~/.ssh/config` and against
   `[hosts.*]` in ssher's own config. The alias is handed to `ssh` unchanged, so
   ssh resolves the hostname, user, port, key and proxy itself.
2. **`user@hostname`**, optionally `:port`, for a host that is in no config.
   IPv6 goes in brackets: `root@[2001:db8::1]:2200`.
3. **A dotted hostname** such as `box.example.com`.

A single bare word that matches no alias is rejected rather than guessed at,
because a mistyped alias is far more likely than a real single-label hostname.

---

## Configuration

Optional, at `~/.ssher/config.toml`. Create a starting point with `ssher config
init`. Everything works without it.

```toml
[defaults]
timeout = 60            # seconds before a command is given up on
max_output = 65536      # bytes kept per stream
shell = "bash -lc"      # wrapper for each command; "none" runs it directly
connect_timeout = 20    # seconds to wait for the TCP connection
transfer_timeout = 600  # seconds for an upload or download

[hosts.gpu1]
description = "training box"
tags = ["gpu"]
init = "source ~/miniconda3/etc/profile.d/conda.sh && conda activate torch"
cwd = "/srv/app"
env = { HF_HOME = "/data/hf" }
timeout = 300
```

`init` runs before every command on that host and is guarded, so if the
activation fails the command does not run in the wrong environment. This is the
one piece of per-host state that survives, and it is where an environment
activation belongs when every call is otherwise its own shell.

**Connection settings are deliberately rejected here.** `hostname`, `user`,
`port`, `identity_file` and `proxy_jump` belong in `~/.ssh/config`, where ssh
reads them and where they also work for plain `ssh`, `scp` and `rsync`. Putting
one in this file is an error naming the offending key, not a silent no-op.

Set `SSHER_HOME` to move the config directory, and `SSHER_SSH_CONFIG` to point
at a different ssh config. Both are mainly for testing.

---

## Host keys

ssher never puts `StrictHostKeyChecking` on the ssh command line, so it never
overrides what you set. If you have `StrictHostKeyChecking yes` on a host, it
stays `yes`.

An unknown host key returns `HOST_KEY_UNKNOWN` and tells you to pass
`--accept-new` or to connect once with plain `ssh` first. A key that has
**changed** returns `HOST_KEY_MISMATCH`, and no flag will get you past it. If a
server really was rebuilt, remove its line from `~/.ssh/known_hosts` yourself.

---

## sudo

ssher runs ssh with `BatchMode=yes`. A host that cannot authenticate with a key
fails immediately instead of waiting at a password prompt no agent can answer.
There is no login-password support, on purpose: give such a host a key.

A sudo password is a different thing, because the remote shell asks for it after
login and no key avoids it. Store one per host:

```bash
ssher secret set gpu1
```

```bash
ssher secret list
```

```bash
ssher secret rm gpu1
```

It goes into the OS keychain, Windows Credential Manager on Windows. It never
appears in a config file, in an argument, or in the output. With one stored,
`--sudo` uses `sudo -S` and feeds it on stdin. Without one, `--sudo` uses `sudo
-n`, which fails cleanly with `sudo: a password is required` rather than
hanging.

---

## Recipes

**Check a service and read its recent log.**

```bash
ssher gpu1 'systemctl is-active nginx; journalctl -u nginx -n 20 --no-pager'
```

**Deploy a directory and restart.**

```bash
ssher upload gpu1 ./dist /srv/app/dist -r --mkdir
```

```bash
ssher gpu1 --sudo 'systemctl restart app'
```

**Edit a remote config.** Read it, change it locally, upload it. Nothing passes
through a shell in either direction.

```bash
ssher gpu1 'cat /etc/nginx/nginx.conf'
```

```bash
ssher upload gpu1 ./nginx.conf /etc/nginx/nginx.conf
```

**Train a model and hear about it the moment it fails or finishes.**

```bash
ssher job start gpu1 --name train --cwd ~/proj -- python train.py --epochs 40
```

```bash
ssher job wait gpu1 <job_id> --until 'Traceback|CUDA out of memory'
```

**Start a server and wait until it is actually up.**

```bash
ssher job start gpu1 --name api -- python -m uvicorn app:app --port 8000
```

```bash
ssher job wait gpu1 <job_id> --until 'Uvicorn running'
```

**Use a conda environment.** Either put it in the same command:

```bash
ssher gpu1 'source ~/miniconda3/etc/profile.d/conda.sh && conda activate torch && python -V'
```

or set `init` for that host once, and every later call gets it for free.

**Pipe output into another program.** `--raw` prints stdout and nothing else.

```bash
ssher gpu1 --raw 'cat /var/log/app.log' > app.log
```

---

## Limits and behaviour worth knowing

**Every call is its own shell.** `cd`, `export` and activated environments do
not carry over. Put them in the same command, use `--cwd` and `--env`, or set
`init` for the host.

**A command over 8 KB travels on stdin.** ssher hands it to the remote shell's
standard input instead of the command line, which removes the operating system's
argument length limit; a 200 KB script works. That path cannot also carry
`--stdin` or a sudo password, and says so plainly if you ask for both.

**Killing ssh usually kills the remote command,** because the remote sshd hangs
up its session. It is not guaranteed for a process that has detached itself,
which is exactly what you want for a background job.

**Output is UTF-8.** On Windows, decode it as UTF-8 explicitly when piping
ssher's output into another program. The console code page will otherwise mangle
non-ASCII, and it will look like ssher's fault.

**ANSI escapes are left alone by default.** A program that colours its output
unconditionally will put escape sequences in `stdout`. Pass `--no-color` style
flags to that program, or set `TERM=dumb` with `--env`, rather than expecting
ssher to clean up after it.

**`--tty` changes several things at once.** It allocates a real terminal, so
programs that refuse to run without one will work. It also makes ssher strip
ANSI sequences, and it makes ssh add a line like `Connection to 10.0.0.5
closed.` to `stderr`. Use it only when a program genuinely needs a terminal.

---

## How it works

There is no daemon, no connection pool and no protocol implementation. Each call
builds an `ssh` command line, runs it, shapes the output, and exits.

```
ssher gpu1 'uptime'
  -> ssh -T -o BatchMode=yes gpu1 "bash -lc 'uptime'"
  -> JSON on stdout
```

The MCP server is a second way in to the same code. It stays up for as long as
the agent's session, because that is how MCP works, but it is the harness that
starts and stops it: ssher never spawns anything of its own, and nothing is left
running when the session ends.

```
 terminal, scripts, agents without MCP        agents with MCP
              ssher gpu1 uptime                  ssher mcp
           (parses the command line)        (parses JSON requests)
                        \                        /
                         ssh.py and jobs.py: host lookup, script
                         assembly, ssh and scp, error classification
                                        |
                                ssh.exe -> server
```

A job is a script written to `~/.ssher/jobs/<id>/` and launched with `setsid`,
so it leads its own session and process group. A wrapper records its pid, its
start and finish times, and its exit status, written atomically and last.
`job wait` is a short loop on the server that watches for that status file, a
dead process, or a pattern in the log, so the agent's side never has to keep
time.

Driving OpenSSH rather than importing an SSH library is both simpler and faster
here. Importing a Python SSH library costs more time per call than the TCP
handshake it would let you avoid, and OpenSSH already knows how to read your
config, talk to your agent, jump through your bastion, and enforce your host key
policy.

| Approach | Per call, LAN host |
| --- | --- |
| Plain `ssh` | 205 ms |
| ssher | 250 ms |

The 45 ms difference is Python starting up.

---

## Development

```bash
uv sync --extra dev
```

```bash
uv run pytest
```

```bash
SSHER_TEST_HOST=myhost uv run pytest tests/test_integration.py tests/test_integration_jobs.py
```

The offline tests need no network. The end-to-end tests drive the real command
line the way an agent would, and the real MCP server through the official
client over stdio. They confine themselves to `~/ssher-pytest` and to the jobs
they start themselves, so they are safe to point at a machine with real work on
it.

Source layout:

| File | Contains |
| --- | --- |
| `cli.py` | argument parsing, the three command-input paths, JSON output |
| `ssh.py` | host resolution, script assembly, running ssh and scp, error classification |
| `config.py` | `~/.ssher/config.toml` and a reader for `~/.ssh/config` |
| `output.py` | decoding, ANSI stripping, size-bounded truncation |
| `secrets.py` | the OS keychain, for sudo passwords only |
| `guide.py` | the text `ssher agent-guide` prints |
