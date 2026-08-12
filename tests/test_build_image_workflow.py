"""Tests for the build-image GitHub Actions workflow.

Validates workflow structure, triggers, and build configuration.
"""

import os
import re
import subprocess

import yaml

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "build-image.yml")

# Every spelling of "the SHA the workflow FILE came from". On a
# workflow_dispatch that is master, not the litclock_ref that got checked out.
UNTRUSTWORTHY_SHA = re.compile(r"GITHUB_SHA|github\.sha|github\.event\.[\w.]*\bsha\b")


def _load_workflow():
    with open(WORKFLOW_PATH) as f:
        wf = yaml.safe_load(f)
    # PyYAML parses the YAML keyword `on:` as boolean True.
    # Normalize so tests can use wf["on"] regardless.
    if True in wf and "on" not in wf:
        wf["on"] = wf.pop(True)
    return wf


def _step(name):
    wf = _load_workflow()
    for step in wf["jobs"]["build"]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"{name} step not found in build-image.yml")


def _strip_comments(script):
    """Drop full-line shell comments.

    Without this, commenting out `RESOLVED_SHA="$(git rev-parse ...)"` and
    reverting the behaviour elsewhere left every assertion here green — the
    positive check was satisfied by the corpse of the line it was guarding.
    """
    return "\n".join(ln for ln in script.splitlines() if not ln.strip().startswith("#"))


def _step_text(name):
    """Everything in a step that can set a value: `run` AND `env`.

    Reading only `run` was defeatable: a new `env:` entry could reintroduce
    GITHUB_SHA with the whole suite green, because nothing ever looked there.
    """
    step = _step(name)
    env = step.get("env") or {}
    return _strip_comments(step.get("run", "")) + "\n" + "\n".join(f"{k}={v}" for k, v in env.items())


def _version_step_script():
    """The `run:` body of the Determine version step, comments stripped."""
    return _strip_comments(_step("Determine version")["run"])


class TestVersionStampProvenance:
    """The version/SHA stamp must describe the code actually built.

    On workflow_dispatch, GITHUB_SHA is the SHA of the ref the workflow FILE
    came from (master), not the litclock_ref that got checked out. Using it
    stamped dev images of a feature branch with master's SHA, in both the
    release asset name and the git_sha= line of /etc/litclock-version — so a QA
    artifact could not be traced to the code inside it.
    """

    def test_sha_comes_from_the_checked_out_head(self):
        script = _version_step_script()
        assert "git rev-parse HEAD" in script, (
            "the version stamp must resolve HEAD after checkout, not trust GITHUB_SHA"
        )
        assert "--short" not in script, (
            "`git rev-parse --short=7` returns a MINIMUM of 7 chars and lengthens on collision "
            "or with core.abbrev set; slice the full SHA instead so the tag and asset name are deterministic"
        )

    def test_github_sha_is_not_used_for_the_stamp(self):
        offenders = [ln.strip() for ln in _step_text("Determine version").splitlines() if UNTRUSTWORTHY_SHA.search(ln)]
        assert not offenders, (
            f"the workflow-file SHA was reintroduced into the version stamp: {offenders}. "
            f"On workflow_dispatch it is master's SHA, not the checked-out ref."
        )

    def test_resolved_shas_are_assigned_exactly_once(self):
        """The negative check above scans for a known-bad token, so it cannot
        see a REASSIGNMENT that launders the same value through a different
        expression. Verified defeat: appending a second
        `RESOLVED_SHA="$(git rev-parse --short=7 origin/master)"` restored the
        bug with the whole suite green. Pin the count instead."""
        script = _version_step_script()
        for var, expected in (
            ("RESOLVED_SHA", 'RESOLVED_SHA="${RESOLVED_FULL_SHA:0:7}"'),
            ("RESOLVED_FULL_SHA", 'RESOLVED_FULL_SHA="$(git rev-parse HEAD)"'),
        ):
            # `export`/`declare`/`readonly`/`+=` are the same defeat one keyword
            # wider than the bare reassignment the docstring describes.
            assigns = re.findall(rf"^\s*(?:export\s+|declare\s+-\w+\s+|readonly\s+)?{var}\+?=.*$", script, re.MULTILINE)
            assert len(assigns) == 1, f"{var} is assigned {len(assigns)} times: {assigns}. Exactly one is safe."
            assert assigns[0].strip() == expected, f"{var} no longer resolves the checked-out HEAD: {assigns[0]!r}"

    def test_full_sha_is_published_and_is_not_the_short_form(self):
        """`gh release create --target` takes a branch name or a FULL commit
        SHA; a 7-char abbreviation is not guaranteed to resolve. The two
        outputs must stay distinct, or --target silently gets the short one."""
        script = _version_step_script()
        assert 'echo "full_sha=${RESOLVED_FULL_SHA}"' in script, "full_sha is not published as a step output"
        assert 'echo "sha=${RESOLVED_SHA}"' in script, "sha is not published as a step output"
        assert "--short" not in re.search(r"^\s*RESOLVED_FULL_SHA=.*$", script, re.MULTILINE).group(0), (
            "RESOLVED_FULL_SHA must not be abbreviated — --target needs the full SHA"
        )

    def test_checkout_precedes_the_version_step(self):
        """git rev-parse only resolves the right commit if checkout ran first."""
        steps = _load_workflow()["jobs"]["build"]["steps"]
        checkout = next(i for i, s in enumerate(steps) if "actions/checkout" in str(s.get("uses", "")))
        version = next(i for i, s in enumerate(steps) if s.get("name") == "Determine version")
        assert checkout < version, "Determine version must run AFTER checkout or HEAD is not yet the target ref"

    def test_checkout_actually_fetches_the_dispatch_ref(self):
        """Ordering is not enough — the whole fix rests on HEAD being the
        dispatched ref, which is the checkout step's `with: ref:`.

        Verified defeat: deleting that `with:` block entirely left all workflow
        tests green while HEAD silently reverted to master on workflow_dispatch,
        fully restoring the bug this PR fixed. The same deletion drops
        `submodules: true`, which is the lib/e-Paper driver — an image that
        boots, serves the PWA, and never paints a quote.
        """
        steps = _load_workflow()["jobs"]["build"]["steps"]
        checkout = next(s for s in steps if "actions/checkout" in str(s.get("uses", "")))
        with_block = checkout.get("with") or {}
        assert with_block.get("ref") == "${{ github.event.inputs.litclock_ref || github.ref }}", (
            f"checkout no longer resolves the dispatch ref: {with_block.get('ref')!r}"
        )
        assert with_block.get("submodules") is True, (
            "checkout lost `submodules: true` — the waveshare driver lives in the lib/e-Paper submodule"
        )

    def test_the_image_sha_stamp_comes_from_the_resolved_short_sha(self):
        """LITCLOCK_SHA is the line that produces `git_sha=` in
        /etc/litclock-version — the exact field this PR's rationale names as
        having been mislabelled. Repointing it at outputs.ref (a branch name) or
        outputs.version survived green, reintroducing an untraceable QA
        artifact."""
        step = _step("Configure pi-gen")
        run = _strip_comments(step["run"])
        env = step.get("env") or {}
        assigns = re.findall(r"^\s*printf\s+'LITCLOCK_SHA=%q\\n'\s+\"([^\"]*)\"", run, re.MULTILINE)
        assert len(assigns) == 1, f"LITCLOCK_SHA is written {len(assigns)} times: {assigns}"
        var = re.fullmatch(r"\$\{(\w+)\}", assigns[0])
        assert var, f"LITCLOCK_SHA={assigns[0]!r}; expected a shell variable bound via env:"
        assert env.get(var.group(1)) == "${{ steps.version.outputs.sha }}", (
            f"LITCLOCK_SHA resolves to {env.get(var.group(1))!r}, not the resolved short SHA"
        )

    def test_dispatch_input_never_reaches_a_shell_string_inline(self):
        """`${{ }}` is spliced in before bash parses the line, so an input of
        `master$(...)` executes. Every step that consumes the dispatch ref must
        take it through `env:` and reference a quoted shell variable.

        The runner holds contents:write, id-token:write and attestations:write,
        so this reaches all the way to signing and publishing a release image.
        """
        offenders = []
        for step in _load_workflow()["jobs"]["build"]["steps"]:
            run = step.get("run")
            if not run:
                continue
            for hit in re.findall(r"\$\{\{\s*([^}]+?)\s*\}\}", _strip_comments(run)):
                # Any ref- or input-derived value, not just the two spellings
                # that were being abused. Scoping this to `inputs`/`outputs.ref`
                # quietly blessed the sinks left behind: outputs.version is
                # github.ref_name with a leading v stripped on the tag path, and
                # it flows on into outputs.img. Same sink class, one hop later.
                if re.search(r"\binputs\b|outputs\.(ref|version|img|sha)\b|github\.ref_name", hit):
                    offenders.append(f"{step.get('name', '?')}: {hit}")
        assert not offenders, (
            f"dispatch-derived values interpolated directly into a shell script: {offenders}. "
            f'Pass them through the step\'s env: block and reference "$VAR".'
        )

    def test_dev_prefix_preserved(self):
        """`dev-` is load-bearing: the stale-release cleanup selects on it, and
        the OTA resolver filters tags to vMAJOR.MINOR.PATCH so dev tags stay
        invisible to fielded clocks."""
        assert 'VERSION="dev-$(date +%Y%m%d)-' in _version_step_script()


class TestWorkflowTriggers:
    def test_triggers_on_version_tags(self):
        wf = _load_workflow()
        tags = wf["on"]["push"]["tags"]
        assert "v*" in tags

    def test_supports_manual_dispatch(self):
        wf = _load_workflow()
        assert "workflow_dispatch" in wf["on"]

    def test_manual_dispatch_has_ref_input(self):
        wf = _load_workflow()
        inputs = wf["on"]["workflow_dispatch"]["inputs"]
        assert "litclock_ref" in inputs


class TestWorkflowBuildJob:
    def test_build_job_exists(self):
        wf = _load_workflow()
        assert "build" in wf["jobs"]

    def test_runs_on_ubuntu(self):
        wf = _load_workflow()
        assert "ubuntu" in wf["jobs"]["build"]["runs-on"]

    def test_has_write_permissions(self):
        """Needs contents:write to create GitHub Releases."""
        wf = _load_workflow()
        perms = wf["jobs"]["build"]["permissions"]
        assert perms["contents"] == "write"

    def test_frees_disk_space(self):
        """pi-gen needs ~10 GB; runner must free space first."""
        wf = _load_workflow()
        steps = wf["jobs"]["build"]["steps"]
        step_names = [s.get("name", "") for s in steps]
        assert any("disk" in name.lower() for name in step_names)

    def test_clones_pi_gen_at_pinned_tag(self):
        """pi-gen should be cloned at a specific tag, not HEAD."""
        wf = _load_workflow()
        steps = wf["jobs"]["build"]["steps"]
        clone_step = next(s for s in steps if s.get("name") == "Clone pi-gen")
        assert "--branch" in clone_step["run"]

    def test_compresses_with_xz(self):
        wf = _load_workflow()
        steps = wf["jobs"]["build"]["steps"]
        compress_step = next(s for s in steps if s.get("name") == "Compress image")
        assert "xz" in compress_step["run"]

    def test_generates_sha256_checksum(self):
        wf = _load_workflow()
        steps = wf["jobs"]["build"]["steps"]
        compress_step = next(s for s in steps if s.get("name") == "Compress image")
        assert "sha256sum" in compress_step["run"]

    def test_creates_release_on_tag(self):
        wf = _load_workflow()
        steps = wf["jobs"]["build"]["steps"]
        release_step = next(s for s in steps if s.get("name") == "Create release")
        assert "gh release create" in release_step["run"]
        assert release_step["if"] == "github.ref_type == 'tag'"

    def test_uploads_dev_build_on_dispatch(self):
        wf = _load_workflow()
        steps = wf["jobs"]["build"]["steps"]
        dev_step = next(s for s in steps if s.get("name") == "Upload dev build")
        assert dev_step["if"] == "github.ref_type != 'tag'"
        assert "gh release create" in dev_step["run"]
        assert "--prerelease" in dev_step["run"]


class TestBuildIsGatedOnTests:
    """The build workflow must actually run the image-correctness tests.

    This is a single `build` job with no `needs:`, so for a while it ran no
    tests at all — which meant moving the [Install]-validity check out of
    05-smoke-test and into pytest quietly moved it off the release path
    entirely. A `WnatedBy=` typo would have shipped on a tag push.
    """

    def _steps(self):
        return _load_workflow()["jobs"]["build"]["steps"]

    def test_pytest_runs_during_the_build(self):
        runs = " ".join(s.get("run", "") for s in self._steps())
        assert "pytest" in runs, "the build workflow runs no tests; pytest-side gates do not gate anything"
        assert "tests/test_pi_gen.py" in runs, (
            "the build does not run tests/test_pi_gen.py, which is the image-correctness gate"
        )

    def test_the_test_step_can_actually_fail_the_build(self):
        """Substring checks are satisfied by the TEXT of the command, not its
        effect. Verified: appending `|| true` to the pytest line left this class
        green while the step could no longer fail anything — and this step is the
        only thing putting TestInstallSectionValidity back on the release path.
        `continue-on-error: true`, `if: false`, and a narrowing `-k` all defeat
        it identically."""
        step = next(s for s in self._steps() if "-m pytest" in s.get("run", ""))
        assert not step.get("continue-on-error"), "the test step is continue-on-error; it cannot fail the build"
        assert "if" not in step, f"the test step is conditional (if: {step.get('if')!r}); it may be skipped"
        run = step["run"]
        # `-m pytest`, not `pytest`: the step also pip-installs pytest, and
        # matching the install line meant `|| true` on the actual invocation
        # went unchecked. Verified defeat.
        pytest_line = next(ln for ln in run.splitlines() if "-m pytest" in ln)
        for swallow in ("|| true", "|| :", "|| echo", "; true"):
            assert swallow not in pytest_line, f"pytest failure is swallowed by {swallow!r}"
        assert "set +e" not in run, "set +e disables failure propagation in the test step"
        for narrowing in (" -k ", "--deselect", "--ignore"):
            assert narrowing not in pytest_line, (
                f"the test step narrows its selection with {narrowing!r}; the gate may skip the validity test"
            )

    def test_the_test_step_runs_before_the_expensive_build(self):
        """A broken tree must fail in ~1 minute, not at minute 38 of a 40-minute
        build."""
        names = [s.get("name", "") for s in self._steps()]
        runs = [s.get("run", "") for s in self._steps()]
        test_idx = next(i for i, r in enumerate(runs) if "pytest" in r)
        build_idx = next(i for i, n in enumerate(names) if n == "Build image")
        assert test_idx < build_idx, f"test step (index {test_idx}) runs after Build image (index {build_idx})"


class TestReleaseTargetCommitish:
    """Both release steps must stamp target_commitish with the SHA that was
    actually built.

    The dev step is the one that was wrong: it runs on `github.ref_type !=
    'tag'`, i.e. exactly the workflow_dispatch path, where github.sha is the
    SHA of the ref the workflow FILE came from. So a dev build produced an
    image stamped with the right branch SHA and a Release pointing at master.
    The tag step happened to be correct — on a tag push the two SHAs coincide —
    but it is held to the same rule so there is one rule rather than two and a
    footnote about which path each is safe on.
    """

    RELEASE_STEPS = ("Upload dev build", "Create release")

    def test_both_release_steps_target_the_resolved_full_sha(self):
        """Resolves through `env:`.

        The values are passed via env rather than spliced inline with `${{ }}`,
        because a neighbouring interpolation in the same string carried the
        dispatch input and was a shell-injection sink. So the assertion has to
        follow both hops: the --target argument must be a shell variable, and
        that variable's env entry must bind to the resolved full SHA.
        """
        for name in self.RELEASE_STEPS:
            step = _step(name)
            env = step.get("env") or {}
            run = _strip_comments(step["run"])
            targets = re.findall(r"--target\s+\"([^\"]+)\"", run)
            assert targets, f"{name} does not pass --target; GitHub would store the default branch name"
            for t in targets:
                var = re.fullmatch(r"\$\{(\w+)\}", t)
                assert var, f"{name} targets {t!r}; expected a shell variable bound via env:"
                bound = env.get(var.group(1))
                assert bound == "${{ steps.version.outputs.full_sha }}", (
                    f"{name}: env {var.group(1)} is {bound!r}, not the resolved full SHA"
                )

    def test_no_release_step_uses_the_workflow_file_sha(self):
        for name in self.RELEASE_STEPS:
            offenders = [ln.strip() for ln in _step_text(name).splitlines() if UNTRUSTWORTHY_SHA.search(ln)]
            assert not offenders, f"{name} reintroduced the workflow-file SHA: {offenders}"

    def test_the_workflow_never_references_github_sha_anywhere(self):
        """The broadest form of the guard, and the one that would have caught
        the missed instance: `7dcfc182` fixed the version stamp and left
        `--target ${{ github.sha }}` in the dev step untouched, because every
        assertion was scoped to a single step. Nothing in this workflow has a
        legitimate use for github.sha — the checked-out HEAD is always the
        honest answer. If one ever appears, this test is the place to justify
        it explicitly rather than let it in silently."""
        with open(WORKFLOW_PATH) as f:
            lines = f.read().splitlines()
        offenders = [ln.strip() for ln in lines if not ln.strip().startswith("#") and UNTRUSTWORTHY_SHA.search(ln)]
        assert not offenders, (
            f"github.sha / GITHUB_SHA used in build-image.yml: {offenders}. On workflow_dispatch it is "
            f"the SHA of the ref the workflow FILE came from, not the code being built."
        )


class TestCheckoutDoesNotShipCredentials:
    """litclock-dev#551 — the build copies the working tree (.git included) into the
    image rootfs, so checkout must not persist the job token into .git/config.

    `cp -a .` runs mid-job, before actions/checkout's post-step credential
    cleanup, and nothing strips .git afterwards. `.git` is deliberately kept
    (README Option 2 documents /home/pi/litclock as a build-from-source
    checkout), so the fix is to never write the credential.
    """

    def test_checkout_sets_persist_credentials_false(self):
        wf = _load_workflow()
        checkouts = [
            s
            for s in wf["jobs"]["build"]["steps"]
            if isinstance(s.get("uses"), str) and s["uses"].startswith("actions/checkout@")
        ]
        assert checkouts, "no actions/checkout step found in build-image.yml"
        for step in checkouts:
            with_block = step.get("with") or {}
            assert "persist-credentials" in with_block, (
                "actions/checkout must set persist-credentials — the tree "
                "(including .git) is copied into the image rootfs (litclock-dev#551)"
            )
            value = with_block["persist-credentials"]
            # PyYAML gives a bool for `false`; accept the string spelling too.
            assert value is False or str(value).strip().lower() == "false", (
                f"persist-credentials must be false, got {value!r}"
            )

    def test_tree_copy_still_happens_after_checkout(self):
        """Anchors WHY the above matters. If the copy step is ever removed or
        renamed, this test should be revisited rather than silently passing."""
        wf = _load_workflow()
        scripts = " ".join(s.get("run", "") for s in wf["jobs"]["build"]["steps"] if isinstance(s.get("run"), str))
        assert "cp -a . /tmp/pi-gen/litclock-src" in scripts, (
            "the tree-copy step changed — re-check whether .git still reaches the image rootfs (litclock-dev#551)"
        )

    def test_no_step_strips_dot_git_so_the_guard_is_load_bearing(self):
        """If someone later strips .git from the staged tree, persist-credentials
        stops being the only defence and this suite should say so."""
        wf = _load_workflow()
        scripts = " ".join(s.get("run", "") for s in wf["jobs"]["build"]["steps"] if isinstance(s.get("run"), str))
        strips = re.search(r"rm -rf\s+[^\s]*litclock-src/\.git\b", scripts)
        assert not strips, (
            "a step now strips .git from the staged tree — persist-credentials "
            "is no longer the sole defence; update litclock-dev#551's reasoning"
        )


class TestConfigSourcingInjection:
    """The pi-gen config file is SOURCED (stage3/04-finalize/00-run.sh), so a
    ref-derived value appended to it unquoted re-parses as shell — the same
    injection class `env:` closed for `${{ }}` splicing, reintroduced one layer
    down at the config-file boundary (litclock-dev#617). Git ref names legitimately
    allow `;`, `$`, `(` and `)`, and the build job holds contents:write +
    id-token:write + attestations:write.

    These tests execute the actual workflow script text, not a description of
    it: reverting `printf %q` to a bare echo, or deleting the allowlist, fails
    them functionally.
    """

    BASH = ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c"]

    def _run(self, script, env_overrides, cwd=REPO_ROOT):
        env = dict(os.environ, **env_overrides)
        return subprocess.run(self.BASH + [script], env=env, cwd=cwd, capture_output=True, text=True)

    def _run_version_step(self, tmp_path, ref_type, ref_name, dispatch, name="gh_output"):
        """Run the Determine version step with a given ref shape.

        Returns (proc, gh_output_text). Both allowlist tests thread the same
        four env vars through here so a new variable added to the step lands in
        one place, not two.
        """
        out = tmp_path / name
        out.touch()
        proc = self._run(
            _version_step_script(),
            {"REF_TYPE": ref_type, "REF_NAME": ref_name, "DISPATCH_REF": dispatch, "GITHUB_OUTPUT": str(out)},
        )
        return proc, out.read_text()

    def test_config_appends_survive_sourcing_with_a_hostile_ref(self, tmp_path):
        """Extract the append lines from Configure pi-gen, run them with a
        ref name that is valid to git but hostile to a sourced file, source
        the result, and check nothing executed and the value round-tripped."""
        run_body = _strip_comments(_step("Configure pi-gen")["run"])
        config = tmp_path / "config"
        appends = "\n".join(
            ln.replace("/tmp/pi-gen/config", str(config))
            for ln in run_body.splitlines()
            if ">> /tmp/pi-gen/config" in ln
        )
        for var in ("LITCLOCK_REF", "LITCLOCK_VERSION", "LITCLOCK_SHA"):
            assert var in appends, f"config append for {var} not found in Configure pi-gen"

        pwned = tmp_path / "pwned"
        hostile = f"v1.0$(touch {pwned});id"
        proc = self._run(appends, {"REF": hostile, "VERSION": hostile, "SHA": hostile})
        assert proc.returncode == 0, proc.stderr

        # Sourcing the config must neither execute anything nor mangle the value.
        proc = self._run(f'. "{config}" && printf %s "$LITCLOCK_REF"', {})
        assert not pwned.exists(), (
            "sourcing the pi-gen config executed a command embedded in the ref "
            "name — the append lines are no longer shell-quoted (litclock-dev#617)"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == hostile, f"value did not round-trip through the sourced config: {proc.stdout!r}"

    def test_version_step_rejects_hostile_tag(self, tmp_path):
        """A tag like v1.0$(id) must fail the build in Determine version,
        before it can become a release title, asset name, or config line."""
        proc, text = self._run_version_step(tmp_path, "tag", "v1.0$(id)", "master")
        assert proc.returncode != 0, "hostile tag name passed Determine version"
        assert "characters outside" in proc.stderr
        assert "version=" not in text, "outputs were written before the validation failed"

    def test_version_step_rejects_hostile_dispatch_ref(self, tmp_path):
        """The workflow_dispatch litclock_ref is free attacker text (a branch
        that exists checks out fine), and on the branch path REF=DISPATCH_REF.
        It must be validated too — not just the tag path."""
        proc, text = self._run_version_step(tmp_path, "branch", "master", "master$(id)")
        assert proc.returncode != 0, "hostile dispatch ref passed Determine version"
        assert "characters outside" in proc.stderr
        assert "ref=" not in text, "outputs were written before the validation failed"

    def test_version_step_rejects_a_slashed_tag_at_the_version_field(self, tmp_path):
        """VERSION becomes the image filename litclock-<VERSION>.img and the
        release tag, so it must reject '/' even though REF (branch names) allows
        it. A tag v1.0/x strips to VERSION=1.0/x and must fail closed."""
        proc, text = self._run_version_step(tmp_path, "tag", "v1.0/x", "master")
        assert proc.returncode != 0, "slashed tag produced a slashed VERSION"
        assert "version" in proc.stderr
        assert "version=" not in text

    def test_version_step_rejects_a_bare_v_tag(self, tmp_path):
        """The '+' in the allowlist is deliberate: a tag literally named 'v'
        (matched by the v* trigger) strips to an empty VERSION, which would
        ship an image named litclock-.img. Empty must fail closed."""
        proc, text = self._run_version_step(tmp_path, "tag", "v", "master")
        assert proc.returncode != 0, "empty VERSION from a bare 'v' tag was accepted"
        assert "version=" not in text

    def test_version_step_accepts_our_real_ref_shapes_with_exact_outputs(self, tmp_path):
        """The allowlist must not reject anything we actually name, AND the
        outputs must carry the right values — asserting presence alone let a
        dropped `#v` strip or a REF/DISPATCH_REF swap pass silently."""
        # Tag path: VERSION is REF_NAME with the leading v stripped; REF == REF_NAME.
        proc, text = self._run_version_step(tmp_path, "tag", "v0.223.0", "master", name="gh_tag")
        assert proc.returncode == 0, proc.stderr
        assert "version=0.223.0" in text.splitlines(), text
        assert "ref=v0.223.0" in text.splitlines(), text

        # Branch (dispatch) path: REF == DISPATCH_REF (slashed branches allowed);
        # VERSION is a dev- stamp. Pin REF exactly and VERSION's shape.
        proc, text = self._run_version_step(tmp_path, "branch", "master", "feat/runtime-render-release", name="gh_br")
        assert proc.returncode == 0, proc.stderr
        assert "ref=feat/runtime-render-release" in text.splitlines(), text
        version_line = next(ln for ln in text.splitlines() if ln.startswith("version="))
        assert re.fullmatch(r"version=dev-\d{8}-[0-9a-f]{7}", version_line), version_line
