# Translating LitClock

Thank you for helping translate LitClock! This folder is everything you need
to produce one language bundle. You do **not** need to be a programmer.

## What you're producing

One file — `languages/<code>/strings.json` — with a translation for every
piece of text LitClock shows: the WiFi setup page, the phone/desktop control
app, and the messages painted on the physical e-ink screen. `<code>` is the
two-letter language code (Spanish = `es`, German = `de`, …).

## Pick your tool

**Easiest — the fill-in tool.** Open `tool/index.html` in any web browser
(double-click it). Every line shows the English, where it appears, and its
rules. Type your translation; the tool flags mistakes as you go. When you're
done, click **Download strings.json**. You can reload a half-finished file
with the file picker to continue later.

**Prefer plain text?** Copy `strings.template.json` and fill in each value.
`worksheet.md` lists every key with its English text, context, and rules to
read alongside.

## The rules (the automated check enforces these)

1. **Translate every value; keep every key.** Don't add, remove, or rename
   the keys (the part before the `:`).
2. **Copy every `{placeholder}` exactly.** `{network}`, `{n}`, `{error}` and
   friends are filled in by the software — never translate the word inside
   the braces, and keep the same number of them. The worksheet says what each
   one means.
3. **Formatting tags** — some lines allow `{b}…{/b}` (bold), `{em}…{/em}`
   (italic), or `{code}…{/code}` (monospace). Only use the ones a line lists,
   and always close them in order. Every other line is plain text.
4. **No HTML and no `<` or `>`.** If your sentence needs a "less than" sign,
   reword it. Don't use HTML entities like `&lt;` either — write the real
   character.
5. **Use normal `{ }` braces**, not full-width `｛ ｝`, and no hidden
   text-direction characters.
6. **Counts (plural).** English has two forms per count: `.one` (exactly one)
   and `.other` (any other number). On the `.one` form you may spell the
   number out ("hace un minuto") and drop `{n}`; on `.other` keep `{n}` — it
   stands for any number.

**One thing that is _not_ auto-checked: length.** Lines shown on the physical
e-ink screen are width-limited and will clip if they run long. The tool and
worksheet mark these lines and show the English length as a guide, but nothing
measures the real width — so keep splash lines close to the English, and if you
can, check them on a real device. This is guidance, not a gate.

## Which languages this kit can complete today

The kit targets languages that, like English, have **one/other** plural forms —
Spanish, German, French, Italian, Portuguese, Dutch, and many more. Languages
with richer plural systems (Russian, Polish, Arabic, … need `few`/`many`/etc.)
can translate everything *except* the count strings from this kit; the extra
plural forms need a small change on the project side first. The validator will
tell you if your language is one of these — finish the rest and coordinate the
plural keys with the maintainer (litclock-dev#532).

## Check your work before submitting

The validator needs a full checkout of the project (it inspects the app's
source to know which lines allow formatting). From the repository root:

```
python3 scripts/validate_translation.py es --file /path/to/your/strings.json
```

It prints, per line, exactly what the project's automated check (CI) will
report — because it runs the *same* rules. A clean run here means a clean run
there. Fix anything it lists, then open a pull request that adds your file at
`languages/<code>/strings.json`.

## Two things beyond the strings

A complete language also needs a **registry entry** in `languages.json` (the
maintainer adds this — status, plural forms, coverage floor) and its own
**quote corpus** — the literary time-quotes the clock displays, which must be
sourced in your language. The strings in this kit make the *interface*
speak your language; the quote corpus is a separate, larger effort. Talk to
the maintainer about the corpus before starting it.

---

_`worksheet.md`, `strings.template.json`, `kit-data.json`, and `tool/index.html`
are generated from the English catalog by `scripts/build_translator_kit.py`.
Don't hand-edit them — they're rebuilt (and checked in CI) whenever the
English text changes._
