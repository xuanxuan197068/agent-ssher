from __future__ import annotations

from ssher import output


def test_short_text_is_untouched():
    text, truncated = output.truncate("hello", 100)
    assert (text, truncated) == ("hello", False)


def test_text_exactly_at_the_cap_is_untouched():
    text, truncated = output.truncate("x" * 100, 100)
    assert truncated is False and len(text) == 100


def test_truncation_keeps_both_ends():
    body = "".join(f"line{i}\n" for i in range(5000))
    text, truncated = output.truncate(body, 400)
    assert truncated is True
    assert text.startswith("line0\n")
    assert text.rstrip().endswith("line4999")
    assert "bytes dropped" in text


def test_truncation_reports_the_original_size():
    body = "a" * 10000
    text, _ = output.truncate(body, 200)
    assert "10000 total" in text


def test_truncation_does_not_split_a_multibyte_character():
    body = "码" * 5000
    text, truncated = output.truncate(body, 300)
    assert truncated is True
    text.encode("utf-8")  # would raise on a broken surrogate
    assert "�" not in text


def test_zero_cap_disables_truncation():
    assert output.truncate("x" * 1000, 0) == ("x" * 1000, False)


def test_strip_ansi_removes_colours():
    assert output.strip_ansi("\x1b[31mred\x1b[0m") == "red"


def test_strip_ansi_removes_an_osc_title():
    assert output.strip_ansi("\x1b]0;title\x07done") == "done"


def test_strip_ansi_removes_bracketed_paste_markers():
    assert output.strip_ansi("\x1b[?2004hhi\x1b[?2004l") == "hi"


def test_newlines_are_normalised():
    assert output.normalize_newlines("a\r\nb\rc") == "a\nb\nc"


def test_decode_replaces_invalid_bytes():
    assert output.decode(b"ok\xff") == "ok�"


def test_clean_combines_the_three_steps():
    text, truncated = output.clean(b"\x1b[32mhi\x1b[0m\r\n", 1000)
    assert (text, truncated) == ("hi\n", False)


# ------------------------------------------------- progress bars in job logs


def test_a_progress_bar_keeps_only_its_last_frame():
    log = "\r 10%|#         |\r 50%|#####     |\r100%|##########|\n"
    assert output.render_terminal(log) == "100%|##########|\n"


def test_lines_without_carriage_returns_are_untouched():
    assert output.render_terminal("one\ntwo\n") == "one\ntwo\n"


def test_windows_line_endings_are_not_mistaken_for_redraws():
    assert output.render_terminal("one\r\ntwo\r\n") == "one\ntwo\n"


def test_a_trailing_carriage_return_does_not_blank_the_line():
    # tqdm and friends often end a frame with \r, before the next one arrives.
    assert output.render_terminal("loss 0.31\r") == "loss 0.31"


def test_colour_and_cursor_codes_are_removed_from_a_log():
    assert output.render_terminal("\x1b[32mok\x1b[0m\x1b[2K\n") == "ok\n"


def test_clean_can_leave_carriage_returns_for_later():
    text, _ = output.clean("a\rb", 1000, ansi=False, newlines=False)
    assert text == "a\rb"
