# ssher — run commands on remote servers

    ssher --compact <host> <command>

Prints one JSON object on stdout. Use this instead of building an `ssh` command
line yourself. Always pass `--compact`.

## 1. Pick the input form. Getting this wrong corrupts commands silently.

| Your command | Use |
| --- | --- |
| no quote characters | plain text |
| has `'` or `"`, single line | `--b64` |
| multi-line, or a script | `--cmd-file` |

On Linux and macOS plain text is always safe. On Windows PowerShell it is safe
only without quotes, because PowerShell strips double quotes out of arguments
before the program sees them, usually with no error.

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
    --timeout SECONDS    default 60
    --sudo               uses a stored sudo password if there is one
    --check              non-zero exit becomes ok:false
    --accept-new         record an unknown host key on first connect
    --raw                print stdout only, no JSON
    --max-output BYTES   default 65536

Flags go before the host or right after it. After the first non-flag word,
everything is part of the command.

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

## 7. There is no job system

For work longer than a timeout, start it detached and poll the log.

    ssher --compact gpu1 --cmd-file ./start.sh   # setsid nohup ... > ~/log 2>&1 &
    ssher --compact gpu1 'tail -c 4000 ~/log'

## 8. Errors

    {"ok": false, "error": {"code": "AUTH_FAILED", "message": "...", "stderr": "..."}}

    HOST_NOT_FOUND      not in ~/.ssh/config; run `ssher hosts`
    AUTH_FAILED         no key accepted; ssh runs in batch mode and never
                        prompts, so a password will not help
    HOST_KEY_UNKNOWN    first connect; retry once with --accept-new
    HOST_KEY_MISMATCH   the key changed; report it, do not work around it
    CONNECT_FAILED      unreachable or unresolvable
    TIMEOUT             raise --timeout, or run it detached
    TRANSFER_FAILED     scp failed; the message has its output
    BAD_REQUEST         bad arguments, config, or base64
    NO_SSH              the OpenSSH client is not on PATH

The `message` says what to do. Read it before retrying.

## 9. Two things that look like bugs

Output is UTF-8. On Windows, decode it as UTF-8 explicitly if you pipe ssher's
output into another program, or non-ASCII will look corrupted.

ANSI colour codes are passed through untouched. If a program colours its output,
turn that off at the program, for example with `--env TERM=dumb`.
