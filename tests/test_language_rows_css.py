"""litclock-dev#532 D15/D17: the language controls are radio rows, not a
binary pill — and accessible ones. The rules live in segmented.css under
`.settings-segmented--rows`; these pin the D17 numbers so a restyle cannot
quietly drop them (the PWA has no visual regression suite)."""

from __future__ import annotations

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "src/control_server/static/css/segmented.css").read_text("utf-8")


def _rule(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert m, f"no rule for {selector}"
    return m.group(1)


def test_rows_stack_with_8px_between():
    rule = _rule(".settings-segmented--rows")
    assert "flex-direction: column" in rule and "gap: var(--sp-sm)" in rule


def test_each_row_is_a_44px_target_that_wraps_rather_than_clips():
    rule = _rule(".settings-segmented--rows .settings-segmented__opt")
    assert "min-height: 44px" in rule and "overflow-wrap: anywhere" in rule


def test_the_current_language_reads_by_shape_not_colour_alone():
    """The native radio is visible (a filled dot), and the selected row is
    not marked by a fill colour."""
    radio = _rule(".settings-segmented--rows .settings-segmented__input")
    assert "opacity: 1" in radio and "position: static" in radio and "pointer-events: auto" in radio
    selected = _rule(
        ".settings-segmented--rows .settings-segmented__opt.is-selected,\n"
        ".settings-segmented--rows .settings-segmented__opt:has(.settings-segmented__input:checked)"
    )
    assert "background: none" in selected


def test_the_focus_ring_is_the_2px_2px_outline():
    focus = _rule(".settings-segmented__opt:has(.settings-segmented__input:focus-visible)")
    assert "outline: 2px solid var(--accent)" in focus and "outline-offset: 2px" in focus


def test_weight_follows_the_radio_not_the_server_rendered_class():
    """/system never updates .is-selected; the starting language must not
    stay bold beside the newly picked one (DT7 review)."""
    cancel = _rule(
        ".settings-segmented--rows .settings-segmented__opt.is-selected,\n"
        ".settings-segmented--rows .settings-segmented__opt:has(.settings-segmented__input:checked)"
    )
    assert "font-weight" not in cancel
    checked = re.findall(
        re.escape(".settings-segmented--rows .settings-segmented__opt:has(.settings-segmented__input:checked)")
        + r"\s*\{([^}]*)\}",
        CSS,
    )
    assert any("font-weight: 600" in body for body in checked)
