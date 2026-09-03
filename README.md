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
same four walls: quoting that silently rewrites the command, output that floods
the context window, commands that hang with no timeout, and errors that say
nothing you can act on. It fixes those four and stops there. There is no daemon,
no connection pool, no session state, and nothing installed on your servers.

---

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Sending a command](#sending-a-command)
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
uv tool install agent-ssher
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

Windows PowerShell 5.1 strips double quotes out of an argument before the
program ever sees it. A command containing `"` or `'` therefore arrives
corrupted. Often it arrives corrupted with no error at all:

```bash
ssher gpu1 "echo $HOME"
```

prints the local Windows path, because PowerShell expanded `$HOME` first and
then bash ate the backslashes. Exit code 0, no warning.

A 30-case set covering every shell metacharacter was sent through each path and
compared byte for byte against what the remote actually received:

| Path | Result |
| --- | --- |
| Plain text from PowerShell 5.1 | 27 / 30 |
| Plain text from Git Bash or Linux | 30 / 30 |
| Command file | 30 / 30 |
| Base64 | 30 / 30 |

The three PowerShell failures all contained a quote character. Everything else,
including `$` `` ` `` `$()` `&` `|` `;` `>` `#` `{}` `[]` `@()` `%` `!` `\` `*`
`~` `^`, tabs, CJK and emoji, passes in plain text.

**Rule of thumb.** Agent on Windows: use a command file for scripts and base64
for one-liners. Agent on Linux or macOS: plain text is fine. Anywhere: a command
file is never wrong.

---

## Command reference

### `ssher <host> <command>...`

Run a command. The word `run` is optional and only needed when your host is
named the same as a subcommand.

```bash
ssher gpu1 --cwd /srv/app --timeout 300 'make build'
```

| Flag | Meaning |
| --- | --- |
| `--b64` | the command is base64 of UTF-8 text |
| `--cmd-file PATH` | read the command from a file, or from stdin when PATH is `-` |
| `--cwd DIR` | run in this directory; a leading `~` is expanded on the server |
| `--env NAME=VALUE` | set an environment variable; repeatable |
| `--timeout SECONDS` | give up and kill ssh after this long; default 60 |
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
  "home": "/home/xuan",
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
| `--timeout SECONDS` | default 600 |
| `--accept-new` | record an unknown host key on first connect |

A leading `~` in the remote path is expanded by the remote shell. Directories
without `-r` are refused rather than silently skipped.

scp will not create the destination's parent directory, and its own message does
not say which path it could not find. Uploading into a directory that does not
exist yet therefore fails with `TRANSFER_FAILED`, and the message names the
missing directory and tells you to pass `--mkdir`.

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
| `TIMEOUT` | raise `--timeout`, or start the work detached |
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

**Run something that outlives the connection.** There is no job system. Put the
launcher in a file and poll the log.

```bash
ssher gpu1 --cmd-file ./start-training.sh
```

```bash
ssher gpu1 'tail -c 4000 ~/train.log'
```

where `start-training.sh` holds:

```bash
setsid nohup python train.py > ~/train.log 2>&1 < /dev/null &
echo $!
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
SSHER_TEST_HOST=myhost uv run pytest tests/test_integration.py
```

The offline tests need no network. The end-to-end tests drive the real command
line the way an agent would, confine themselves to `~/ssher-pytest` on the
target host, and clean up after themselves.

Source layout:

| File | Contains |
| --- | --- |
| `cli.py` | argument parsing, the three command-input paths, JSON output |
| `ssh.py` | host resolution, script assembly, running ssh and scp, error classification |
| `config.py` | `~/.ssher/config.toml` and a reader for `~/.ssh/config` |
| `output.py` | decoding, ANSI stripping, size-bounded truncation |
| `secrets.py` | the OS keychain, for sudo passwords only |
| `guide.py` | the text `ssher agent-guide` prints |
