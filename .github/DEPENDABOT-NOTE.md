# Why there is no `dependabot.yml` here

Dependency changes reach this repository by **port from the development repo**,
not by Dependabot raising pull requests here.

Dependabot used to run version updates on this repo, and it duplicated work
rather than adding any. On 2026-09-09 all four of its open PRs (#63-#66) were
byte-for-byte the same four bumps that had already merged upstream days
earlier. Since `requirements.txt`, `requirements-dev.txt` and `package.json`
all arrive by port, those PRs could only ever conflict with the port or become
no-ops — and a change authored *here* flows the wrong way, which is how a lint
fix was once lost between the two repos.

**Security advisories are still surfaced.** Dependabot *alerts* remain enabled
(a repository setting, not a config file), so an advisory is still reported on
the Security tab. What is switched off is automatic PR creation — both routine
version updates and automated security fixes — because nothing here merges
those PRs automatically, so they were work to manage rather than work done.

If an alert appears, fix it upstream and port it, the same as any other change.
