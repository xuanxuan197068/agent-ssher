"""Output shaping: decoding, ANSI stripping, size-bounded truncation."""

from __future__ import annotations

import re

ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences, e.g. window titles
    r"|\x1b\[[0-?]*[ -/]*[@-~]"           # CSI sequences, e.g. colours
    r"|\x1b[@-Z\\-_]"                     # everything else two-byte
)
CR_RE = re.compile(r"\r\n?")

TRUNC_NOTE = "\n... [ssher: {dropped} bytes dropped, {total} total] ...\n"


def decode(data: bytes | str) -> str:
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def normalize_newlines(text: str) -> str:
    return CR_RE.sub("\n", text)


def truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    """Keep the head and the tail, drop the middle.

    The middle is where long build logs put the boring part; the head has the
    command echo and the tail has the error.
    """
    if max_bytes <= 0:
        return text, False
    raw = text.encode("utf-8")
    total = len(raw)
    if total <= max_bytes:
        return text, False

    head_budget = max_bytes // 2
    tail_budget = max_bytes - head_budget
    head = raw[:head_budget].decode("utf-8", errors="ignore")
    tail = raw[total - tail_budget:].decode("utf-8", errors="ignore")
    dropped = total - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    return head + TRUNC_NOTE.format(dropped=dropped, total=total) + tail, True


def clean(
    data: bytes | str, max_bytes: int, ansi: bool = True, newlines: bool = True
) -> tuple[str, bool]:
    text = decode(data)
    if newlines:
        text = normalize_newlines(text)
    if ansi:
        text = strip_ansi(text)
    return truncate(text, max_bytes)


def render_terminal(text: str) -> str:
    """Show a log the way the terminal would have left it.

    Progress bars redraw one line by writing a carriage return and the new
    state, hundreds of times. In a log file that is one enormous "line" holding
    every frame, and turning each carriage return into a newline would make it
    hundreds of lines instead. A terminal shows only the last frame, so that is
    what we keep: per line, the last non-empty segment after a carriage return.
    Colour and cursor-movement codes go too, since nothing will interpret them.
    """
    text = strip_ansi(text.replace("\r\n", "\n"))
    lines = []
    for line in text.split("\n"):
        if "\r" in line:
            frames = [frame for frame in line.split("\r") if frame.strip()]
            line = frames[-1] if frames else ""
        lines.append(line)
    return "\n".join(lines)
