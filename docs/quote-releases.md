# Quote image releases

Quote images (`images/quote_HHMM_N.png` and `images/metadata/*`) are **not** stored in git. They live as a GitHub Release asset under a tag of the form `litclock-images-vN` and are fetched on demand by:

- `scripts/update.sh` — during an in-place update on an existing device (Phase 2c)
- `.github/workflows/build-image.yml` — when pi-gen bakes an OS image

The repo pins which release is current via `.images-version` at the repo root. A single-line file, just the version (e.g. `v2`). Bump it in a reviewed PR whenever the canonical image set should change.

## When to cut a new release

Rare. The quote set turns over infrequently. Typical triggers:

- Adding or correcting a quote (edited `image-gen/litclock_annotated.csv` → regenerated images via `php image-gen/quote_to_image.php`)
- Regenerating after a font or layout change in `image-gen/quote_to_image.php`
- Retagging quotes (see issue #192 for the gold-set audit flow)

The #192 audit tooling can find more changes; folding multiple small corrections into a single `v2` bump is fine and encouraged. One release per quote edit is wasteful.

## Release process

1. **Make and commit the source-of-truth changes** on a branch.
   - `image-gen/litclock_annotated.csv` edits
   - Any regenerated images under `images/` (run `cd image-gen && php quote_to_image.php`)
   - Push to a PR. Do **not** bump `.images-version` yet.

2. **Cut the release artifact on your dev box** from the branch tip.

   ```bash
   # From a clean working tree on the branch
   scripts/release_images.sh v2
   ```

   This tars the current `images/` directory, computes SHA256, and uploads both assets to `litclock-images-v2`. Refuses to run with an uncommitted working tree, and refuses to overwrite an existing release.

3. **Bump `.images-version`** in a follow-up commit on the same branch.

   ```bash
   echo "v2" > .images-version
   git add .images-version
   git commit -m "chore(images): bump .images-version to v2"
   ```

4. **Push and merge the PR.**
   CI on the merge commit reads the new `.images-version` via `.github/workflows/build-image.yml`'s pre-flight check. If the release artifact from step 2 is missing, the pre-flight fails fast with a clear error.

5. **Cut a new OS image tag so fresh SD flashes ship the new quotes.**
   Either push a `v*` tag (if you're also cutting a code release), or manually trigger `build-image.yml` via the GitHub Actions UI. The resulting `.img.xz` release bakes in the new image set.

### Why steps 2 and 3 are separate

Release creation (`gh release create`) is a side effect on GitHub. Bumping `.images-version` is a source-of-truth change in the repo. Separating them keeps the Git history reviewable in isolation ("this PR shipped image corpus v2") and lets PR review catch a wrong version number before the release is visible to fresh installs.

### Why the tag matters, and why you should still cut an OS image

Deployed Pis do pick up quote changes on their own: `litclock-update.timer` runs `update.sh` weekly, and its Phase 2c calls `download_images.sh`, which syncs the image set to whatever `.images-version` pins.

The catch is *which* commit they read that from. `update.sh` does not track `master` — it resolves the highest semver tag and `git reset --hard`s onto that SHA. So a `.images-version` bump sitting on `master` reaches nobody until a `v*` tag contains it. **Merging the bump is not shipping it; tagging is.**

Cutting a fresh OS image is a separate benefit: it bakes the new quote set into the `.img.xz` so a newly flashed card is correct before it ever reaches the network, rather than downloading the corpus on first update.

## Auth while the repo is private

Until the repo flips to public, release asset downloads require authentication. `scripts/download_images.sh` and the build-image workflow honor two env vars:

- `GH_TOKEN` (preferred)
- `GITHUB_TOKEN` (also accepted — GitHub Actions sets this automatically)

Set whichever is convenient before running `update.sh` on the test Pi:

```bash
export GH_TOKEN=ghp_yourpersonaltokenhere
scripts/update.sh
```

Once the repo is public, the conditional auth becomes a no-op: unauthenticated curl works via the public S3 redirects that GitHub issues for public release assets, and neither env var is required.

## Rollback

If a v2 release introduces a problem:

1. Edit `.images-version` back to `v1` on a branch.
2. Open a PR, merge.
3. **Cut a `v*` tag containing the revert.** Deployed Pis resolve the highest semver tag, so a revert that only lands on `master` reaches none of them — they keep running v2 until a tag carries the revert. This step is the rollback; the merge alone is not.
4. Trigger `build-image.yml` to cut a fresh OS image pinned to v1, so newly flashed cards are correct too.

The v2 release itself can stay on GitHub — nothing consumes it unless a `.images-version` commit points at it.

## Local verification before tagging

Before cutting a release, verify `download_images.sh` will accept the tarball:

```bash
# Make the tarball + sha locally (without uploading)
mkdir -p /tmp/litclock-release
tar -czf /tmp/litclock-release/litclock-images.tar.gz -C . images
cd /tmp/litclock-release && sha256sum litclock-images.tar.gz > litclock-images.tar.gz.sha256

# Run the pytest suite
cd -
python3 -m pytest tests/test_download_images.py tests/test_release_images.py -q
```

All green means the script will happily consume what `scripts/release_images.sh` produces.
