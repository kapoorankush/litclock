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

    litclock-dev#532 converted this pair from two hand-written copies to ONE
    source: the language catalog. The Jinja side calls t(); the JS side reads
    the injected blob with baked-in English FALLBACK literals for SW-cached
    HTML that predates the blob. This class is therefore one-sided now: both
    surfaces are pinned AGAINST THE CATALOG, and the blob's key list is
    pinned to cover what the JS reads."""

    KEYS = {
        frozenset({"network"}): "diag.settling.network",
        frozenset({"time-location"}): "diag.settling.time_location",
        frozenset({"network", "time-location"}): "diag.settling.both",
    }

    @staticmethod
    def _catalog_value(key: str) -> str:
        import os as _os
        import sys as _sys

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import strings_catalog  # noqa: PLC0415

        # Pin the language (/review litclock-dev#739 F5): the JS fallbacks are English
        # by definition, so this class compares against the English catalog
        # regardless of the dev box's ambient LITCLOCK_LANGUAGE/env.sh.
        _os.environ["LITCLOCK_LANGUAGE"] = "en"
        strings_catalog.reset_cache()
        try:
            value = strings_catalog.get(key)
        finally:
            _os.environ.pop("LITCLOCK_LANGUAGE", None)
            strings_catalog.reset_cache()
        assert value != key, f"catalog key {key!r} unresolved — the source of truth is missing it"
        return value

    def _jinja_mapping(self) -> dict[frozenset[str], str]:
        """EXECUTE the template's expression with the real catalog behind t()."""
        source = DIAG_TEMPLATE.read_text()
        m = re.search(r"\{%\s*set _settling_body\s*=(.*?)%\}", source, re.DOTALL)
        assert m, "could not find the _settling_body expression — the template was restructured"
        expr = m.group(1).strip()
        env = jinja2.Environment(autoescape=False)  # noqa: S701 — rendering one literal, not user input
        env.globals["t"] = self._catalog_value
        tmpl = env.from_string("{% set _settling_body = " + expr + " %}{{ _settling_body }}")
        cases = set(self.KEYS) | {frozenset()}
        return {case: tmpl.render(_uncollected=list(case)) for case in cases}

    def _js_fallbacks(self) -> dict[str, str]:
        source = DIAG_JS.read_text()
        body = re.search(r"function _settlingBody\(uncollected\)\s*\{(.*?)\n  \}", source, re.DOTALL)
        assert body, "could not find _settlingBody() — diagnostics.js was restructured"
        pairs = re.findall(r"tr\('([^']+)',\s*'([^']+)'\)", body.group(1))
        assert pairs, "the settling body no longer routes through tr() — the pair is duplicated again"
        return dict(pairs)

    def test_jinja_expression_routes_through_t(self):
        # /review litclock-dev#739 F4c: the JS side asserts tr() routing; without this,
        # reverting the template to hardcoded English passes everything
        # until the day the catalog text changes.
        source = DIAG_TEMPLATE.read_text()
        m = re.search(r"\{%\s*set _settling_body\s*=(.*?)%\}", source, re.DOTALL)
        assert m
        assert m.group(1).count("t('diag.settling.") == 3, (
            "the settling expression no longer resolves all three states via t() — "
            "a hardcoded sentence is a second source (litclock-dev#532)"
        )

    def test_jinja_states_resolve_to_catalog_sentences(self):
        jinja = self._jinja_mapping()
        for case, key in self.KEYS.items():
            assert jinja[case] == self._catalog_value(key), (
                f"SSR renders {sorted(case)} from somewhere other than the catalog"
            )
        assert jinja[frozenset()] == ""

    def test_js_fallback_literals_match_the_catalog(self):
        # The one-sided pin: the SW-cache fallbacks are COPIES verified
        # against the source of truth, not an independent second source.
        fallbacks = self._js_fallbacks()
        assert set(fallbacks) == set(self.KEYS.values()), (
            f"JS reads keys {sorted(fallbacks)} but the state map expects {sorted(self.KEYS.values())}"
        )
        for key, literal in fallbacks.items():
            assert literal == self._catalog_value(key), (
                f"diagnostics.js fallback for {key!r} drifted from the catalog:\n"
                f"  catalog: {self._catalog_value(key)!r}\n  js:      {literal!r}"
            )

    def test_injected_blob_covers_the_js_keys(self):
        source = DIAG_TEMPLATE.read_text()
        m = re.search(r"data-litclock-strings>\{\{ catalog_subset\(\[(.*?)\]\)", source, re.DOTALL)
        assert m, "the diagnostics page no longer injects its strings blob"
        blob_keys = set(re.findall(r"'([^']+)'", m.group(1)))
        assert set(self.KEYS.values()) <= blob_keys, (
            f"blob missing keys the JS reads: {sorted(set(self.KEYS.values()) - blob_keys)}"
        )

    def test_each_state_says_something_different(self):
        """If two states collapse to the same sentence the branch is pointless."""
        values = [self._catalog_value(k) for k in self.KEYS.values()]
        assert len(set(values)) == 3, f"the three settling states must read differently: {values}"


class TestDiagnosticsCopyPayloadParity:
    """The "Copy support payload" row labels — one-sided catalog form
    (litclock-dev#532 PR 4c). Both sides now resolve labels from
    `diag.row.<value_key>`, so agreement is by construction; what remains
    to pin is (a) the catalog key MATCHES the value key at every site (a
    label/key swap now looks like t('diag.row.lat') paired with 'lon'),
    (b) the page/payload key sets differ exactly as documented, and (c)
    every referenced catalog key resolves."""

    PAGE_ONLY = {"author", "picked_at", "quote", "render_mode", "time", "title"}
    # Empty by construction now: the old {"state"} entry was never a display
    # row — it was a dict.get("state", "unknown") the first conversion regex
    # over-matched (caught in-round; the run-the-tool-on-the-real-repo class).
    PAYLOAD_ONLY: set[str] = set()

    _catalog_value = staticmethod(TestSettlingBannerParity._catalog_value)

    @staticmethod
    def _sites(text: str) -> dict[str, str]:
        """value_key → catalog_key for every converted tuple."""
        found = re.findall(
            r"""\(\s*_?t\(\s*['"]diag\.row\.([a-z0-9_]+)['"]\s*\)\s*,\s*['"]([a-z0-9_]+)['"]\s*\)""",
            text,
        )
        return {value_key: f"diag.row.{cat_suffix}" for cat_suffix, value_key in found}

    # EXACT counts, not floors (/review litclock-dev#741 F1: a >=N floor let up to five
    # shared rows revert to two-source literals with the suite green — the
    # precise shape this file's TECHNIQUE NOTE rejected). A genuinely added
    # or removed row updates these two numbers in the same diff.
    PAGE_ROWS = 36
    PAYLOAD_ROWS = 30

    def _page(self) -> dict[str, str]:
        sites = self._sites(DIAG_TEMPLATE.read_text())
        assert len(sites) == self.PAGE_ROWS, (
            f"parsed {len(sites)} converted page rows, expected {self.PAGE_ROWS} — "
            "a row was added/removed/reverted without updating the pin"
        )
        return sites

    def _payload(self) -> dict[str, str]:
        sites = self._sites((REPO_ROOT / "src/control_server/routes/diagnostics/_copy_payload.py").read_text())
        assert len(sites) == self.PAYLOAD_ROWS, (
            f"parsed {len(sites)} converted payload rows, expected {self.PAYLOAD_ROWS}"
        )
        return sites

    def test_no_unconverted_literal_rows_remain_on_the_page(self):
        # The template's rows=[...] blocks must contain no hand-written
        # (label, key) tuple — the payload side can't use this check
        # (dict.get false-positives), which is why its count is exact.
        tmpl = DIAG_TEMPLATE.read_text()
        literal_rows = re.findall(
            r"""\(\s*['"][^'"]{2,40}['"]\s*,\s*['"][a-z0-9_]+['"]\s*\)""", tmpl
        )
        assert not literal_rows, (
            f"unconverted literal row tuples on the page: {literal_rows[:3]} — "
            "route them through t('diag.row.*')"
        )

    def test_catalog_key_matches_value_key_at_every_site(self):
        for name, sites in (("page", self._page()), ("payload", self._payload())):
            for value_key, catalog_key in sites.items():
                assert catalog_key == f"diag.row.{value_key}", (
                    f"{name} row {value_key!r} resolves its label from {catalog_key!r} — "
                    "a label/key swap (Lat showing the longitude)"
                )

    def test_shared_row_sets_differ_exactly_as_documented(self):
        page, payload = set(self._page()), set(self._payload())
        assert page - payload == self.PAGE_ONLY, (
            f"newly missing from the payload: {sorted(page - payload - self.PAGE_ONLY)} | "
            f"no longer missing: {sorted(self.PAGE_ONLY - (page - payload))}"
        )
        assert payload - page == self.PAYLOAD_ONLY, (
            f"newly unshown on the page: {sorted(payload - page - self.PAYLOAD_ONLY)}"
        )

    def test_every_row_key_resolves_in_the_catalog(self):
        for value_key in set(self._page()) | set(self._payload()):
            value = self._catalog_value(f"diag.row.{value_key}")
            assert value.strip(), f"diag.row.{value_key} resolves to an empty label"


class TestUncollectedPlaceholderParity:
    """'Not yet collected' + its aria-label — one-sided catalog form
    (litclock-dev#532 PR 4b). The aria pair is the first aria.* key (the
    reserved class earns real enforcement only when a second language's
    canonical diff runs — /review litclock-dev#740 F5)."""

    KEYS = {
        "pill": "diag.pill.uncollected",
        "aria": "aria.diag.pill.uncollected",
    }

    _catalog_value = staticmethod(TestSettlingBannerParity._catalog_value)

    def test_template_sites_route_through_t(self):
        tmpl = DIAG_TEMPLATE.read_text()
        m = re.search(r"\{%\s*elif is_uncollected\s*%\}\{\{ t\('([^']+)'\) \}\}", tmpl)
        assert m and m.group(1) == self.KEYS["pill"], (
            "the pill branch no longer routes through t() — a hardcoded label "
            "is a second source"
        )
        # Anchor on the SPECIFIC key — first-match broke the moment the
        # template gained OTHER t()-routed aria-labels (slice 9).
        assert f"aria-label=\"{{{{ t('{self.KEYS['aria']}') }}}}\"" in tmpl, (
            "the uncollected-pill aria-label no longer routes through t()"
        )

    def test_js_fallbacks_match_the_catalog(self):
        js = DIAG_JS.read_text()
        m = re.search(r"var PILL_LABEL_UNCOLLECTED = tr\('([^']+)',\s*'([^']*)'\)", js)
        assert m, "PILL_LABEL_UNCOLLECTED no longer routes through tr()"
        assert m.group(1) == self.KEYS["pill"]
        assert m.group(2) == self._catalog_value(self.KEYS["pill"]), (
            "the JS pill fallback drifted from the catalog"
        )
        m = re.search(r"tr\('aria\.diag\.pill\.uncollected',\s*'([^']*)'\)", js)
        assert m, "the aria-label no longer routes through tr()"
        assert m.group(1) == self._catalog_value(self.KEYS["aria"]), (
            "the JS aria fallback drifted from the catalog — invisible to "
            "sighted review, only this test will ever catch it"
        )

    def test_blob_covers_both_keys(self):
        source = DIAG_TEMPLATE.read_text()
        m = re.search(r"data-litclock-strings>\{\{ catalog_subset\(\[(.*?)\]\)", source, re.DOTALL)
        assert m
        blob_keys = set(re.findall(r"'([^']+)'", m.group(1)))
        assert set(self.KEYS.values()) <= blob_keys

    def test_pill_is_a_prefix_of_aria_but_not_equal(self):
        # The original drift trap: the short label is a substring of the
        # long aria sentence. Keep them related but distinct.
        pill = self._catalog_value(self.KEYS["pill"])
        aria = self._catalog_value(self.KEYS["aria"])
        assert aria.startswith(pill) and aria != pill


class TestBannerTitleParity:
    """The status banner's TITLE — converted to the one-sided catalog form
    (litclock-dev#532 PR 4b): SSR branch, JS fallbacks, and blob coverage
    are each pinned AGAINST the catalog."""

    KEYS = {
        "error": "diag.banner.title.error",
        "warning": "diag.banner.title.warning",
        "settling": "diag.banner.title.settling",
        "ok": "diag.banner.title.ok",
    }

    _catalog_value = staticmethod(TestSettlingBannerParity._catalog_value)

    def _jinja_mapping(self) -> dict[str, str]:
        source = DIAG_TEMPLATE.read_text()
        m = re.search(
            r"data-diag-banner-title>\s*(\{%\s*if _severity.*?\{%\s*endif\s*%\})",
            source,
            re.DOTALL,
        )
        assert m, "the banner-title branch was not found — diagnostics.html.j2 was restructured"
        env = jinja2.Environment(autoescape=False)  # noqa: S701 — rendering one literal, not user input
        env.globals["t"] = self._catalog_value
        tmpl = env.from_string(m.group(1))
        return {sev: tmpl.render(_severity=sev).strip() for sev in self.KEYS}

    def _js_fallbacks(self) -> dict[str, str]:
        js = DIAG_JS.read_text()
        m = re.search(r"function bannerTitle\(severity\)\s*\{(.*?)\n  \}", js, re.DOTALL)
        assert m, "bannerTitle() not found — diagnostics.js was restructured"
        pairs = re.findall(r"tr\('([^']+)',\s*(\"[^\"]*\"|'[^']*')\)", m.group(1))
        assert len(pairs) == 4, (
            f"bannerTitle() must route all four severities through tr(): parsed {len(pairs)}"
        )
        return {k: v[1:-1] for k, v in pairs}

    def test_jinja_titles_resolve_to_catalog(self):
        jinja = self._jinja_mapping()
        for sev, key in self.KEYS.items():
            assert jinja[sev] == self._catalog_value(key), (
                f"SSR renders the {sev} title from somewhere other than the catalog"
            )

    def test_js_fallbacks_match_the_catalog(self):
        fallbacks = self._js_fallbacks()
        assert set(fallbacks) == set(self.KEYS.values())
        for key, literal in fallbacks.items():
            assert literal == self._catalog_value(key), (
                f"diagnostics.js title fallback for {key!r} drifted from the catalog"
            )

    def test_blob_covers_the_title_keys(self):
        source = DIAG_TEMPLATE.read_text()
        m = re.search(r"data-litclock-strings>\{\{ catalog_subset\(\[(.*?)\]\)", source, re.DOTALL)
        assert m
        blob_keys = set(re.findall(r"'([^']+)'", m.group(1)))
        assert set(self.KEYS.values()) <= blob_keys, (
            f"blob missing title keys: {sorted(set(self.KEYS.values()) - blob_keys)}"
        )

    def test_each_severity_has_its_own_title(self):
        values = [self._catalog_value(k) for k in self.KEYS.values()]
        assert len(set(values)) == 4, f"the four severities must read differently: {values}"


class TestNoInCodeGrammarAssembly:
    """litclock-dev#532 (scope-audit item 11): user-visible sentences are
    whole templates with named slots. The suffix-splice idiom
    (f"...{'s' if n != 1 else ''}...") assembles grammar in code, which a
    translator cannot translate — pinned at zero occurrences in src/.

    Honest scope (/review litclock-dev#736 F3): this guards the PYTHON spellings of the
    idiom — direct, reversed, and the 'es' variants, all currently at zero.
    It does NOT and cannot guard the JS splice class: `' + x + '` is the
    page-assembly idiom throughout the PWA scripts, so any grep either
    misses sentences or drowns in markup. JS regressions are held instead
    by the rendered-output pins in tests/js/ (drawer hidden-batch, status
    stale banner, diagnostics Refreshed line)."""

    # Both quote styles of each spelling. The reversed form ('' if n == 1
    # else 's') and multiplicative form ("s" * (n != 1)) evade a naive
    # "'s' if" grep — all verified at zero before pinning.
    _IDIOMS = (
        "'s' if",
        '"s" if',
        "'es' if",
        '"es" if',
        "'' if",
        '"" if',
        "'s' * (",
        '"s" * (',
    )

    def test_no_plural_suffix_splicing_in_src(self):
        offenders = []
        for path in (REPO_ROOT / "src").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if any(idiom in line for idiom in self._IDIOMS):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()[:80]}")
        assert not offenders, (
            "in-code plural assembly is back — use whole-sentence templates "
            f"with named slots (litclock-dev#532 item 11): {offenders}"
        )


class TestRenderedBlobCarriesResolvedValues:
    """/review litclock-dev#739 F4a (both passes): the template-source checks alone let a
    broken catalog_subset ship green — a keys-as-values blob repaints the
    banner with the literal string diag.settling.both 30 seconds after page
    load. This renders the REAL route and asserts the parsed blob."""

    def test_diagnostics_blob_resolves_to_english_sentences(self, monkeypatch):
        import json
        import sys as _sys

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import strings_catalog  # noqa: PLC0415
        from control_server import create_app  # noqa: PLC0415

        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        strings_catalog.reset_cache()
        app = create_app(test_config={"VERSION_OVERRIDE": "v0.0.0-test"})
        app.config["TESTING"] = True
        page = app.test_client().get("/diagnostics").data.decode()
        m = re.search(r'data-litclock-strings>(.*?)</script>', page, re.DOTALL)
        assert m, "the rendered page carries no strings blob"
        blob = json.loads(m.group(1))
        assert blob["diag.settling.both"] == (
            "Your clock is finishing its first network and location checks."
        ), f"the blob serves something other than the resolved sentence: {blob}"
        assert blob["diag.settling.network"] == "Your clock is finishing its first network check."
        assert blob["diag.settling.time_location"] == "Your clock is finishing its first location check."
        for key, value in blob.items():
            assert value != key, f"blob degraded {key!r} to a raw key at render time"


class TestStaleBannerParity:
    """The Status tab's paused banner — the FIFTH pair, SSR (status.html.j2)
    vs the 30s poll (status.js). Never pinned before litclock-dev#532 PR 4b;
    converted straight to the one-sided catalog form."""

    KEYS = ("status.stale.with_age", "status.stale.no_quote")
    STATUS_TEMPLATE = TEMPLATES / "status.html.j2"
    STATUS_JS = JS / "status.js"

    _catalog_value = staticmethod(TestSettlingBannerParity._catalog_value)

    def test_template_routes_both_arms_through_t(self):
        tmpl = self.STATUS_TEMPLATE.read_text()
        assert "t('status.stale.with_age', n=" in tmpl, (
            "the aged arm no longer routes through t() with the n slot"
        )
        assert "t('status.stale.no_quote')" in tmpl

    def test_js_fallbacks_match_the_catalog(self):
        js = self.STATUS_JS.read_text()
        for key in self.KEYS:
            m = re.search(r"tr\('" + re.escape(key) + r"',\s*'([^']*)'\)", js)
            assert m, f"status.js no longer routes {key} through tr()"
            assert m.group(1) == self._catalog_value(key), (
                f"status.js fallback for {key} drifted from the catalog"
            )

    def test_blob_covers_the_keys(self):
        tmpl = self.STATUS_TEMPLATE.read_text()
        m = re.search(r"data-litclock-strings>\{\{ catalog_subset\(\[(.*?)\]\)", tmpl, re.DOTALL)
        assert m, "status.html.j2 no longer injects its strings blob"
        blob_keys = set(re.findall(r"'([^']+)'", m.group(1)))
        assert set(self.KEYS) <= blob_keys

    def test_rendered_status_blob_resolves(self, monkeypatch):
        import json
        import sys as _sys

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import strings_catalog  # noqa: PLC0415
        from control_server import create_app  # noqa: PLC0415

        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        strings_catalog.reset_cache()
        app = create_app(test_config={"VERSION_OVERRIDE": "v0.0.0-test"})
        app.config["TESTING"] = True
        page = app.test_client().get("/").data.decode()
        m = re.search(r"data-litclock-strings>(.*?)</script>", page, re.DOTALL)
        assert m, "the rendered status page carries no strings blob"
        blob = json.loads(m.group(1))
        assert blob["status.stale.with_age"] == (
            "Clock service may be paused — last quote {n} min ago"
        ), f"blob degraded or drifted: {blob}"
        assert blob["status.stale.no_quote"] == "Clock service may be paused — no quote published yet"


class TestBlobMechanismInvariants:
    """/review litclock-dev#740 F3+F4: hazards of the mechanism itself.

    F3: both JS readers take the FIRST [data-litclock-strings] in document
    order, and base.html.j2 loads shared scripts before extra_body — a
    future base-level blob would silently shadow every page's own. Pin:
    the base template and its includes carry no blob; each page carries at
    most one.

    F4: the STRINGS/tr accessor is a deliberate per-IIFE duplicate (a
    shared file would add the load-order + SW-precache coupling PR 4a's
    review killed). Deliberate duplication still needs a lockstep pin.
    """

    def test_base_and_includes_carry_no_page_blob(self):
        """Pins the PAGE-blob attribute in its markup form (attribute name
        followed by `>` — a prose mention in a comment must not satisfy or
        falsify this). The slice-8 SHELL blob rides its own prefix-free
        attribute and is pinned separately below."""
        for name in ("base.html.j2", "_handoff-banner.html.j2"):
            path = TEMPLATES / name
            if path.exists():
                assert "data-litclock-strings>" not in path.read_text(), (
                    f"{name} grew a PAGE strings blob — it would shadow every page's own "
                    "(first-match querySelector semantics)"
                )

    def test_base_carries_exactly_one_shell_blob(self):
        """drawer.js + handoff.js run on EVERY page and read
        [data-litclock-shell-strings] — exactly one, in base, never in a
        page template (a page copy would shadow it for those readers)."""
        base = (TEMPLATES / "base.html.j2").read_text()
        assert base.count("data-litclock-shell-strings>") == 1
        for path in TEMPLATES.glob("*.j2"):
            if path.name in ("base.html.j2", "sw.js.j2"):
                continue
            assert "data-litclock-shell-strings" not in path.read_text(), path.name

    def test_each_page_carries_at_most_one_blob(self):
        for path in TEMPLATES.glob("*.j2"):
            count = path.read_text().count("data-litclock-strings>")
            assert count <= 1, f"{path.name} carries {count} blobs — first-match shadows the rest"

    def test_accessor_copies_are_in_lockstep(self):
        def _accessor(path):
            text = path.read_text()
            i = text.index("  var STRINGS = (function () {")
            j = text.index("\n  }\n", text.index("function tr(key, fallback)", i)) + 4
            return text[i:j]

        diag = _accessor(DIAG_JS)
        status = _accessor(JS / "status.js")
        updates = _accessor(JS / "updates.js")
        system = _accessor(JS / "system.js")
        settings = _accessor(JS / "settings.js")
        assert diag == status == updates == system == settings, (
            "the STRINGS/tr accessor copies drifted — keep them byte-identical "
            "(per-file copies are the RECORDED decision: a shared strings.js "
            "would add a load-order + stale-cache availability dependency; "
            "revisit only if the copy count gets silly — litclock-dev#532)"
        )


class TestSetupServerCopyThroughCatalog:
    """litclock-dev#532 PR 4c: setup_server's user-visible copy constants
    resolve from the catalog (the Python→JS json.dumps thread already made
    them single-source within the file; this pins the catalog routing and
    that the POST sentinel stays a literal)."""

    SETUP = REPO_ROOT / "src" / "setup_server.py"

    def test_visible_copy_resolves_per_request_through_the_catalog(self):
        # litclock-dev#532 follow-up (Stage-4 gates /review): the picker's
        # manual-option and placeholder copy resolved at MODULE IMPORT,
        # freezing the language for the retry re-render. Pin the request-time
        # accessor form AND that no module-level assignment re-freezes them.
        src = self.SETUP.read_text()
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        for func, key in (
            ("_manual_ssid_text", "setup.wifi.manual_option"),
            ("_wifi_placeholder_text", "setup.wifi.placeholder"),
            ("_wifi_placeholder_empty_text", "setup.wifi.placeholder_empty"),
        ):
            assert f'def {func}():\n    return _catalog_get("{key}")' in code, (
                f"{func} no longer resolves {key} from the catalog at request time"
            )
        # AST guard, not a line scan (/review: a column-0 filter missed
        # indented module-level freezes — if/try blocks, continuation lines —
        # and any indirection like X = _manual_ssid_text()). Walk every
        # statement that EXECUTES at import (module body incl. nested
        # if/try/with/for, class bodies; not function bodies) and reject any
        # assignment whose value calls into the catalog or the accessors.
        import ast

        frozen_calls = {
            "_catalog_get",
            "_manual_ssid_text",
            "_wifi_placeholder_text",
            "_wifi_placeholder_empty_text",
        }
        hits = []

        def _walk_import_reachable(body):
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call):
                            fn = sub.func
                            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                            aliased_get = (
                                name == "get"
                                and isinstance(fn, ast.Attribute)
                                and isinstance(fn.value, ast.Name)
                                and fn.value.id == "strings_catalog"
                            )
                            if name in frozen_calls or aliased_get:
                                hits.append(node.lineno)
                for attr in ("body", "orelse", "finalbody", "handlers"):
                    child = getattr(node, attr, None)
                    if child:
                        _walk_import_reachable(
                            [h for h in child] if attr != "handlers" else [b for h in child for b in h.body]
                        )

        _walk_import_reachable(ast.parse(self.SETUP.read_text()).body)
        assert not hits, (
            "import-reachable assignment(s) resolve catalog copy at module load "
            f"(lines {hits}) — that froze the language for retry re-renders once "
            "already (litclock-dev#532 Stage-4 /review)"
        )

    def test_setup_keys_resolve_in_the_catalog(self):
        # /review litclock-dev#741 F2: source pins alone let a deleted catalog key ship —
        # the hotspot picker would show 'setup.wifi.placeholder' and the
        # setup_server tests, which compare HTML against the module constant
        # itself, would stay self-referentially green.
        for key in ("setup.wifi.manual_option", "setup.wifi.placeholder", "setup.wifi.placeholder_empty"):
            value = TestSettlingBannerParity._catalog_value(key)
            assert value.strip() and value != key

    def test_post_sentinel_stays_a_literal(self):
        # "__litclock_type_it_myself__" is a VALUE marker compared against
        # POSTs — translating it would break form handling.
        src = self.SETUP.read_text()
        assert 'MANUAL_SSID_VALUE = "__litclock_type_it_myself__"' in src


class TestGiftMessageErrorTemplate:
    """The one fragment-composition site (scope-audit item 11 tail): the
    envelope sentence is a whole template with a named slot; the validator
    fragment joins the catalog with the bulk extraction."""

    _catalog_value = staticmethod(TestSettlingBannerParity._catalog_value)

    def test_route_fills_the_catalog_template(self):
        src = (REPO_ROOT / "src" / "control_server" / "routes" / "system.py").read_text()
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        assert 'strings_catalog.get("system.gift_message.error", error=validation_error)' in code, (
            "the gift-message envelope no longer routes through the catalog template"
        )
        assert 'f"Message {validation_error}."' not in code, (
            "the inline fragment composition is back"
        )

    def test_template_shape(self):
        value = self._catalog_value("system.gift_message.error")
        assert "{error}" in value and value.endswith("."), value


class TestRenderedRowLabelsCanary:
    """/review litclock-dev#741 (both passes): source pins + catalog resolution cover a
    missing key, but only a rendered check catches a broken t() at the
    route. One canary per surface: no raw diag.row.* key may reach the
    rendered page or the built payload, and a representative label must."""

    def _app(self, monkeypatch):
        import sys as _sys

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import strings_catalog  # noqa: PLC0415
        from control_server import create_app  # noqa: PLC0415

        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        strings_catalog.reset_cache()
        app = create_app(test_config={"VERSION_OVERRIDE": "v0.0.0-test"})
        app.config["TESTING"] = True
        return app

    def test_rendered_page_carries_labels_not_keys(self, monkeypatch):
        page = self._app(monkeypatch).test_client().get("/diagnostics").data.decode()
        assert "CPU temp °C" in page, "a representative rendered row label vanished"
        assert "diag.row." not in page.replace("data-litclock-strings", ""), (
            "a raw catalog key reached the rendered diagnostics page"
        )

    def test_built_payload_carries_labels_not_keys(self, monkeypatch):
        import sys as _sys

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        from control_server.routes.diagnostics._copy_payload import build_copy_payload  # noqa: PLC0415

        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        payload = build_copy_payload({"cpu_temp_c": 51.2, "weather_units": "imperial"})
        assert "CPU temp °C" in payload
        assert "diag.row." not in payload, "a raw catalog key reached the support payload"


class TestStatusPageSsrCatalog:
    """litclock-dev#532 bulk extraction slice 3: every static SSR string on
    the Status page routes through t(). One-sided catalog form: rendered
    canaries at route level (source pins alone let a broken t() ship —
    proven three times in this file's history)."""

    STATIC_KEYS = {
        "status.heading": "Now",
        "status.phase3_skip": "Last update skipped the env-vars merge — will retry next Sunday.",
        "status.empty.lead": "Starting up…",
        "status.empty.body": "Your first quote should appear within a minute.",
        "status.unreachable.headline": "Couldn't reach LitClock.",
        "status.unreachable.help": "A few things that often help:",
        "status.unreachable.tip_power": "Make sure your clock is plugged in and the screen is lit",
        "status.unreachable.tip_qr": "Try scanning the QR code on the clock again",
        "status.unreachable.tip_wifi": "Check that your phone is on the same WiFi as the clock",
        "status.unreachable.retry": "Tap to retry",
        "aria.status.rows": "System status",
        "status.rows.wifi": "WiFi",
        "status.rows.weather": "Weather",
        "status.rows.version": "Version",
        "status.rows.uptime": "Uptime",
        "status.rows.last_update": "Last update",
        "status.mdns.headline": "A more reliable link is available.",
        "status.mdns.body": "Helps your clock keep working even when WiFi changes.",
        "status.mdns.switch": "Switch",
        "status.mdns.dismiss": "Not now",
        "aria.status.mdns": "More reliable connection available",
    }

    def _page(self, monkeypatch):
        import sys as _sys

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import strings_catalog  # noqa: PLC0415
        from control_server import create_app  # noqa: PLC0415

        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        strings_catalog.reset_cache()
        app = create_app(test_config={"VERSION_OVERRIDE": "v0.0.0-test"})
        app.config["TESTING"] = True
        return app.test_client().get("/").data.decode()

    def test_catalog_carries_every_status_page_key(self):
        import json as _json

        catalog = _json.loads(
            (REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8")
        )
        for key, value in self.STATIC_KEYS.items():
            assert catalog.get(key) == value, key

    def test_rendered_page_carries_the_values_not_the_keys(self, monkeypatch):
        from markupsafe import escape

        page = self._page(monkeypatch)
        # Every value renders (the [hidden] blocks are still in the DOM).
        # Compare through Jinja's OWN escaping — apostrophes render as
        # &#39;, so a raw substring check would miss them.
        for key, value in self.STATIC_KEYS.items():
            assert str(escape(value)) in page, f"{key} value missing from the rendered page"
        # ...and no raw dotted key leaks (a broken t() serves the key).
        # Element-content keys would leak as >key<; aria keys live in
        # attributes and would leak as ="key" (Codex slice-3 /review: the
        # first draft exempted aria.* entirely — a vacuous guard).
        blobless = re.sub(r"data-litclock-strings>.*?</script>", "", page, flags=re.DOTALL)
        for key in self.STATIC_KEYS:
            assert f">{key}<" not in blobless, f"{key} leaked as element content"
            assert f'="{key}"' not in blobless, f"{key} leaked as an attribute value"

    def test_template_routes_the_static_strings_through_t(self):
        body = (REPO_ROOT / "src" / "control_server" / "templates" / "status.html.j2").read_text(
            encoding="utf-8"
        )
        for key in self.STATIC_KEYS:
            assert f"t('{key}')" in body, f"{key} not routed through t() in the template"
        # status.heading has TWO deliberate sites (tab-title fallback + the
        # visually-hidden h2) — either could silently revert alone.
        assert body.count("t('status.heading')") == 2


class TestUpdatesPageCatalog:
    """litclock-dev#532 bulk extraction slice 4: the Updates page — SSR
    strings via t(), updates.js dynamic copy via the blob + tr() with
    CI-pinned English fallbacks."""

    SSR_KEYS = {
        "updates.title": "Updates",
        "updates.pill.available": "update available",
        "updates.pill.checking": "checking…",
        "updates.pill.check_failed": "couldn't check",
        "updates.pill.up_to_date": "up to date",
        "updates.facts.current": "Current version",
        "updates.facts.available": "Available",
        "updates.facts.last_checked": "Last checked",
        "updates.apply": "Apply update…",
        "updates.confirm.title": "Apply update?",
        "updates.confirm.body": (
            "LitClock will pause for about 5 minutes while it updates. Your quote will return when it’s done."
        ),
        # Cancel unified onto common.cancel in slice 6 (shared by every
        # confirm sheet on /system and /updates).
        "common.cancel": "Cancel",
        "updates.confirm.confirm": "Apply",
        "updates.reading.title": "Updating LitClock",
        "updates.phase.check": "Checking for updates",
        "updates.phase.pull": "Pulling new code",
        "updates.phase.images": "Syncing quote images",
        "updates.phase.python": "Updating Python packages",
        "updates.phase.verify": "Verifying clock starts",
        "updates.phase.services": "Installing services",
        "updates.phase.restart": "Restarting",
    }

    JS_KEYS = (
        "updates.terminal.unrecovered",
        "updates.terminal.reverted",
        "updates.terminal.dead_updater",
        "updates.terminal.reconnect_failed",
        "common.alert.token_invalid",
        "common.alert.http_status",
        "common.confirm.timeout",
        "updates.pill.checking",
        "updates.pill.check_failed",
    )

    @staticmethod
    def _catalog():
        import json as _json

        return _json.loads(
            (REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8")
        )

    def test_catalog_carries_every_key(self):
        catalog = self._catalog()
        for key, value in self.SSR_KEYS.items():
            assert catalog.get(key) == value, key
        for key in self.JS_KEYS:
            assert key in catalog, key

    def test_template_routes_ssr_strings_through_t(self):
        body = (REPO_ROOT / "src" / "control_server" / "templates" / "updates.html.j2").read_text(
            encoding="utf-8"
        )
        for key in self.SSR_KEYS:
            if key.startswith("updates.phase."):
                assert f"'{key}'" in body, f"{key} missing from the phase-key loop"
            else:
                assert f"t('{key}')" in body, f"{key} not routed through t()"

    def test_blob_covers_the_js_keys(self):
        body = (REPO_ROOT / "src" / "control_server" / "templates" / "updates.html.j2").read_text(
            encoding="utf-8"
        )
        m = re.search(r"data-litclock-strings>\{\{ catalog_subset\(\[(.*?)\]\)", body, re.DOTALL)
        assert m, "updates page carries no strings blob"
        blob_keys = set(re.findall(r"'([a-z0-9_.]+)'", m.group(1)))
        assert blob_keys == set(self.JS_KEYS), blob_keys.symmetric_difference(self.JS_KEYS)

    def test_js_fallbacks_match_the_catalog(self):
        """Every tr('key', 'fallback') in updates.js carries a fallback that
        is a VERIFIED COPY of the catalog value (the stale-JS window
        contract — fallbacks are not a second source)."""
        catalog = self._catalog()
        body = (JS / "updates.js").read_text(encoding="utf-8")
        pairs = re.findall(r"tr\(\s*'([a-z0-9_.]+)'\s*,\s*'((?:[^'\\]|\\.)*)'\s*\)", body)
        pairs += [
            (m[0], m[1])
            for m in re.findall(r'tr\(\s*\'([a-z0-9_.]+)\'\s*,\s*"((?:[^"\\]|\\.)*)"\s*\)', body)
        ]
        assert len(pairs) >= 10, f"tr() extraction drifted: {len(pairs)} pairs"
        seen = set()
        for key, fallback in pairs:
            seen.add(key)
            fallback = fallback.replace("\\'", "'").replace('\\"', '"')
            assert catalog.get(key) == fallback, (key, fallback, catalog.get(key))
        assert seen == set(self.JS_KEYS), seen.symmetric_difference(self.JS_KEYS)

    def test_rendered_page_resolves(self, monkeypatch):
        import sys as _sys

        from markupsafe import escape

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import strings_catalog  # noqa: PLC0415
        from control_server import create_app  # noqa: PLC0415

        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        strings_catalog.reset_cache()
        app = create_app(test_config={"VERSION_OVERRIDE": "v0.0.0-test"})
        app.config["TESTING"] = True
        page = app.test_client().get("/updates").data.decode()
        # Phase rows + confirm sheet + reading title always render.
        for key in (
            "updates.reading.title",
            "updates.confirm.title",
            "updates.confirm.body",
            "updates.phase.check",
            "updates.phase.restart",
        ):
            assert str(escape(self.SSR_KEYS[key])) in page, key
        blobless = re.sub(r"data-litclock-strings>.*?</script>", "", page, flags=re.DOTALL)
        for key in list(self.SSR_KEYS) + list(self.JS_KEYS):
            assert f">{key}<" not in blobless, f"{key} leaked as element content"


class TestSystemPageJsCatalog:
    """litclock-dev#532 bulk extraction slice 5: system.js's dynamic copy
    (reconnect/terminal cards + alerts) through the blob + tr(); the three
    cross-file shared strings unified onto common.* keys used by BOTH
    updates.js and system.js — one source, no drift."""

    JS_KEYS = (
        "common.alert.token_invalid",
        "common.alert.http_status",
        "common.confirm.timeout",
        "common.confirm.consumed",
        "system.card.restarting.title",
        "system.card.restarting.body",
        "system.card.wifi_reset.title",
        "system.card.wifi_reset.body",
        "system.card.wifi_reset.body2",
        "system.card.factory_reset.title",
        "system.card.factory_reset.body",
        "system.card.factory_reset.body2",
        "system.card.factory_reset.body3",
        "system.card.gift.title",
        "system.card.gift.body",
        "system.card.gift.body2_lead",
        "system.card.gift.body2",
        "system.card.retry.title",
        "system.card.retry.button",
        "system.card.shutting_down.title",
        "system.card.shutting_down.body",
        "system.card.syncing.title",
        "system.card.syncing.body",
        "system.card.safe_unplug.title",
        "system.card.safe_unplug.body",
    )

    @staticmethod
    def _catalog():
        import json as _json

        return _json.loads(
            (REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8")
        )

    def test_blob_covers_the_js_keys(self):
        body = (REPO_ROOT / "src" / "control_server" / "templates" / "system.html.j2").read_text(
            encoding="utf-8"
        )
        m = re.search(r"data-litclock-strings>\{\{ catalog_subset\(\[(.*?)\]\)", body, re.DOTALL)
        assert m, "system page carries no strings blob"
        blob_keys = set(re.findall(r"'([a-z0-9_.]+)'", m.group(1)))
        assert blob_keys == set(self.JS_KEYS), blob_keys.symmetric_difference(self.JS_KEYS)

    def test_js_fallbacks_match_the_catalog(self):
        catalog = self._catalog()
        body = (JS / "system.js").read_text(encoding="utf-8")
        pairs = re.findall(r"tr\(\s*'([a-z0-9_.]+)'\s*,\s*'((?:[^'\\]|\\.)*)'\s*\)", body)
        pairs += [
            (m[0], m[1])
            for m in re.findall(r'tr\(\s*\'([a-z0-9_.]+)\'\s*,\s*"((?:[^"\\]|\\.)*)"\s*\)', body)
        ]
        assert len(pairs) >= 25, f"tr() extraction drifted: {len(pairs)} pairs"
        seen = set()
        for key, fallback in pairs:
            seen.add(key)
            fallback = fallback.replace("\\'", "'").replace('\\"', '"')
            assert catalog.get(key) == fallback, (key, fallback, catalog.get(key))
        assert seen == set(self.JS_KEYS), seen.symmetric_difference(self.JS_KEYS)

    def test_shared_common_keys_are_single_sourced_across_files(self):
        """The whole point of common.*: both files' fallbacks for a shared
        key must be the SAME string (and equal the catalog value)."""
        catalog = self._catalog()
        for name in ("common.alert.token_invalid", "common.alert.http_status", "common.confirm.timeout"):
            assert name in catalog
            for js_file in ("updates.js", "system.js"):
                body = (JS / js_file).read_text(encoding="utf-8")
                assert f"tr('{name}'" in body, f"{js_file} lost its {name} site"

    def test_rendered_system_blob_resolves(self, monkeypatch):
        import json as _json
        import sys as _sys

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import strings_catalog  # noqa: PLC0415
        from control_server import create_app  # noqa: PLC0415

        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        strings_catalog.reset_cache()
        app = create_app(test_config={"VERSION_OVERRIDE": "v0.0.0-test"})
        app.config["TESTING"] = True
        page = app.test_client().get("/system").data.decode()
        m = re.search(r"data-litclock-strings>(.*?)</script>", page, re.DOTALL)
        assert m, "rendered system page carries no strings blob"
        blob = _json.loads(m.group(1))
        assert blob["system.card.syncing.title"] == "Almost done… {seconds}s"
        assert blob["system.card.wifi_reset.body"] == (
            "Join {network} from your phone’s WiFi list, then enter your new WiFi."
        )
        # No key resolves to itself (missing-from-catalog symptom).
        for key, value in blob.items():
            assert value != key, f"{key} did not resolve"


class TestSystemPageSsrCatalog:
    """litclock-dev#532 bulk extraction slice 6: system.html.j2's SSR
    strings via t()/t_rich(). t_rich's contract: catalog values stay plain
    text with a whitelisted {b}/{em}/{code} token vocabulary; the value is
    escaped BEFORE tokens become tags (same order as system.js's esc())."""

    PLAIN_KEYS = (
        "system.title",
        "aria.system.actions",
        "common.banner.saved",
        "common.cancel",
        "system.action.restart.title",
        "system.action.restart.consequence",
        "system.action.restart.button",
        "system.action.poweroff.title",
        "system.action.poweroff.consequence",
        "system.action.poweroff.button",
        "system.action.wifi_reset.title",
        "system.action.wifi_reset.consequence",
        "system.action.wifi_reset.button",
        "system.action.factory_reset.title",
        "system.action.factory_reset.button",
        "system.action.gift.title",
        "system.action.gift.consequence",
        "system.action.gift.button",
        "system.gift.label",
        "system.gift.placeholder",
        "system.gift.save_draft",
        "system.sheet.reboot.title",
        "system.sheet.reboot.body",
        "system.sheet.reboot.confirm",
        "system.sheet.poweroff.title",
        "system.sheet.poweroff.confirm",
        "system.sheet.wifi_reset.title",
        "system.sheet.wifi_reset.confirm",
        "system.sheet.factory_reset.title",
        "system.sheet.factory_reset.confirm",
        "system.sheet.gift.title",
        "system.sheet.gift.body",
        "system.sheet.gift.confirm",
    )
    RICH_KEYS = (
        "system.action.factory_reset.consequence",
        "system.gift.help",
        "system.sheet.poweroff.body",
        "system.sheet.wifi_reset.body",
        "system.sheet.factory_reset.body",
    )

    @staticmethod
    def _catalog():
        import json as _json

        return _json.loads(
            (REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8")
        )

    def test_catalog_carries_every_key_and_rich_tokens_balance(self):
        catalog = self._catalog()
        for key in self.PLAIN_KEYS + self.RICH_KEYS:
            assert key in catalog, key
        for key in self.RICH_KEYS:
            value = catalog[key]
            for open_tok, close_tok in (("{b}", "{/b}"), ("{em}", "{/em}"), ("{code}", "{/code}")):
                assert value.count(open_tok) == value.count(close_tok), (key, open_tok)
        # Plain keys carry NO rich tokens (they render through t(), which
        # would show them literally) — the FULL vocabulary, open and close
        # (Codex slice-6 /review: a two-token check misses {em} and the
        # closers).
        for key in self.PLAIN_KEYS:
            for tok in ("{b}", "{/b}", "{em}", "{/em}", "{code}", "{/code}"):
                assert tok not in catalog[key], (key, tok)

    def test_template_routes_through_t_and_t_rich(self):
        body = (REPO_ROOT / "src" / "control_server" / "templates" / "system.html.j2").read_text(
            encoding="utf-8"
        )
        for key in self.RICH_KEYS:
            assert f"t_rich('{key}'" in body, f"{key} must go through t_rich"
        for key in self.PLAIN_KEYS:
            if key == "common.cancel":
                assert body.count(f"t('{key}')") == 5  # every sheet's Cancel
            else:
                assert f"t('{key}')" in body, key

    def test_rendered_page_composes_rich_copy(self, monkeypatch):
        import sys as _sys

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import strings_catalog  # noqa: PLC0415
        from control_server import create_app  # noqa: PLC0415

        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        strings_catalog.reset_cache()
        app = create_app(test_config={"VERSION_OVERRIDE": "v0.0.0-test"})
        app.config["TESTING"] = True
        page = app.test_client().get("/system").data.decode()
        # Tokens converted to real tags; the {network} slot filled.
        assert "<strong>Only your WiFi</strong>" in page
        assert "<strong>LitClock-Setup</strong>" in page
        assert "<strong>unplug and re-plug</strong>" in page
        assert "<code>$</code>" in page
        # No token or slot leaks to the rendered page.
        blobless = re.sub(r"data-litclock-strings>.*?</script>", "", page, flags=re.DOTALL)
        assert "{b}" not in blobless and "{/b}" not in blobless
        assert "{network}" not in blobless
        # No raw keys leak.
        for key in self.PLAIN_KEYS + self.RICH_KEYS:
            assert f">{key}<" not in blobless, key
            assert f'="{key}"' not in blobless, key

    def test_t_rich_escapes_before_token_conversion(self, monkeypatch):
        """A hostile catalog value's markup renders as TEXT; only the token
        whitelist becomes tags (executed against a poisoned registry)."""
        import sys as _sys

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import strings_catalog  # noqa: PLC0415
        from control_server import create_app  # noqa: PLC0415

        app = create_app(test_config={"VERSION_OVERRIDE": "v0.0.0-test"})
        t_rich = app.jinja_env.globals["t_rich"]
        monkeypatch.setattr(
            strings_catalog,
            "get",
            lambda key, /, **slots: '<script>alert(1)</script> {b}safe{/b} "q"',
        )
        # create_app bound t_rich to the module function at registration
        # time — patch through the module attribute the closure reads.
        out = str(t_rich("any.key"))
        assert "<script>" not in out
        assert "&lt;script&gt;" in out
        assert "<strong>safe</strong>" in out

    def test_t_rich_slot_values_cannot_form_tokens_or_markup(self, monkeypatch):
        """Slots fill AFTER escaping and token conversion: a hostile slot
        value carrying {b} or <script> renders as literal text."""
        import sys as _sys

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import strings_catalog  # noqa: PLC0415
        from control_server import create_app  # noqa: PLC0415

        app = create_app(test_config={"VERSION_OVERRIDE": "v0.0.0-test"})
        t_rich = app.jinja_env.globals["t_rich"]
        monkeypatch.setattr(
            strings_catalog, "get", lambda key, /, **slots: "join {b}{network}{/b} now"
        )
        out = str(t_rich("any.key", network="{b}<script>x</script>{/b}"))
        assert "<script>" not in out
        assert "&lt;script&gt;" in out
        # The slot's OWN {b} stays literal text; only the value's tokens converted.
        assert out.count("<strong>") == 1
        assert "{b}&lt;script&gt;" in out


class TestSettingsPageCatalog:
    """litclock-dev#532 bulk extraction slice 7: the Settings page —
    template via t()/t_rich, settings.js via blob + tr()."""

    JS_KEYS = (
        "settings.location.save_disabled_tooltip",
        "settings.js.geocode_not_found",
        "settings.js.tz_failed_http",
        "settings.js.tz_failed_network",
        "settings.js.save_failed.nsfw",
        "settings.js.save_failed.diag_shortcut",
        "settings.js.save_failed.weather",
        "settings.js.save_failed.units",
        "settings.js.save_failed.language",
    )
    # A representative SSR sample (the full set lives in the template; these
    # rendered-value canaries catch a broken t() wholesale).
    SSR_SAMPLE = {
        "settings.title": "Settings",
        "settings.location.title": "Location",
        "settings.location.hint": "Used for weather and timezone.",
        "settings.location.mode_auto": "Automatic",
        "settings.location.mode_specific": "Specific",
        "settings.location.place_placeholder": "Type a city or zip",
        "settings.tz_fallback.button": "Use my browser's timezone ({tz})",
        "settings.temperature.fahrenheit": "Fahrenheit",
        "settings.temperature.celsius": "Celsius",
        "settings.advanced.nsfw_label": "Allow NSFW quotes",
        "settings.footer.link": "System → Reset WiFi",
        "aria.settings.location_mode": "Location mode",
        "aria.settings.temperature_units": "Temperature units",
    }

    @staticmethod
    def _catalog():
        import json as _json

        return _json.loads(
            (REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8")
        )

    def test_catalog_carries_the_keys(self):
        catalog = self._catalog()
        for key, value in self.SSR_SAMPLE.items():
            assert catalog.get(key) == value, key
        for key in self.JS_KEYS:
            assert key in catalog, key
        # Conditionally rendered (only when the Save button is disabled) —
        # catalog + template routing checked here, not on the default render.
        assert catalog.get("settings.location.save_disabled_tooltip") == "Type a place or pick Automatic"
        body = (REPO_ROOT / "src" / "control_server" / "templates" / "settings.html.j2").read_text(
            encoding="utf-8"
        )
        assert "t('settings.location.save_disabled_tooltip')" in body

    def test_blob_covers_the_js_keys(self):
        body = (REPO_ROOT / "src" / "control_server" / "templates" / "settings.html.j2").read_text(
            encoding="utf-8"
        )
        m = re.search(r"data-litclock-strings>\{\{ catalog_subset\(\[(.*?)\]\)", body, re.DOTALL)
        assert m, "settings page carries no strings blob"
        blob_keys = set(re.findall(r"'([a-z0-9_.]+)'", m.group(1)))
        assert blob_keys == set(self.JS_KEYS), blob_keys.symmetric_difference(self.JS_KEYS)

    def test_js_fallbacks_match_the_catalog(self):
        catalog = self._catalog()
        body = (JS / "settings.js").read_text(encoding="utf-8")
        pairs = re.findall(r"tr\(\s*'([a-z0-9_.]+)'\s*,\s*'((?:[^'\\]|\\.)*)'\s*\)", body)
        pairs += [
            (m[0], m[1])
            for m in re.findall(r'tr\(\s*\'([a-z0-9_.]+)\'\s*,\s*"((?:[^"\\]|\\.)*)"\s*\)', body)
        ]
        assert len(pairs) == len(self.JS_KEYS), f"tr() extraction drifted: {len(pairs)}"
        seen = set()
        for key, fallback in pairs:
            seen.add(key)
            fallback = fallback.replace("\\'", "'").replace('\\"', '"')
            assert catalog.get(key) == fallback, (key, fallback, catalog.get(key))
        assert seen == set(self.JS_KEYS)

    def test_rendered_page_resolves_and_leaks_nothing(self, monkeypatch):
        import sys as _sys

        from markupsafe import escape

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import strings_catalog  # noqa: PLC0415
        from control_server import create_app  # noqa: PLC0415

        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        strings_catalog.reset_cache()
        app = create_app(test_config={"VERSION_OVERRIDE": "v0.0.0-test"})
        app.config["TESTING"] = True
        page = app.test_client().get("/settings").data.decode()
        for key, value in self.SSR_SAMPLE.items():
            if "{tz}" in value:
                # Rendered split around the JS-filled span.
                assert str(escape(value.split("{tz}")[0])).rstrip("(") in page, key
                continue
            assert str(escape(value)) in page, key
        blobless = re.sub(r"data-litclock-strings>.*?</script>", "", page, flags=re.DOTALL)
        for key in list(self.SSR_SAMPLE) + list(self.JS_KEYS):
            assert f">{key}<" not in blobless and f'="{key}"' not in blobless, key
        # The tz-button split kept the span mechanism intact.
        assert 'data-browser-tz-label>—</span>)' in page
        # No rich tokens leak.
        assert "{b}" not in blobless and "{/b}" not in blobless


class TestShellAndHandoffCatalog:
    """litclock-dev#532 bulk extraction slice 8: the shell (base template +
    drawer.js) and the handoff banner (+ handoff.js) through the catalog.
    The shell scripts read a base-level blob on its own attribute; their
    accessor is the standard copy with only the selector line differing —
    pinned as a pair here (the 5-way page-blob lockstep stays separate)."""

    SHELL_JS_KEYS = (
        "shell.drawer.hidden_batch",
        "shell.drawer.fresh_clicked",
        "handoff.fail_body_tz",
    )
    SSR_SAMPLE = {
        "nav.status": "Status",
        "nav.settings": "Settings",
        "nav.system": "System",
        "nav.updates": "Updates",
        "aths.title": "Add to Home Screen",
        "shell.drawer.label": "Live diagnostics",
        "shell.level.all": "All",
        "shell.empty.quiet_headline": "It's quiet.",
        "shell.drawer.fresh": "Start fresh",
        "shell.welcome.dismiss": "Got it",
        "aria.shell.drawer_open": "Open live diagnostics drawer",
        "aria.nav.primary": "Primary",
    }
    HANDOFF_KEYS = {
        "handoff.success.title": "Setup complete",
        "handoff.success.done": "Done — Start the Clock",
        # NBSP between "2" and "minutes" — the original template's &nbsp;.
        "handoff.success.body": "Quotes start in 2\u00a0minutes. Tap Done to start sooner, or fine-tune in {link}.",
        "handoff.fail.tz_button": "Use {tz}",
        "handoff.fail.set_tz": "Set my timezone",
        "handoff.fail_body_tz": (
            "We couldn’t detect your timezone. Your phone says you’re in {tz}. "
            "Confirm so quotes show at the right time."
        ),
    }

    @staticmethod
    def _catalog():
        import json as _json

        return _json.loads(
            (REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8")
        )

    def test_catalog_carries_the_keys(self):
        catalog = self._catalog()
        for table in (self.SSR_SAMPLE, self.HANDOFF_KEYS):
            for key, value in table.items():
                assert catalog.get(key) == value, key
        for key in self.SHELL_JS_KEYS:
            assert key in catalog, key

    def test_shell_blob_covers_the_js_keys(self):
        base = (TEMPLATES / "base.html.j2").read_text()
        m = re.search(
            r"data-litclock-shell-strings>\{\{ catalog_subset\(\[(.*?)\]\)", base, re.DOTALL
        )
        assert m, "base carries no shell blob"
        blob_keys = set(re.findall(r"'([a-z0-9_.]+)'", m.group(1)))
        assert blob_keys == set(self.SHELL_JS_KEYS), blob_keys.symmetric_difference(
            self.SHELL_JS_KEYS
        )

    def test_shell_accessor_pair_in_lockstep(self):
        def _accessor(path):
            text = path.read_text()
            i = text.index("  var STRINGS = (function () {")
            j = text.index("\n  }\n", text.index("function tr(key, fallback)", i)) + 4
            return text[i:j]

        drawer = _accessor(JS / "drawer.js")
        handoff = _accessor(JS / "handoff.js")
        assert drawer == handoff, "the SHELL accessor pair drifted — keep byte-identical"
        assert "data-litclock-shell-strings" in drawer
        # And the page-blob accessors keep their OWN selector.
        assert "data-litclock-shell-strings" not in _accessor(JS / "status.js")

    def test_shell_js_fallbacks_match_the_catalog(self):
        catalog = self._catalog()
        for js_file, expected in (("drawer.js", 2), ("handoff.js", 1)):
            body = (JS / js_file).read_text(encoding="utf-8")
            pairs = re.findall(r"tr\(\s*\n?\s*'([a-z0-9_.]+)'\s*,\s*\n?\s*'((?:[^'\\]|\\.)*)'\s*\n?\s*\)", body)
            assert len(pairs) == expected, (js_file, len(pairs))
            # (drawer.js: hidden_batch + fresh_clicked; the follow pill is
            # SSR-composed in base — JS only updates the count span.)
            for key, fallback in pairs:
                fallback = fallback.replace("\\'", "'")
                assert catalog.get(key) == fallback, (js_file, key)

    def test_rendered_shell_resolves(self, monkeypatch):
        import json as _json
        import sys as _sys

        from markupsafe import escape

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import strings_catalog  # noqa: PLC0415
        from control_server import create_app  # noqa: PLC0415

        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        strings_catalog.reset_cache()
        app = create_app(test_config={"VERSION_OVERRIDE": "v0.0.0-test"})
        app.config["TESTING"] = True
        page = app.test_client().get("/").data.decode()
        for key, value in self.SSR_SAMPLE.items():
            assert str(escape(value)) in page, key
        m = re.search(r"data-litclock-shell-strings>(.*?)</script>", page, re.DOTALL)
        assert m, "rendered page carries no shell blob"
        blob = _json.loads(m.group(1))
        assert blob["shell.drawer.hidden_batch"].startswith("Earlier {n} hidden")
        # The follow pill split kept the count span between the halves.
        assert re.search(r"↓ <span data-diag-drawer-follow-count>0</span> new", page), (
            "follow-pill slot split lost its composition"
        )

    def test_handoff_banner_routes_through_t(self):
        body = (TEMPLATES / "_handoff-banner.html.j2").read_text()
        for key in self.HANDOFF_KEYS:
            if key == "handoff.fail_body_tz":
                continue  # JS-only key (shell blob)
            assert f"'{key}'" in body, key
        # The tz-button template attr carries the catalog {tz} slot.
        assert 'data-tz-label-template="{{ t(\'handoff.fail.tz_button\') }}"' in body


class TestDiagnosticsPageCatalogRemainder:
    """litclock-dev#532 bulk extraction slice 9: the diagnostics page's
    remaining copy — banner meta, reveal pill, sections, tails, copy card,
    announcer strings — through t() and the existing page blob."""

    JS_ONLY_KEYS = (
        "diag.refreshed.seconds",
        "diag.refreshed.minutes",
        "diag.refreshed.hours",
        "diag.reveal.hide",
        "diag.copy.state_visible",
        "diag.announce.copied",
        "diag.announce.copied_payload",
        "diag.announce.copy_failed",
        "diag.announce.reveal_on",
        "diag.announce.reveal_off",
        "diag.announce.avail_both",
        "diag.announce.avail_net",
        "diag.announce.avail_loc",
        "diag.tail.error",
        "diag.tail.empty",
        "diag.payload.loading",
        "diag.payload.empty",
        "diag.payload.unavailable",
        "diag.copy.state_redacted",
        "diag.pill.ok",
        "diag.refresh_failed",
        "diag.value.yes",
        "diag.value.no",
    )
    SSR_SAMPLE = {
        "diag.refreshed.just_now": "Refreshed just now",
        "diag.banner.auto_interval": "Auto every 30s",
        "diag.reveal.show": "Reveal",
        "diag.section.services": "Services",
        "diag.section.log": "Recent log entries",
        "diag.copy.title": "Copy support payload",
        "diag.copy.button": "Copy",
        "diag.copy.download": "Download full logs",
        "diag.copy.state_redacted": "redacted",
        "diag.drawer_link": "Open live drawer",
        "diag.drawer_link.nojs_note": "(The live drawer needs JavaScript.)",
        "aria.diag.reveal": "Reveal SSID, city, and exact coordinates",
        "diag.section.build": "Build & version",
        "diag.section.network": "Network",
        "diag.anomaly.network": "Connection issue",
        "diag.anomaly.generic": "Needs attention",
        "diag.uncollected.network": (
            "Network details fill in once your clock sees a network event."
        ),
        "diag.log.snapshot_only": "Snapshot only.",
    }

    @staticmethod
    def _catalog():
        import json as _json

        return _json.loads(
            (REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8")
        )

    def test_catalog_carries_the_keys(self):
        catalog = self._catalog()
        for key, value in self.SSR_SAMPLE.items():
            assert catalog.get(key) == value, key
        for key in self.JS_ONLY_KEYS:
            assert key in catalog, key
        # The description's {state} slot must survive in the raw value.
        assert "{state}" in catalog["diag.copy.description"]

    def test_blob_covers_the_new_js_keys(self):
        body = DIAG_TEMPLATE.read_text()
        m = re.search(r"data-litclock-strings>\{\{ catalog_subset\(\[(.*?)\]\)", body, re.DOTALL)
        assert m
        blob_keys = set(re.findall(r"'([a-z0-9_.]+)'", m.group(1)))
        for key in self.JS_ONLY_KEYS + ("diag.refreshed.just_now", "diag.reveal.show", "diag.tail.loading"):
            assert key in blob_keys, key

    def test_new_js_fallbacks_match_the_catalog(self):
        catalog = self._catalog()
        body = DIAG_JS.read_text(encoding="utf-8")
        for key in self.JS_ONLY_KEYS:
            m = re.search(r"tr\('" + re.escape(key) + r"',\s*'((?:[^'\\]|\\.)*)'\)", body)
            assert m, f"{key} has no tr() site in diagnostics.js"
            assert catalog[key] == m.group(1).replace("\\'", "'"), key

    def test_rendered_page_resolves(self, monkeypatch):
        import sys as _sys

        from markupsafe import escape

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import strings_catalog  # noqa: PLC0415
        from control_server import create_app  # noqa: PLC0415

        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        strings_catalog.reset_cache()
        app = create_app(test_config={"VERSION_OVERRIDE": "v0.0.0-test"})
        app.config["TESTING"] = True
        page = app.test_client().get("/diagnostics").data.decode()
        for key, value in self.SSR_SAMPLE.items():
            assert str(escape(value)) in page, key
        # The {state} split kept the reveal-state span composition intact.
        assert re.search(r"<span data-diag-copy-reveal-state>redacted</span>", page)
        blobless = re.sub(r"data-litclock-strings>.*?</script>", "", page, flags=re.DOTALL)
        assert "{state}" not in blobless
        for key in list(self.SSR_SAMPLE) + list(self.JS_ONLY_KEYS):
            assert f">{key}<" not in blobless and f'="{key}"' not in blobless, key


class TestRouteMessagesCatalog:
    """litclock-dev#532 bulk extraction slice 10: user-visible route-layer
    messages resolve from the catalog; the confirm-token copy resolves from
    the SAME keys the JS fallbacks pin (server and client can never drift).
    Developer-contract API messages (hand-crafted-request 400s: bad_limit,
    invalid_section, missing_sid, the SSE unit errors) stay pinned-English
    by DESIGN — they are API surface, not user copy."""

    API_KEYS = {
        "common.rate_limited.destructive": "Too many destructive actions. Try again shortly.",
        "common.update_in_progress": "An update is in progress. Try again in a few minutes.",
        "api.system.rate_limited": "Too many system actions. Try again shortly.",
        "api.system.systemctl_failed": "The system action could not be invoked.",
        "api.system.factory_reset_failed": "The factory reset could not be started.",
        "api.wifi.reset_failed": "The WiFi reset could not be started.",
        "api.updates.already_running": "An update is already running. Watch progress on the Updates tab.",
        "api.updates.dispatch_failed": "The update could not be started.",
        "api.handoff.set_tz_first": "Set your timezone first so quotes show at the right time.",
        "api.handoff.write_failed": "Could not finish setup. The clock will start on its own shortly.",
        "api.tz.required": "A timezone is required.",
        "api.tz.invalid_pick_settings": "That timezone isn't recognized. Pick one from Settings instead.",
        "api.tz.invalid": "That timezone isn't recognized. Pick one from a standard IANA list.",
        "api.gift.busy": "Another gift preparation is already in progress. Try again in a moment.",
        "api.gift.dispatch_failed": "The gift preparation could not be started.",
        "api.gift.unavailable": (
            "Gift preparation is unavailable on this device — the gift service isn't installed. "
            "Nothing was changed."
        ),
        "api.gift.env_lock_timeout": (
            "Settings file is busy — another update is in progress. Try again in a few seconds."
        ),
        "api.gift.clear_location_failed": (
            "Could not clear your location from the device. Nothing was changed — try again."
        ),
        "api.gift.stage_message_failed": "Could not stage the gift message. Try again.",
        "api.gift.stage_language_failed": "Could not stage the gift language. Try again.",
        "api.gift.clear_stale_language_failed": "Could not clear a stale gift language. Try again.",
        "api.gift.reset_warning": (
            "Gift prep couldn't start, and your saved location and timezone were reset — retry, or "
            "re-add your city and timezone in Settings if you're keeping this device."
        ),
        "api.updates.already_up_to_date": "You're already on {version} — nothing to do.",
        "system.gift_language.error": "Language {error}.",
    }

    def test_catalog_carries_the_keys(self):
        import json as _json

        catalog = _json.loads(
            (REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8")
        )
        for key, value in self.API_KEYS.items():
            assert catalog.get(key) == value, key
        # (the gift family + already_up_to_date are pinned by VALUE above)

    def test_confirm_token_copy_single_sourced_with_js(self):
        """The three consume-outcome messages come from the SAME keys the
        client-side fallbacks pin — assert the server file routes through
        them (the JS-side fallback parity already pins the values)."""
        body = (REPO_ROOT / "src" / "control_server" / "confirm_tokens.py").read_text()
        for key in ("common.confirm.timeout", "common.confirm.consumed", "common.alert.token_invalid"):
            assert f'strings_catalog.get("{key}")' in body, key

    def test_live_consume_outcome_resolves(self, monkeypatch):
        """Executed: the envelope body carries the resolved English copy."""
        import sys as _sys

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import strings_catalog  # noqa: PLC0415
        from control_server import create_app  # noqa: PLC0415
        from control_server.confirm_tokens import envelope_for_consume_outcome  # noqa: PLC0415

        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        strings_catalog.reset_cache()
        app = create_app(test_config={"VERSION_OVERRIDE": "v0.0.0-test"})
        with app.test_request_context():
            body, status = envelope_for_consume_outcome("expired")
            assert status == 401
            assert body.get_json()["error"]["message"] == (
                "This confirmation timed out for safety. Reload the page and try again."
            )

    def test_no_route_hardcodes_the_shared_copy(self):
        """The shared sentences must not survive as literals in any route
        module — a reintroduced copy would drift from the catalog."""
        routes = (REPO_ROOT / "src" / "control_server" / "routes")
        for path in list(routes.glob("*.py")) + [REPO_ROOT / "src" / "control_server" / "confirm_tokens.py"]:
            body = path.read_text()
            code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
            for sentence in (
                "Couldn't verify that action. Reload the page and try again.",
                "This confirmation timed out for safety.",
                "This action was already submitted.",
                "An update is in progress. Try again in a few minutes.",
            ):
                assert sentence not in code, (path.name, sentence)


class TestFinalSliceCatalog:
    """litclock-dev#532 bulk extraction final slice: the geocode copy pair,
    the config.py validator FRAGMENTS (they compose after field labels and
    the "Message {error}." template — the 4c decision, documented on the
    keys), and setup_server's remaining page copy."""

    VALIDATOR_KEYS = {
        "validator.numeric": "must be numeric",
        "validator.lat_range": "must be between -90 and 90",
        "validator.units_enum": "must be 'imperial' or 'metric'",
        "validator.language_active": "must be one of the active languages: {codes}",
        "validator.max_bytes": (
            "must be at most {n} bytes (emoji and accented characters take more than one byte each)"
        ),
        "validator.forbidden_char": "may not contain {ch}",
    }
    SETUP_KEYS = {
        "setup.page.title": "LitClock Setup",
        "setup.wifi.pick_label": "Pick your WiFi network",
        "setup.wifi.password_label": "Your WiFi Password",
        "setup.submit": "Complete Setup",
        "setup.banner.error_lead": "{b}Couldn’t join your WiFi:{/b} {error}",
        "setup.error.title": "Setup Error",
        "setup.error.pick_network": "Please select a WiFi network",
        "api.geocode.failed": "Location lookup failed.",
        "api.geocode.not_found": "Location not found.",
    }

    def test_catalog_carries_the_keys(self):
        import json as _json

        catalog = _json.loads(
            (REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8")
        )
        for table in (self.VALIDATOR_KEYS, self.SETUP_KEYS):
            for key, value in table.items():
                assert catalog.get(key) == value, key

    def test_validator_output_byte_identical(self):
        """Executed: the composed validator messages reproduce the old
        f-string output exactly (incl. the repr'd forbidden char)."""
        import sys as _sys

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import config  # noqa: PLC0415

        ok, err = config.validate_setting("WEATHER_LATITUDE", "banana")
        assert (ok, err) == (False, "must be numeric")
        ok, err = config.validate_setting("GIFT_MODE_MESSAGE", "has ` backtick")
        assert ok is False and err == "may not contain '`'", err

    def test_setup_error_template_prefilled(self):
        """The .replace prefill is non-vacuous: no static placeholder
        survives in HTML_ERROR, the retry copy renders, and the runtime
        {error} field is still there for .format."""
        import sys as _sys

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import setup_server  # noqa: PLC0415

        # Per-request build since the final-slice /review: every field
        # fills via .replace (brace-proof); the rendered page carries the
        # resolved copy and no placeholder, while the raw template still
        # holds them all for the builder.
        page = setup_server._error_page("PROBE-ERROR {oops}")
        for placeholder in ("{ERROR_TITLE_TEXT}", "{ERROR_RETRY_TEXT}", "{loading_js}", "{retry_js}", "{error}"):
            assert placeholder not in page, placeholder
        assert "Setup Error" in page
        assert "Try again" in page
        assert '"Try again"' in page  # the json.dumps'd JS literal
        assert "PROBE-ERROR {oops}" in page  # braces in the error survive literally
        for placeholder in ("{ERROR_TITLE_TEXT}", "{ERROR_RETRY_TEXT}", "{error}"):
            assert placeholder in setup_server._HTML_ERROR_TEMPLATE, placeholder

    def test_setup_copy_resolves_per_request_not_at_import(self, monkeypatch):
        """The final-slice /review's activation-day fix: page copy resolves
        at BUILD time, so a language persisted mid-session (failed-WiFi
        retry) or carried by the gift marker reaches the re-render —
        import-time constants froze the language and made retries
        mixed-language. Proven by poisoning the resolver between builds."""
        import sys as _sys

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import setup_server  # noqa: PLC0415

        old_mode = setup_server.PROVISIONING_MODE
        setup_server.PROVISIONING_MODE = True
        try:
            # setup_server binds `from strings_catalog import get as
            # _catalog_get` — patch the module-local name (the language
            # itself re-resolves per call inside get(), reading env.sh).
            real_get = setup_server._catalog_get
            monkeypatch.setattr(
                setup_server,
                "_catalog_get",
                lambda key, /, **slots: "SWITCHED-" + key
                if key == "setup.wifi.pick_label"
                else real_get(key, **slots),
            )
            page = setup_server._build_setup_html()
            assert "SWITCHED-setup.wifi.pick_label" in page
            # Error + CNA pages re-resolve too.
            assert "Setup Error" in setup_server._error_page("x")
            assert "Open Setup" in setup_server._build_cna_bridge_html()
        finally:
            setup_server.PROVISIONING_MODE = old_mode

    def test_setup_page_renders_catalog_values(self):
        import sys as _sys

        _src = str(REPO_ROOT / "src")
        if _src not in _sys.path:
            _sys.path.insert(0, _src)
        import setup_server  # noqa: PLC0415

        monkeypatch_mode = setup_server.PROVISIONING_MODE
        setup_server.PROVISIONING_MODE = True
        try:
            page = setup_server._build_setup_html()
        finally:
            setup_server.PROVISIONING_MODE = monkeypatch_mode
        for key, value in self.SETUP_KEYS.items():
            if key.startswith(("api.", "setup.error.", "setup.banner.")):
                continue  # not on the default render
            assert value in page, key
        # The rich banner composes on the failure render.
        old_error = setup_server.WIFI_CONNECT_ERROR
        old_mode = setup_server.PROVISIONING_MODE
        try:
            setup_server.WIFI_CONNECT_ERROR = "test failure"
            setup_server.PROVISIONING_MODE = True
            page = setup_server._build_setup_html()
            assert "<strong>Couldn’t join your WiFi:</strong> test failure<br>" in page
        finally:
            setup_server.WIFI_CONNECT_ERROR = old_error
            setup_server.PROVISIONING_MODE = old_mode
