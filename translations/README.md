# Bringing LitClock to your language

Thank you for helping! LitClock tells the time with a line from a book — a
sentence that names the minute, like *"It was nearly half past seven"*. A new
language needs **its own** such lines, from its own literature. That is the
part only a reader of that literature can do, and it is all we ask of you.

**What you supply:**

1. **Quotes** — the time-telling lines, one per row, in a small text file
   (below). This is the real work, and a partial set is welcome.
2. **Three or more farewell lines** — short literary lines the clock shows
   when it is gifted, restarting or switched off.

**What we write:** every piece of the clock's interface — the WiFi setup page,
the phone app, the messages on the screen, the way the date is written. You do
not need to translate any of it. If you would like to read our wording and tell
us where it sounds wrong, that is very welcome, and the last section shows how —
but it is optional.

## The quotes

One file, `languages/<code>/quotes.csv`, where `<code>` is your language's
two-letter code (Spanish = `es`, German = `de`, …). Save it as **UTF-8
without a byte-order mark**, one quote per line, six fields separated by `|`
— a vertical bar, not a comma:

```
HH:MM|time phrase|quote|title|author|flag
07:30|half past seven|It was half past seven and the house was still asleep.|A Novel|A. Author|
```

| Field | What goes there |
|---|---|
| `HH:MM` | The minute the quote names, 24-hour, with ordinary digits: `07:30`, `19:30`. A quote that fits both morning and evening goes in twice. |
| time phrase | The words in the quote that tell the time, copied **exactly** as they appear in it — the clock prints them in bold, and finds them by matching. |
| quote | The sentence itself. Keep it short: a sentence or two. |
| title | The book's title. |
| author | The author's name. |
| flag | `YES` for a quote not suitable for a family room (violence, sex, strong language), otherwise leave it **empty** (`NO` also works). Write exactly `YES` — not the word in your language: the clock reads anything else as "suitable", so a `SÍ` or `oui` would show a mature quote on a filtered clock. |

**Quotation marks.** The file is read as CSV, so a field that contains a
`"` must be wrapped in double quotes, with each `"` inside it doubled. A quote
that opens with dialogue is the usual case:

```
07:30|half past seven|"""Come at half past seven,"" she said."|A Novel|A. Author|
```

Written plainly, as `"Come at half past seven," she said.`, the clock would
silently drop the marks. Typographic quotes (“ ” or « ») need no wrapping at
all, which makes them the easiest choice.

**From a spreadsheet.** Excel's "CSV UTF-8" export will not work as it is: it
separates fields with commas and adds a byte-order mark. LibreOffice Calc can
export it correctly — *File → Save As → Text CSV*, tick *Edit filter
settings*, then character set **UTF-8**, field delimiter **`|`**, string
delimiter **`"`** — and it does the quotation-mark wrapping for you. A plain
text editor works just as well.

**Which alphabets work.** The clock's screen font covers Latin, Greek and
Cyrillic letters; right-to-left scripts (Arabic, Hebrew) and Chinese, Japanese
and Korean are not supported yet. The screen has no fallback font, so every
character must be one it has — including spaces: use an ordinary space where
French typography puts a narrow one before `;:!?`.

**Every minute does not need a quote.** A minute with none shows the time in
plain numerals — never an English quote. A language goes live on clocks once
its quotes cover enough of the day (80% of the minutes today); below that it
is still worth sending what you have, because others can build on it.

**Copyright.** Quotes are short excerpts with the book named, from works you
may lawfully quote — and a translation has its own copyright, separate from
the book's. [CONTRIBUTING.md](../CONTRIBUTING.md) ("Quote and translation
contributions") explains what can be accepted. Name the edition each quote
comes from in your pull request; it is the one fact nobody can recover later.
Your file is shared under the same licence as the English quotes,
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/): anyone
may use and adapt it with credit, never commercially, and on the same terms.

### Check your file

From a checkout of the project, with four Python packages installed
(`pip install freetype-py pillow requests "fonttools[woff]"`):

```
python3 tools/render_invariants.py check --language <code>
```

It reads `languages/<code>/quotes.csv`, draws every quote the way the clock
would, and reports any row it cannot accept — a time that is not a real minute,
a time phrase that is empty or missing from its quote, a flag other than `YES`,
`NO` or empty, a line with other than five or six fields (the flag may be
left off), quotation marks the clock would drop, or a character the screen
cannot draw. It also prints your coverage: the share of the day's minutes with a
suitable quote, the share between 07:00 and 23:00, and the longest stretch
without one — which tells you where more quotes would help most. The project's
automated check (CI) runs the same command on your pull request — and fails it
if a CSV under `languages/` sits anywhere but `languages/<code>/quotes.csv`,
so a misnamed file is reported rather than skipped.

## The farewell lines

Three moments, at least one line each:

- **welcome** — shown when the clock is prepared as a gift, before its new
  owner first turns it on;
- **restarting** — shown while it restarts;
- **switched off** — left on the screen once it is off.

Each is one short line from your language's literature, with its author —
not a translation of the English ones (that raises copyright questions a
translation cannot settle). More than one per moment is welcome; the clock
picks among them. Put them in your pull request's description.

## Optional: reading our interface wording

The interface text for your language lives in `languages/<code>/strings.json`
once we have written it. To read it alongside the English and what each line
is for, open `tool/index.html` from this folder in any browser and load that
file with its file picker — every line shows the English, where it appears,
and its rules. Comments in the pull request, or an issue, are the best way to
send corrections.

---

## For the maintainer: adding a language

Everything below is the project side. The kit in this folder is the
maintainer's authoring tool for the strings bundle (and a reading aid for a
reviewer, above).

### Writing the strings bundle

`languages/<code>/strings.json` holds a value for every key in the English
catalog. Author it with `tool/index.html` (type each line; **Download
strings.json** at the end; reload a half-finished file to continue), or copy
`strings.template.json` and read `worksheet.md` alongside — it lists every key
with its English, context and rules. The rules, all enforced by the check:

1. **Every key, no others.** Keys are never added, removed or renamed, except
   the farewell lines below.
2. **Every `{placeholder}` copied exactly** — `{network}`, `{n}`, `{error}` —
   never translated, the same number of them.
3. **Formatting tags** `{b}…{/b}`, `{em}…{/em}`, `{code}…{/code}` only where a
   line lists them, closed in order. Every other line is plain text.
4. **No HTML, no `<` or `>`**, no entities like `&lt;` — reword instead.
5. **Plain `{ }` braces**, not full-width `｛ ｝`, and no hidden text-direction
   characters.
6. **Plurals.** Each count has `.one` and `.other`. `.one` may spell the number
   out and drop `{n}`; `.other` keeps `{n}`.

**Fonts.** The control app's web fonts are Latin-1 subsets, narrower than the
screen's: Spanish, French, German, Italian, Portuguese, Dutch and the
Scandinavian languages are covered; Polish (ą, ł), Czech, Turkish, Greek and
Cyrillic letters would show in a fallback font, and the check reports each as
`FONT`. Such a language needs a font change in the app first. Screen strings
are checked character by character against the screen's fonts, which have no
fallback at all.

**The farewell lines** are `shutdown.farewell.welcome.N`, `.reboot.N` and
`.poweroff.N`, from the volunteer. `.1` is required per screen; any more are
optional — blank, or numbered past English's five.

**The date on the clock face** is built from the strings, not a locale:
`clock.masthead.date` is the pattern (English `{weekday}, {month} {day}` →
"Wed, September 30"), and `clock.weekday.*`, `clock.month.*` and
`clock.day.1`…`clock.day.31` fill it — each in the form the pattern needs
(abbreviated or full, the right grammatical case and capitalisation, the day as
the language writes it: "5", "05", "1er"). This line **is** measured: every
weekday × month × day must fit 443 pixels at the date size, and the test suite
fails the language if one does not.

**Other screen lines are not measured.** They are width-limited and clip if
they run long; the tool and worksheet mark them and show the English length.
Keep them close to it, and look at them on a device.

**Plural systems beyond one/other** (Russian, Polish, Czech, Arabic, … need
`few`, `many` and others) are not supported by the kit or the catalog yet; the
check says so for those codes. That support is its own piece of work, built
when a volunteer for such a language appears.

### Checking it

```
python3 scripts/validate_translation.py <code>
```

prints, per key, what CI reports — the same rules, from the same module.
`--all` in place of the code checks **every** `languages/*/strings.json` in
the tree, registered or not; the test suite runs exactly that, so a bundle
cannot skip the gate by not being listed yet.

### The registry

`languages.json` at the repository root lists the languages the clock knows.
The maintainer edits it — a volunteer's pull request adds only
`languages/<code>/quotes.csv`, which CI checks as it stands (by that
conventional path) until the language is registered.

| Field | Meaning |
|---|---|
| `code` | The language code, the same as the entry's key and its directory: lower-case, with at most one subtag (`es`, `pt-BR`). The CI corpus list refuses anything else. |
| `native_name` | The name in the language itself, as the app's language picker shows it. |
| `status` | `incubating` (checked by CI, not selectable on a clock) or `active` (selectable). Nothing else. |
| `curator` | Who looks after the corpus. |
| `corpus.path` | `languages/<code>/<name>.csv` — keep the volunteer's `quotes.csv` where it is. |
| `corpus.rows`, `corpus.sfw_coverage_pct` | The row count and suitable-minute coverage the clock actually reads. Tests measure both and fail when the recorded values drift. |
| `min_coverage_pct` | The coverage an `active` language must reach (80 by default). |
| `fonts` | Which fonts draw it (`builtin-literata`). |
| `strings` | `languages/<code>/strings.json`. |
| `nsfw_tagged` | Whether the corpus marks mature quotes. |
| `plural_forms` | The plural categories its strings carry: `["one", "other"]`. |

The order of events: the volunteer's corpus lands at
`languages/<code>/quotes.csv`; we write the strings bundle and add the entry as
`incubating` in the same pull request; the entry flips to `active` once the
coverage floor is met and the language has been checked on a real clock.

---

_`worksheet.md`, `strings.template.json`, `kit-data.json`, and `tool/index.html`
are generated from the English catalog by `scripts/build_translator_kit.py`.
Don't hand-edit them — they're rebuilt (and checked in CI) whenever the
English text changes._
