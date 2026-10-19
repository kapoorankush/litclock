"""litclock-dev#532 RT6 (eng re-review Scope #8 as corrected by R18): EVERY
`tr('key', 'fallback')` call in the PWA's static JS, not a surface at a time.

`tr()` prefers the server-injected strings blob and falls back to an English
literal. R18 keeps those literals as stale-JS-window fallbacks (static URLs are
unversioned, cached up to 15 minutes), which is only safe if:

1. every key a file asks for IS in the blob that file reads — otherwise the
   literal fires on every render, and a non-English page shows that one
   string in English permanently, not just in the stale window;
2. every literal is an exact copy of the English catalog value — otherwise
   the stale window shows text the catalog no longer has.

The per-surface tests in test_cross_file_string_parity.py pin chosen keys;
this one enumerates every call, so a new `tr()` cannot be added unpinned.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
JS_DIR = REPO_ROOT / "src" / "control_server" / "static" / "js"
TEMPLATES = REPO_ROOT / "src" / "control_server" / "templates"
EN = json.loads((REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))

# tr( 'key' , <fallback> ) — the fallback is captured when it is string
# literals only (single- or double-quoted, optionally joined with `+` across
# lines). Every `tr` token outside a literal is found first by _TOKEN; a
# `tr(` must then parse as _CALL at that position, so a call in any other
# shape (a double-quoted or computed key, a variable fallback) is reported
# rather than silently skipped, and so is every other use (`tr (`, `tr?.(`,
# `t = tr`, `tr.call`), so no call can reach tr() unseen.
_LIT = r"""(?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")"""
_CALL = re.compile(r"tr\(\s*'([a-z0-9_.]+)'\s*,\s*(" + _LIT + r"(?:\s*\+\s*" + _LIT + r")*)?\s*\)", re.S)
_TOKEN = re.compile(r"(?<![\w$.])tr(?![\w$])")
_DEF = re.compile(r"\bfunction\s+$")
_BLOB = re.compile(r"data-litclock-(shell-)?strings>\{\{\s*catalog_subset\(\[(.*?)\]\)", re.S)
_KEY = re.compile(r"'([a-z0-9_.]+)'")
# A `/` after one of these punctuators, after one of these keywords, or at
# the start opens a regex literal, not a division — the usual tokenizer
# heuristic.
_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_KEYWORDS = re.compile(
    r"(?<![\w$.])(?:return|typeof|case|do|else|in|of|instanceof|new|delete|void|throw|yield|await)\s*$"
)


def _scan(text: str) -> tuple[str, str]:
    """(code, blanked): the JS with comments removed, and the same text with
    every string, template and regex literal's body blanked as well.

    Comments are found by a scanner that knows where literals are, so a
    `/*` or `//` inside `'image/*'`, a URL or a regex does not open a comment
    that swallows real calls, and a trailing `// tr('x', 'y')` is removed.
    A template's `${...}` is scanned as code (a call in it is a call, and a
    template nested inside it is its own literal), tracked by brace depth.
    """
    code, blank = [], []
    i, n = 0, len(text)
    # One entry per open `${`: the `{` depth inside that substitution.
    subst: list[int] = []

    def emit(chunk: str, blanked: str | None = None) -> None:
        code.append(chunk)
        blank.append(chunk if blanked is None else blanked)

    def regex_can_start() -> bool:
        tail = "".join(code[-40:]).rstrip()
        return not tail or tail[-1] in _REGEX_PREV or bool(_REGEX_KEYWORDS.search(tail))

    def template_from(j: int) -> int:
        """Emit a template body from j (just past a backtick or a `}`) up to
        its closing backtick or its next `${`; return where code resumes."""
        start = j
        while j < n:
            if text[j] == "\\":
                j += 2
            elif text[j] == "`":
                body = text[start:j]
                emit(body + "`", re.sub(r"[^\n]", " ", body) + "`")
                return j + 1
            elif text.startswith("${", j):
                body = text[start:j]
                emit(body + "${", re.sub(r"[^\n]", " ", body) + "${")
                subst.append(0)
                return j + 2
            else:
                j += 1
        body = text[start:]
        emit(body, re.sub(r"[^\n]", " ", body))
        return n

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            emit(" ")
        elif c == "`":
            emit("`")
            i = template_from(i + 1)
        elif subst and c == "{":
            subst[-1] += 1
            emit(c)
            i += 1
        elif subst and c == "}":
            emit(c)
            if subst[-1]:
                subst[-1] -= 1
                i += 1
            else:
                subst.pop()
                i = template_from(i + 1)
        elif c in "'\"" or (c == "/" and regex_can_start()):
            j, in_class = i + 1, False
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if c == "/" and text[j] == "[":
                    in_class = True
                elif c == "/" and text[j] == "]":
                    in_class = False
                elif text[j] == c and not in_class:
                    break
                elif text[j] == "\n":
                    break
                j += 1
            lit = text[i : j + 1]
            emit(lit, c + re.sub(r"[^\n]", " ", lit[1:-1]) + lit[-1:])
            i = j + 1
        else:
            emit(c)
            i += 1
    return "".join(code), "".join(blank)


def _code(path: Path) -> str:
    """The JS with comments removed, so a key named in a comment is no call."""
    return _scan(path.read_text(encoding="utf-8"))[0]


def _calls(code: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(key, fallback literal) for every tr() call, plus the uses of `tr`
    this test cannot read — each of which is a failure, not a skip.

    Calls are LOCATED in the blanked text, so `tr(` inside a string or a
    template's text is no call, and PARSED from the real text at the same
    offset (blanking keeps every length)."""
    blanked = _scan(code)[1]
    calls, unreadable = [], []
    for m in _TOKEN.finditer(blanked):
        if blanked.startswith("tr(", m.start()):
            if _DEF.search(blanked, 0, m.start()):
                continue
            call = _CALL.match(code, m.start())
            if call:
                calls.append(call.groups(default=""))
                continue
        unreadable.append(" ".join(code[max(0, m.start() - 20) : m.start() + 40].split()))
    return calls, unreadable


def _js_files_with_tr():
    """Every file that uses a `tr` token outside a literal, not only the
    ones that define it with `function tr(`."""
    return sorted(p for p in JS_DIR.glob("*.js") if _TOKEN.search(_scan(_code(p))[1]))


def _blobs(template_text: str) -> tuple[set[str], set[str]]:
    page, shell = set(), set()
    for is_shell, body in _BLOB.findall(template_text):
        (shell if is_shell else page).update(_KEY.findall(body))
    return page, shell


def _templates_loading(js: Path) -> list[Path]:
    needle = f"js/{js.name}"
    return [t for t in TEMPLATES.glob("*.html.j2") if needle in t.read_text(encoding="utf-8")]


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_ESCAPE = re.compile(r"\\(u\{([0-9a-fA-F]+)\}|u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|\n|.)", re.S)


def _js_unescape(body: str) -> str:
    def one(m: re.Match) -> str:
        code = m.group(2) or m.group(3) or m.group(4)
        if code:
            return chr(int(code, 16))
        ch = m.group(1)
        return "" if ch == "\n" else _ESCAPES.get(ch, ch)

    return _ESCAPE.sub(one, body)


def _unescape(expr: str) -> str:
    """The JS string a `'a' + "b"` literal expression evaluates to."""
    return "".join(_js_unescape(p[1:-1]) for p in re.findall(_LIT, expr))


@pytest.mark.parametrize(
    ("js", "calls", "unreadable"),
    [
        # A `/*` inside a string, a comment or a regex opens no comment.
        ("var a = 'image/*';\ntr('a.b', 'v');\nfoo(); /* x */", [("a.b", "'v'")], 0),
        ("// matches image/*\ntr('a.b', 'v');\n/* x */", [("a.b", "'v'")], 0),
        ("var r = /['\"]/g; tr('a.b', 'v');", [("a.b", "'v'")], 0),
        ("x = y / 2; tr('a.b', 'v'); z = 3 / 4;", [("a.b", "'v'")], 0),
        # A regex whose class holds `/*` opens no comment; a division
        # before a trailing comment is no regex that keeps the comment.
        ("var r = /[/*]/; tr('a.b', 'v'); /* x */", [("a.b", "'v'")], 0),
        ("x = a / b; // tr('ghost.key', 'x')\n", [], 0),
        # A `/` after a keyword opens a regex; a template nested in `${}` is
        # its own literal and the outer one resumes after it.
        ("return /'/.test(s) ? 'http://x' : tr('a.b', 'v');", [("a.b", "'v'")], 0),
        ("return /'/.test(s) && tr ('a.b', 'v');", [], 1),
        ("el(`${a ? `http://x` : ''}${tr('a.b', 'v')}`);", [("a.b", "'v'")], 0),
        ("el(`a ${ {k: 1}.k } // b ${tr('a.b', 'v')}`);", [("a.b", "'v'")], 0),
        ("el(`tr('ghost.key', 'x')`);", [], 0),
        ("el(`${tr('a.b', 'v')}`);", [("a.b", "'v'")], 0),
        # A trailing comment is no call; a string naming `tr` is no use of it.
        ("foo(); // tr('ghost.key', 'x')\n", [], 0),
        ("document.createElement('tr');", [], 0),
        # Every other shape is reported, never skipped.
        ("tr('missing.key', fallback);", [], 1),
        ('tr("a.b", "v");', [], 1),
        ("tr(k, 'v');", [], 1),
        ("tr ('a.b', 'v');", [], 1),
        ("tr?.('a.b', 'v');", [], 1),
        ("var t = tr; t('a.b', 'v');", [], 1),
        ("tr.call(null, 'a.b', 'v');", [], 1),
        ("function tr(key, fallback) { return fallback; }", [], 0),
    ],
)
def test_the_scanner_reads_or_reports_every_shape(js, calls, unreadable):
    found, bad = _calls(_scan(js)[0])
    assert found == calls
    assert len(bad) == unreadable, bad


def test_fallback_escapes_decode_as_javascript_does():
    assert _unescape("'a\\u2014b' + \"\\x41\\'\\u{1F600}\"") == "a\u2014bA'\U0001F600"


def test_there_are_tr_calls_to_check():
    """Guard against the enumeration silently finding nothing."""
    files = _js_files_with_tr()
    assert len(files) >= 7, [p.name for p in files]
    assert sum(len(_calls(_code(p))[0]) for p in files) >= 80


def test_every_tr_call_is_in_a_shape_this_test_reads():
    """A call _CALL cannot parse would escape all three checks below."""
    unreadable = [f"{js.name}: {c}" for js in _js_files_with_tr() for c in _calls(_code(js))[1]]
    assert not unreadable, "tr() calls not of the form tr('key', 'literal'):\n" + "\n".join(unreadable)


def test_every_tr_key_is_in_the_blob_its_file_reads():
    _page_base, shell = _blobs((TEMPLATES / "base.html.j2").read_text(encoding="utf-8"))
    missing = []
    for js in _js_files_with_tr():
        code = _code(js)
        keys = {k for k, _lit in _calls(code)[0]}
        if "[data-litclock-shell-strings]" in code:
            missing += [f"{js.name}: {k} (shell blob)" for k in sorted(keys - shell)]
            continue
        pages = _templates_loading(js)
        assert pages, f"{js.name} reads a page blob but no template loads it"
        for tpl in pages:
            page, _ = _blobs(tpl.read_text(encoding="utf-8"))
            missing += [f"{js.name}: {k} (not injected by {tpl.name})" for k in sorted(keys - page)]
    assert not missing, "tr() keys the page never injects — the English literal would show permanently:\n" + "\n".join(
        missing
    )


def test_every_key_exists_in_the_catalog():
    unknown = sorted(
        f"{js.name}: {k}" for js in _js_files_with_tr() for k, _ in _calls(_code(js))[0] if k not in EN
    )
    assert not unknown, unknown


def test_every_fallback_literal_is_the_catalog_value():
    drift, unpinnable = [], []
    for js in _js_files_with_tr():
        for key, literal in _calls(_code(js))[0]:
            if not literal:
                unpinnable.append(f"{js.name}: {key}")
                continue
            if key in EN and _unescape(literal) != EN[key]:
                drift.append(f"{js.name}: {key}: {_unescape(literal)!r} != {EN[key]!r}")
    assert not drift, "stale-window fallbacks that no longer match the catalog:\n" + "\n".join(drift)
    assert not unpinnable, (
        "tr() calls whose fallback is not one plain literal, so it cannot be pinned:\n" + "\n".join(unpinnable)
    )
