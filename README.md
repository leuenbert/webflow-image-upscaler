# Webflow image upscaler

Upscales every image in the Webflow "Projects" CMS collection to 3440 px with Real-ESRGAN (x4plus) and
writes an upload index + runbook for Claude in Chrome. See `PLAN_webflow_upscale.md` for the full design.

## Requirements

Install these yourself before running setup:

- **macOS** (Apple Silicon or Intel)
- **Python 3.11+** (from python.org or Homebrew)
- **cwebp**, the WebP encoder: `brew install webp`. The pipeline uses the `cwebp` on your PATH; to use a
  different one, set `cwebp` in `config.toml` to its full path.

`scripts/setup.sh` downloads only Real-ESRGAN (pinned release, checksum-verified) into `tools/`, because
there is no Homebrew package for it.

## Usage

Put the Webflow CSV export of the collection at `input/projects.csv`. It isn't committed, because it holds
client data.

```sh
./scripts/setup.sh                              # venv + Real-ESRGAN in tools/ + smoke test
.venv/bin/python -m upscale run --limit 3       # test on the first 3 items
open review.html                                # check for AI artifacts (use "100% crop")
.venv/bin/python -m upscale run                 # everything (~20 s per image on Apple Silicon)
.venv/bin/python -m upscale status
.venv/bin/python -m upscale mark "<slot_id>" uploaded [--note "..."]   # globs allowed
```

Stages can also be run one at a time: `parse`, `download`, `upscale`, `encode`, `verify`, `index`. All
take `--only-slug SLUG` (repeatable), `--limit N` and `--dry-run`, and all of them can be resumed. The
state is kept in `cache/manifest.json`.

**High-res originals:** put the photographer's files in `originals_override/`. A file matches when its
name stem matches the decoded CDN filename (e.g. `IMG_7041.jpg`). Put a file in
`originals_override/<slug>/` when the stem is ambiguous (e.g. `image_1`). Otherwise, set
`manual_override` in `originals_map.csv`. A value of `none` disables a wrong match.

**Texture:** Real-ESRGAN smooths fine surfaces. The `[texture]` section in `config.toml` blends the real
source back in and sharpens the result (blend 0.36, sharpen 60, no grain). Changing these settings only
re-runs the encode step.

**Output:** the files to upload are WebP, at `output/<slug>/<slug>--<field>[-NN].webp`, 3440 px wide and at most
3.5 MB. They are encoded with `cwebp` using the settings from `optimize_images.sh` (q82, `-m 6 -sharp_yuv
-metadata none`); the quality drops below 82 only when a file would exceed the limit. `cache/encoded/` also
holds a q95 JPEG master of each image for reference. It has no size limit and is never uploaded.

Settings are in `config.toml`. To remove everything, delete the folder.
