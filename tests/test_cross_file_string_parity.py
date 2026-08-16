"""Cross-file parity for user-facing strings that exist in two places.

Several Control PWA strings are implemented twice — server-side in Jinja for the
SSR first paint, client-side in JS for the poll that replaces it. `diagnostics.js`
already carries the comment that the two "must never disagree". Nothing enforced
it, so the contract was a comment rather than a guard.

These tests make the duplication executable. They deliberately do NOT restructure
anything: giving the server one source of truth is exactly what the
litclock-dev#532 string catalogs will do, and building a bespoke mechanism now
means building it twice.

TECHNIQUE NOTE (from this file's own review). Two shapes of assertion were
rejected as unable to fail:

* Comparing SETS of sentences. Swapping which condition returns which sentence
  keeps both sets identical, so the SSR paint and the 30s poll could describe
  the same state differently and the test stayed green — verbatim the symptom
  the file claims to catch. The contract is a MAPPING, so the Jinja is now
  EXECUTED for each input and its output compared against the JS mapping.
* Comparing label lists with `in` (substring) and a loose `>= N` threshold.
  Renaming every template label to "App version (device)" kept a containment
  check at 26/26, and a `>= 5` threshold survived deleting 15 of the page's
  rows. Both sides declare `(label, key)` pairs, so they are compared as DICTS
  with no threshold at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "src" / "control_server" / "templates"
JS = REPO_ROOT / "src" / "control_server" / "static" / "js"

DIAG_TEMPLATE = TEMPLATES / "diagnostics.html.j2"
DIAG_JS = JS / "diagnostics.js"


class TestSettlingBannerParity:
    """The "your clock is finishing its first checks" banner.

    Rendered server-side for the first paint, then re-rendered client-side by
    _settlingBody() on every 30s poll. A user who loads the page and waits sees
    BOTH implementations within half a minute, so a drift shows up as the banner
    changing its wording by itself.
    """

    CASES = {
        frozenset({"network"}),
        frozenset({"time-location"}),
        frozenset({"network", "time-location"}),
        frozenset(),
    }

    @staticmethod
    def _jinja_mapping() -> dict[frozenset[str], str]:
        """EXECUTE the template's own expression for every input combination.

        Extracting the ternary by regex would re-derive the mapping from the
        sentence text, which is circular — the sentences are the thing under
        test. Rendering it means a swapped condition actually changes output.
        """
        source = DIAG_TEMPLATE.read_text()
        m = re.search(r"\{%\s*set _settling_body\s*=(.*?)%\}", source, re.DOTALL)
        assert m, "could not find the _settling_body expression — the template was restructured"
        expr = m.group(1).strip()

        env = jinja2.Environment(autoescape=False)  # noqa: S701 — rendering one literal, not user input
        tmpl = env.from_string("{% set _settling_body = " + expr + " %}{{ _settling_body }}")
        return {case: tmpl.render(_uncollected=list(case)) for case in TestSettlingBannerParity.CASES}

    @staticmethod
    def _js_mapping() -> dict[frozenset[str], str]:
        source = DIAG_JS.read_text()
        body = re.search(r"function _settlingBody\(uncollected\)\s*\{(.*?)\n  \}", source, re.DOTALL)
        assert body, "could not find _settlingBody() — diagnostics.js was restructured"
        pairs = re.findall(r"key === '([^']+)'\)\s*\{?\s*(?:return\s*)?\n?\s*(?:return\s*)?'([^']+)'", body.group(1))
        mapping = {frozenset(k.split("+")): v for k, v in pairs}
        # The empty case returns '' via the early-out on an empty list.
        mapping[frozenset()] = ""
        return mapping

    def test_the_mapping_agrees_not_just_the_sentence_set(self):
        jinja = self._jinja_mapping()
        js = self._js_mapping()

        assert len(js) == 4, f"expected 4 JS branches (3 states + empty), parsed {len(js)}: {js}"
        assert set(jinja) == self.CASES, f"Jinja did not render every case: {sorted(map(sorted, jinja))}"

        for case in sorted(self.CASES, key=lambda c: sorted(c)):
            assert jinja[case] == js[case], (
                f"SSR and the 30s poll describe {sorted(case) or 'the settled state'} differently, so the "
                "banner would change its wording on its own.\n"
                f"  diagnostics.html.j2: {jinja[case]!r}\n"
                f"  diagnostics.js:      {js[case]!r}"
            )

    def test_each_state_says_something_different(self):
        """If two states collapse to the same sentence the branch is pointless."""
        jinja = self._jinja_mapping()
        non_empty = [v for k, v in jinja.items() if k]
        assert len(set(non_empty)) == 3, f"the three settling states must read differently: {non_empty}"


class TestDiagnosticsCopyPayloadParity:
    """The "Copy support payload" clipboard text re-states the row labels the
    diagnostics page renders. Comparing a pasted payload against the page you
    are looking at is the entire point of that button, so both the labels AND
    which value each names have to agree."""

    @staticmethod
    def _pairs(text: str) -> dict[str, str]:
        """Both sides declare rows as (label, key) tuples — Jinja single-quoted,
        Python double-quoted. Captures the KEY too: a label/key swap (Lat
        showing the longitude) is invisible to a labels-only comparison."""
        found = re.findall(r"""\(\s*['"]([^'"]{2,40})['"]\s*,\s*['"]([a-z0-9_]+)['"]\s*\)""", text)
        return dict(found)

    # Rows the payload legitimately does not carry: the current-quote block is
    # rendered on the page but shipped in the payload's own quote section under
    # different keys. Listed explicitly so a NEW divergence fails instead of
    # being absorbed by a threshold.
    PAGE_ONLY = {"Author", "Picked at", "Quote", "Render tier", "Time", "Title"}
    PAYLOAD_ONLY = {"state"}

    def test_payload_and_page_agree_on_every_shared_row(self):
        payload = self._pairs((REPO_ROOT / "src/control_server/routes/diagnostics/_copy_payload.py").read_text())
        page = self._pairs(DIAG_TEMPLATE.read_text())

        assert len(payload) >= 25, f"parsed only {len(payload)} payload rows — the parser drifted, not the copy"
        assert len(page) >= 30, f"parsed only {len(page)} page rows — the parser drifted, not the copy"

        # Exact set comparison, not a threshold. A `>= N` bound let 8 page rows
        # be deleted while the test stayed green — the same can't-fail shape
        # this file exists to avoid.
        assert set(page) - set(payload) == self.PAGE_ONLY, (
            "the set of page rows the support payload omits changed. "
            f"newly missing from the payload: {sorted(set(page) - set(payload) - self.PAGE_ONLY)}"
            f" | no longer missing: {sorted(self.PAGE_ONLY - (set(page) - set(payload)))}"
        )
        assert set(payload) - set(page) == self.PAYLOAD_ONLY, (
            "the set of payload rows the page does not show changed. "
            f"newly unshown: {sorted(set(payload) - set(page) - self.PAYLOAD_ONLY)}"
        )

        mismatched = {
            label: (payload[label], page[label]) for label in set(payload) & set(page) if payload[label] != page[label]
        }
        assert not mismatched, f"the same label names a DIFFERENT value on each side: {mismatched}"


class TestUncollectedPlaceholderParity:
    """'Not yet collected' and its aria-label are duplicated verbatim between the
    template and diagnostics.js. The aria-label especially: drift there is
    invisible to sighted review, so only a test will ever catch it.

    The first version of this class checked `needle in file_text` for BOTH
    strings, which could not catch a drift in the visible pill label: the short
    needle is a substring of the long aria-label one, and the two live at
    different call sites. Renaming the pill label to 'Pending' on either side
    left the aria-label elsewhere in the same file still satisfying the short
    needle, and the suite stayed green — the very substring-membership shape
    this file's docstring says it rejected. Each string is now located at its
    OWN declaration.
    """

    ARIA = "Not yet collected — data has not been recorded on this clock yet"
    PILL = "Not yet collected"

    def test_the_js_pill_label_is_declared_verbatim(self):
        """Anchored on the assignment, not on membership anywhere in the file."""
        js = DIAG_JS.read_text()
        m = re.search(r"var PILL_LABEL_UNCOLLECTED\s*=\s*'([^']*)'", js)
        assert m, "PILL_LABEL_UNCOLLECTED assignment not found — diagnostics.js was restructured"
        assert m.group(1) == self.PILL, (
            f"the JS pill label is {m.group(1)!r}, the template renders {self.PILL!r} — they have drifted"
        )

    def test_the_template_pill_text_is_rendered_verbatim(self):
        """Anchored on the is_uncollected branch's rendered text, so an
        aria-label elsewhere in the template cannot satisfy it."""
        tmpl = DIAG_TEMPLATE.read_text()
        m = re.search(r"\{%\s*elif is_uncollected\s*%\}\s*([^<{]+)", tmpl)
        assert m, "the is_uncollected pill branch was not found — diagnostics.html.j2 was restructured"
        assert m.group(1).strip() == self.PILL, (
            f"the template pill renders {m.group(1).strip()!r}, the JS declares {self.PILL!r}"
        )

    def test_the_aria_label_matches_on_both_sides(self):
        """This one IS legitimately a membership check: the aria-label is a
        long, unique sentence that appears nowhere else."""
        for path in (DIAG_TEMPLATE, DIAG_JS):
            assert self.ARIA in path.read_text(), f"{self.ARIA!r} vanished from {path.name}"


class TestBannerTitleParity:
    """The status banner's TITLE, the same SSR-vs-poll duplication as the body.

    diagnostics.html.j2 renders one of four titles by severity; diagnostics.js's
    bannerTitle() hardcodes the same four for the same four severities. Nothing
    compared them: the JS test asserts bannerTitle() against itself, and the
    Python test asserts the template's string in isolation. A title changed on
    one side only would ship silently — one level up from the body drift
    TestSettlingBannerParity already covers.
    """

    SEVERITIES = ("error", "warning", "settling", "ok")

    @staticmethod
    def _jinja_mapping() -> dict[str, str]:
        source = DIAG_TEMPLATE.read_text()
        m = re.search(
            r"data-diag-banner-title>\s*(\{%\s*if _severity.*?\{%\s*endif\s*%\})",
            source,
            re.DOTALL,
        )
        assert m, "the banner-title branch was not found — diagnostics.html.j2 was restructured"
        env = jinja2.Environment(autoescape=False)  # noqa: S701 — rendering one literal, not user input
        tmpl = env.from_string(m.group(1))
        return {sev: tmpl.render(_severity=sev).strip() for sev in TestBannerTitleParity.SEVERITIES}

    @staticmethod
    def _js_mapping() -> dict[str, str]:
        js = DIAG_JS.read_text()
        m = re.search(r"function bannerTitle\(severity\)\s*\{(.*?)\n  \}", js, re.DOTALL)
        assert m, "bannerTitle() not found — diagnostics.js was restructured"
        body = m.group(1)
        mapping = {}
        for sev, text in re.findall(r"severity === '([a-z]+)'\)\s*return\s*(\"[^\"]*\"|'[^']*')", body):
            mapping[sev] = text[1:-1]
        fallback = re.search(r"\n\s*return\s*(\"[^\"]*\"|'[^']*');\s*$", body.rstrip())
        assert fallback, "bannerTitle() has no default return — the ok case is unmapped"
        mapping["ok"] = fallback.group(1)[1:-1]
        return mapping

    def test_the_titles_agree_per_severity(self):
        jinja = self._jinja_mapping()
        js = self._js_mapping()
        assert set(js) == set(self.SEVERITIES), f"parsed JS severities {sorted(js)} != {sorted(self.SEVERITIES)}"
        for sev in self.SEVERITIES:
            assert jinja[sev] == js[sev], (
                f"SSR and the 30s poll title the {sev!r} state differently, so the banner heading would "
                "change on its own.\n"
                f"  diagnostics.html.j2: {jinja[sev]!r}\n"
                f"  diagnostics.js:      {js[sev]!r}"
            )

    def test_each_severity_has_its_own_title(self):
        titles = self._jinja_mapping()
        assert len(set(titles.values())) == len(self.SEVERITIES), f"two severities share a title: {titles}"
