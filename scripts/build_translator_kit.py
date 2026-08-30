#!/usr/bin/env python3
"""Generate the LitClock translator kit from the English catalog (litclock-dev#532).

One source (the en catalog + the shared src/catalog_lint.py rules) drives
every translator-facing artifact, so none of them can drift from the CI
gate:

    translations/worksheet.md          per-key worksheet, grouped by surface
    translations/strings.template.json  blank skeleton (exact en key set)
    translations/kit-data.json          machine metadata (slots, vocab, budgets)
    translations/tool/index.html        offline fill-and-export tool (embeds the data)

    python3 scripts/build_translator_kit.py            # write the artifacts
    python3 scripts/build_translator_kit.py --check    # fail if they are stale

The README (translations/README.md) is hand-authored prose, not generated.
Run with --check in CI so an en-catalog edit that isn't re-kitted fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import catalog_lint  # noqa: E402

_OUT = _REPO_ROOT / "translations"

# Namespace → the surface a translator sees it on. The e-ink splash surfaces
# are width-limited: the panel is 800×480 and long lines clip, so a splash
# translation should stay near the English length. The PWA and setup page
# reflow, so they are length-flexible.
_SURFACES: dict[str, tuple[str, str, bool]] = {
    # namespace: (surface label, note, length_sensitive)
    "boot": ("E-ink splash", "Painted on the physical screen — width-limited, keep it short.", True),
    "firstboot": ("E-ink splash", "First-boot screens on the physical panel — width-limited.", True),
    "shutdown": ("E-ink splash", "Shutdown screen on the physical panel — width-limited.", True),
    "bootcheck": ("E-ink splash", "Recovery screen on the physical panel — width-limited.", True),
    "reset": ("E-ink splash", "Factory-reset screen on the physical panel — width-limited.", True),
    "setup": ("WiFi setup page", "The hotspot page a phone opens to join WiFi.", False),
    "system": ("Control app", "The phone/desktop control app (System tab).", False),
    "settings": ("Control app", "The control app's Settings tab.", False),
    "updates": ("Control app", "The control app's Updates tab.", False),
    "status": ("Control app", "The control app's Status view.", False),
    "diag": ("Control app", "The control app's diagnostics view.", False),
    "handoff": ("Control app", "The post-setup handoff banner.", False),
    "shell": ("Control app", "The control app's shared shell / navigation.", False),
    "nav": ("Control app", "The control app's navigation labels.", False),
    "aths": ("Control app", "The 'Add to Home Screen' hint.", False),
    "aria": ("Control app", "Screen-reader labels — describe the control's purpose.", False),
    "common": ("Control app", "Shared UI strings (buttons, alerts).", False),
    "api": ("Messages", "Status and error messages.", False),
    "validator": ("Messages", "Form-validation error messages.", False),
}

# Plain-language glossary for the {slots} a value can carry. A slot is a
# placeholder the software fills in at runtime — copy it EXACTLY, don't
# translate the word inside the braces.
_SLOT_GLOSSARY: dict[str, str] = {
    "network": "the WiFi network name",
    "ssid": "the WiFi network name",
    "n": "a number (a count)",
    "error": "the system's own error text",
    "name": "a name (device or gift recipient)",
    "tz": "the timezone, e.g. America/Chicago",
    "ip": "the device's IP address",
    "host": "the setup web address",
    "summary": "a short summary label",
    "version": "a version number",
    "attempt": "the current retry number",
    "max": "the maximum number of retries",
    "seconds": "a number of seconds",
    "state": "a status word",
    "status": "an HTTP status code (a number)",
    "codes": "a list of codes",
    "link": "a clickable link's text",
    "ch": "a single character",
}

# The rich-token vocabulary in translator language.
_TOKEN_HELP = {
    "b": "{b}…{/b} = bold",
    "em": "{em}…{/em} = italic",
    "code": "{code}…{/code} = monospace",
}


def _en_catalog() -> dict[str, str]:
    reg = json.loads((_REPO_ROOT / "languages.json").read_text(encoding="utf-8"))["languages"]["en"]
    data = json.loads((_REPO_ROOT / reg["strings"]).read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, str)}


def _en_plural_forms() -> set[str]:
    reg = json.loads((_REPO_ROOT / "languages.json").read_text(encoding="utf-8"))["languages"]["en"]
    forms = reg.get("plural_forms")
    return set(forms) if isinstance(forms, list) else {"one", "other"}


def build_data() -> dict:
    """The single metadata structure every artifact derives from."""
    en = _en_catalog()
    capable = catalog_lint.rich_capable_keys(_REPO_ROOT)
    forms = _en_plural_forms()

    # Fail loud, not silent: a new namespace mislabeled "Control app" would
    # strip a real e-ink splash of its length flag, and a rich token with no
    # help line would vanish from the worksheet while still being permitted
    # (the kit /review flagged both as silent-drift). Force a human to extend
    # the maps when the catalog grows.
    unknown_ns = {k.split(".")[0] for k in en} - set(_SURFACES)
    if unknown_ns:
        raise SystemExit(f"add these namespaces to _SURFACES: {sorted(unknown_ns)}")
    unknown_tokens = {t for vocab in capable.values() for t in vocab} - set(_TOKEN_HELP)
    if unknown_tokens:
        raise SystemExit(f"add these rich tokens to _TOKEN_HELP: {sorted(unknown_tokens)}")

    keys = []
    for key in sorted(en):
        english = en[key]
        namespace = key.split(".")[0]
        surface, surface_note, length_sensitive = _SURFACES.get(
            namespace, ("Control app", "", False)
        )
        counts = catalog_lint.slot_counts(english)
        slots = [
            {
                "name": name,
                "count": count,
                "note": _SLOT_GLOSSARY.get(name, "a value the software fills in"),
            }
            for name, count in sorted(counts.items())
        ]
        vocab = capable.get(key, ())
        rich = {
            "allowed": list(vocab),
            "help": [_TOKEN_HELP[t] for t in vocab if t in _TOKEN_HELP],
        }
        category = key.rpartition(".")[2]
        plural = None
        sibling = catalog_lint.plural_sibling_slots(key, forms, en)
        if category in forms and sibling is not None:
            plural = {
                "category": category,
                "may_drop_n": category in catalog_lint.SINGLE_VALUED_CATEGORIES,
                # The union of slots across the plural family — Python's
                # value_errors lets these appear any number of times. Embedded
                # so the HTML tool's JS lint mirrors the gate exactly.
                "sibling_slots": sorted(sibling),
            }
        keys.append(
            {
                "key": key,
                "english": english,
                "surface": surface,
                "surface_note": surface_note,
                "length_sensitive": length_sensitive,
                "en_length": len(english),
                "slots": slots,
                "rich": rich,
                "plural": plural,
            }
        )
    return {
        "_comment": "Generated by scripts/build_translator_kit.py from the English catalog. Do not hand-edit.",
        "schema": 1,
        "plural_forms_en": sorted(forms),
        "keys": keys,
    }


def render_worksheet(data: dict) -> str:
    lines = [
        "# LitClock translation worksheet",
        "",
        "Generated from the English catalog — do not hand-edit; run "
        "`python3 scripts/build_translator_kit.py`.",
        "",
        "Fill in a translation for every key. Read **README.md** first for the "
        "rules (they are also checked automatically). Copy every `{slot}` "
        "exactly — never translate the word inside the braces or change how "
        "many times it appears.",
        "",
    ]
    by_surface: dict[str, list[dict]] = {}
    for entry in data["keys"]:
        by_surface.setdefault(entry["surface"], []).append(entry)
    for surface in sorted(by_surface):
        entries = by_surface[surface]
        note = entries[0]["surface_note"]
        lines.append(f"## {surface}")
        if note:
            lines.append(f"_{note}_")
        lines.append("")
        for e in entries:
            lines.append(f"### `{e['key']}`")
            lines.append(f"- **English:** {e['english']}")
            if e["length_sensitive"]:
                lines.append(f"- **Keep it short** — physical screen; English is {e['en_length']} characters.")
            if e["slots"]:
                slot_str = "; ".join(
                    f"`{{{s['name']}}}`" + (f"×{s['count']}" if s["count"] > 1 else "") + f" = {s['note']}"
                    for s in e["slots"]
                )
                lines.append(f"- **Placeholders (copy exactly):** {slot_str}")
            if e["rich"]["allowed"]:
                lines.append(f"- **Formatting allowed:** {', '.join(e['rich']['help'])}")
            if e["plural"]:
                if e["plural"]["may_drop_n"]:
                    lines.append("- **Plural (singular):** you may spell the number out and drop `{n}`.")
                else:
                    lines.append("- **Plural:** must keep `{n}` — it stands for any count.")
            lines.append("- **Translation:** ")
            lines.append("")
    return "\n".join(lines) + "\n"


def render_skeleton(data: dict) -> str:
    obj = {
        "_comment": "LitClock translation template. Fill EVERY value. Keep the keys exactly. "
        "Validate with: python3 scripts/validate_translation.py <code> --file <this file>",
    }
    for entry in data["keys"]:
        obj[entry["key"]] = ""
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def render_html(data: dict) -> str:
    # Self-defending embed (kit /review): ensure_ascii escapes U+2028/U+2029
    # and all non-ASCII, and "<" -> "\\u003c" prevents a "</script>" in any
    # value from closing the script element. The catalog ban on "<"/">" already
    # blocks this for catalog values, but the hand-authored metadata (surface
    # labels, slot notes) is not gated — belt and suspenders.
    blob = json.dumps(data, ensure_ascii=True).replace("<", "\\u003c")
    # The JS lint is a deliberately non-authoritative pre-check; the Python
    # gate (scripts/validate_translation.py, run in CI) is the final word.
    return _HTML_SHELL.replace("/*__KIT_DATA__*/null", blob)


_HTML_SHELL = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LitClock translation tool</title>
<style>
  :root { font-family: system-ui, -apple-system, sans-serif; }
  body { margin: 0; background: #f8fafc; color: #1e293b; }
  header { position: sticky; top: 0; background: #1e293b; color: #fff; padding: 12px 20px; z-index: 5; }
  header h1 { margin: 0 0 4px; font-size: 18px; }
  header .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; font-size: 14px; }
  header input, header select { font-size: 14px; padding: 4px 8px; border-radius: 6px; border: 1px solid #94a3b8; }
  header button { font-size: 14px; padding: 6px 14px; border-radius: 6px; border: 0; background: #2563eb; color: #fff; cursor: pointer; }
  header button:disabled { background: #64748b; cursor: not-allowed; }
  .count { font-variant-numeric: tabular-nums; }
  main { padding: 16px 20px 80px; max-width: 900px; margin: 0 auto; }
  .surface { font-size: 13px; text-transform: uppercase; letter-spacing: .05em; color: #64748b; margin: 24px 0 4px; }
  .surface-note { font-size: 13px; color: #64748b; margin: 0 0 8px; }
  .key { background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 12px 14px; margin: 10px 0; }
  .key.err { border-color: #dc2626; }
  .key.done { border-color: #16a34a; }
  .keyid { font-family: ui-monospace, monospace; font-size: 12px; color: #64748b; }
  .eng { margin: 4px 0 8px; }
  .meta { font-size: 12px; color: #475569; margin: 0 0 8px; }
  .meta code { background: #f1f5f9; padding: 1px 4px; border-radius: 4px; }
  textarea { width: 100%; box-sizing: border-box; font-size: 15px; padding: 8px; border: 1px solid #cbd5e1; border-radius: 6px; resize: vertical; min-height: 40px; }
  .findings { color: #dc2626; font-size: 13px; margin: 6px 0 0; }
  .disclaimer { background: #fef9c3; border: 1px solid #fde047; border-radius: 8px; padding: 10px 14px; font-size: 13px; margin: 12px 0; }
</style></head><body>
<header>
  <h1>LitClock translation tool</h1>
  <div class="row">
    <label>Language code <input id="code" size="4" placeholder="es"></label>
    <span class="count"><span id="done">0</span> / <span id="total">0</span> filled</span>
    <span class="count"><span id="errs">0</span> issues</span>
    <button id="export" disabled>Download strings.json</button>
    <input type="file" id="import" accept=".json" title="Load a partial translation to continue">
  </div>
</header>
<main>
  <div class="disclaimer">
    This tool checks the mechanical rules (placeholders, formatting tags, forbidden
    characters) as you type. It is a helper, not the final word — the project's
    automated check (<code>scripts/validate_translation.py</code>) runs the same
    rules from the single source and is authoritative. Fill every box, download
    the file, then run that check before opening a pull request.
  </div>
  <div id="rows"></div>
</main>
<script>
const DATA = /*__KIT_DATA__*/null;
const values = {};
const TOKEN_RE = /\{(\/?)(b|em|code)\}/g;
const SLOT_RE = /\{([a-z][a-z0-9_]*)\}/g;
const CONFUSABLE = [..."｛｝❴❵⦃⦄"];

function slotCounts(s) {
  const c = {}; let m;
  SLOT_RE.lastIndex = 0;
  while ((m = SLOT_RE.exec(s))) { if (!["b","em","code"].includes(m[1])) c[m[1]] = (c[m[1]]||0)+1; }
  return c;
}
// "Any visible character" — the negation of Python's invisible set
// (unicodedata categories Cf, Zs, Cc, Zl, Zp) plus the three non-category
// blanks Python lists explicitly (braille blank, hangul fillers). If a value
// matches NOTHING here, every character is invisible. \p{} escapes need the
// u flag and cover Cf codepoints (e.g. U+061C) a hardcoded range would miss.
const VISIBLE = /[^\p{Cf}\p{Zs}\p{Cc}\p{Zl}\p{Zp}\u200b\u2800\u3164\uffa0]/u;
// Non-authoritative mirror of catalog_lint.value_errors (src/catalog_lint.py).
// The Python validator is authoritative; this exists for instant feedback and
// is kept in lock-step (test_translator_kit.py runs BOTH over the same cases).
function lint(entry, val) {
  const out = [];
  if (val === "") return out;                // untouched = "not done yet"
  if (val.trim() === "") return out;         // pure whitespace: not done (matches Python's value.strip() skip)
  if (/[<>]/.test(val)) out.push("remove < or > (use the formatting tags instead)");
  if (/&(?:[a-zA-Z][a-zA-Z0-9]*|#[0-9]+|#[xX][0-9a-fA-F]+);/.test(val)) out.push("write the character itself, not an HTML entity like &lt;");
  if (CONFUSABLE.some(ch => val.includes(ch))) out.push("use normal { } braces, not full-width ｛ ｝");
  for (const ch of val) { const cp = ch.codePointAt(0);
    if ((cp>=0x202A&&cp<=0x202E)||(cp>=0x2066&&cp<=0x2069)) { out.push("remove the hidden text-direction character"); break; }
  }
  for (const ch of val) { const cp = ch.codePointAt(0);
    if ((cp < 0x20 && ch !== "\n") || cp === 0x7f) { out.push("remove the control character (only line breaks are allowed)"); break; }
  }
  if (!VISIBLE.test(val)) out.push("this looks blank — it has no visible letters");
  // Rich tokens: balance + nesting + vocabulary (mirrors the Python stack).
  const allowed = new Set(entry.rich.allowed);
  const stack = []; let m; TOKEN_RE.lastIndex = 0;
  while ((m = TOKEN_RE.exec(val))) {
    if (!allowed.has(m[2])) { out.push("formatting tag {"+m[1]+m[2]+"} is not allowed on this line"); continue; }
    if (!m[1]) stack.push(m[2]); else if (stack.pop() !== m[2]) out.push("formatting tags are not closed in order");
  }
  if (stack.length) out.push("a formatting tag is not closed");
  // Slot counts — the exact Python relax/sibling arithmetic.
  const en = {}; entry.slots.forEach(s => en[s.name] = s.count);
  const got = slotCounts(val);
  const plural = entry.plural;
  const relax = new Set(plural && plural.may_drop_n ? ["n"] : []);
  const siblingAllowed = new Set(plural ? plural.sibling_slots : []);
  const enAdj = Object.assign({}, en), gotAdj = Object.assign({}, got);
  for (const name of relax) { delete enAdj[name]; delete gotAdj[name]; }
  for (const name of siblingAllowed) { if (!(name in en)) delete gotAdj[name]; }
  for (const [name, cnt] of Object.entries(enAdj)) {
    if ((gotAdj[name]||0) < cnt) out.push("missing placeholder {"+name+"}");
  }
  for (const [name, cnt] of Object.entries(gotAdj)) {
    if (cnt > (enAdj[name]||0)) out.push("unexpected placeholder {"+name+"} (check spelling / how many times it appears)");
  }
  // Stray braces left after removing the allowed tokens and slots — mirrors
  // the Python residue check (catches {typo} and a token this line can't use).
  let residue = val;
  for (const t of entry.rich.allowed) residue = residue.split("{"+t+"}").join("").split("{/"+t+"}").join("");
  const allowedNames = new Set([...Object.keys(en), ...siblingAllowed, ...relax]);
  for (const name of allowedNames) residue = residue.split("{"+name+"}").join("");
  if (/[{}]/.test(residue)) out.push("stray { or } — check your placeholders");
  return out;
}
function refresh() {
  let done = 0, errs = 0;
  for (const entry of DATA.keys) {
    const v = values[entry.key] || "";
    if (v.trim()) done++;
    const f = lint(entry, v);
    if (f.length) errs++;
    const row = document.getElementById("row-" + entry.key);
    row.className = "key" + (f.length ? " err" : v.trim() ? " done" : "");
    row.querySelector(".findings").textContent = f.join(" · ");
  }
  document.getElementById("done").textContent = done;
  document.getElementById("errs").textContent = errs;
  document.getElementById("export").disabled = !(done > 0);
}
function build() {
  document.getElementById("total").textContent = DATA.keys.length;
  const rows = document.getElementById("rows");
  let lastSurface = null;
  for (const entry of DATA.keys) {
    if (entry.surface !== lastSurface) {
      lastSurface = entry.surface;
      const h = document.createElement("div"); h.className = "surface"; h.textContent = entry.surface; rows.appendChild(h);
      const n = document.createElement("div"); n.className = "surface-note"; n.textContent = entry.surface_note; rows.appendChild(n);
    }
    const div = document.createElement("div"); div.className = "key"; div.id = "row-" + entry.key;
    const meta = [];
    if (entry.length_sensitive) meta.push("Keep short (physical screen, English is " + entry.en_length + " chars)");
    if (entry.slots.length) meta.push("Placeholders: " + entry.slots.map(s => "<code>{"+escapeHtml(s.name)+"}</code>" + (s.count>1?"×"+s.count:"") + " = " + escapeHtml(s.note)).join("; "));
    if (entry.rich.allowed.length) meta.push("Formatting: " + escapeHtml(entry.rich.help.join(", ")));
    if (entry.plural) meta.push(entry.plural.may_drop_n ? "Plural: may spell the number and drop {n}" : "Plural: keep {n}");
    div.innerHTML = '<div class="keyid">' + escapeHtml(entry.key) + '</div><div class="eng">' + escapeHtml(entry.english) +
      '</div>' + (meta.length ? '<div class="meta">' + meta.join(" · ") + '</div>' : '') +
      '<textarea rows="1"></textarea><div class="findings"></div>';
    const ta = div.querySelector("textarea");
    ta.addEventListener("input", () => { values[entry.key] = ta.value; refresh(); });
    rows.appendChild(div);
  }
  refresh();
}
function escapeHtml(s){return s.replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
document.getElementById("export").addEventListener("click", () => {
  const code = (document.getElementById("code").value || "xx").trim();
  const obj = { _comment: "LitClock " + code + " translation. Validate: python3 scripts/validate_translation.py " + code + " --file <this file>" };
  for (const entry of DATA.keys) obj[entry.key] = values[entry.key] || "";
  const blob = new Blob([JSON.stringify(obj, null, 2) + "\n"], {type:"application/json"});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "strings.json"; a.click();
});
document.getElementById("import").addEventListener("change", (ev) => {
  const file = ev.target.files[0]; if (!file) return;
  const r = new FileReader();
  r.onload = () => { try { const o = JSON.parse(r.result);
    for (const entry of DATA.keys) if (typeof o[entry.key] === "string") values[entry.key] = o[entry.key];
    document.querySelectorAll("#rows .key").forEach(row => { const k = row.id.slice(4); const ta = row.querySelector("textarea"); if (ta) ta.value = values[k] || ""; });
    refresh();
  } catch (e) { alert("Could not read that file: " + e.message); } };
  r.readAsText(file);
});
build();
</script>
</body></html>
"""


def _artifacts() -> dict[Path, str]:
    data = build_data()
    return {
        _OUT / "kit-data.json": json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        _OUT / "worksheet.md": render_worksheet(data),
        _OUT / "strings.template.json": render_skeleton(data),
        _OUT / "tool" / "index.html": render_html(data),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the LitClock translator kit.")
    parser.add_argument("--check", action="store_true", help="fail if committed artifacts are stale")
    args = parser.parse_args(argv)

    artifacts = _artifacts()
    if args.check:
        stale = []
        for path, content in artifacts.items():
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                stale.append(path.relative_to(_REPO_ROOT))
        if stale:
            print("stale translator-kit artifacts (run scripts/build_translator_kit.py):", file=sys.stderr)
            for p in stale:
                print(f"  {p}", file=sys.stderr)
            return 1
        print("translator kit is up to date.")
        return 0

    for path, content in artifacts.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
