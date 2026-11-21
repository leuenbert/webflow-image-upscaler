#!/usr/bin/env bash
# Set up everything inside this project folder:
#   .venv/              Python venv with Pillow, requests, tqdm (no PyTorch)
#   tools/realesrgan/   portable Real-ESRGAN ncnn-vulkan macOS build (pinned + checksum-verified)
# Deleting the project folder removes everything; nothing is installed system-wide.
# Prerequisite (installed by you): cwebp, e.g. `brew install webp`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RELEASE_TAG="v0.2.5.0"
ZIP_NAME="realesrgan-ncnn-vulkan-20220424-macos.zip"
ZIP_URL="https://github.com/xinntao/Real-ESRGAN/releases/download/${RELEASE_TAG}/${ZIP_NAME}"
ZIP_SHA256="e0ad05580abfeb25f8d8fb55aaf7bedf552c375b5b4d9bd3c8d59764d2cc333a"
TOOLS="$ROOT/tools"
DEST="$TOOLS/realesrgan"

# --- Python ------------------------------------------------------------------
PY=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    PY="$(command -v "$cand")"; break
  fi
done
[ -n "$PY" ] || { echo "Python 3.11+ is required (python.org installer)." >&2; exit 1; }
echo "==> Python: $PY ($("$PY" --version))"

if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e .
echo "==> venv ready: $ROOT/.venv"

# --- Real-ESRGAN -------------------------------------------------------------
mkdir -p "$TOOLS"
ZIP="$TOOLS/$ZIP_NAME"
if [ -f "$ZIP" ] && [ "$(shasum -a 256 "$ZIP" | cut -d' ' -f1)" != "$ZIP_SHA256" ]; then
  echo "==> cached zip has the wrong checksum; downloading again"; rm -f "$ZIP"
fi
if [ ! -f "$ZIP" ]; then
  echo "==> downloading $ZIP_URL"
  curl -fL --retry 3 -o "$ZIP.part" "$ZIP_URL"
  mv "$ZIP.part" "$ZIP"
fi
GOT="$(shasum -a 256 "$ZIP" | cut -d' ' -f1)"
if [ "$GOT" != "$ZIP_SHA256" ]; then
  echo "Checksum mismatch for $ZIP_NAME: got $GOT, expected $ZIP_SHA256" >&2; exit 1
fi
unzip -tq "$ZIP" >/dev/null
echo "==> zip verified (sha256 $ZIP_SHA256)"

if [ ! -x "$DEST/realesrgan-ncnn-vulkan" ]; then
  rm -rf "$DEST"; mkdir -p "$DEST"
  unzip -q "$ZIP" -d "$DEST"
  # the zip may contain a top-level folder; flatten it
  if [ ! -f "$DEST/realesrgan-ncnn-vulkan" ]; then
    inner="$(dirname "$(find "$DEST" -name realesrgan-ncnn-vulkan -type f | head -n1)")"
    [ -n "$inner" ] && [ "$inner" != "$DEST" ] && mv "$inner"/* "$DEST"/ && rmdir "$inner" || true
  fi
  rm -f "$DEST/onepiece_demo.mp4"
fi
chmod +x "$DEST/realesrgan-ncnn-vulkan"
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
[ -f "$DEST/models/realesrgan-x4plus.bin" ] || { echo "model realesrgan-x4plus missing in $DEST/models" >&2; exit 1; }

# --- cwebp (prerequisite, not downloaded) ----------------------------------------
command -v cwebp >/dev/null || { echo "cwebp not found. Install it first: brew install webp" >&2; exit 1; }
echo "==> using $(command -v cwebp) ($(cwebp -version | head -1))"

# --- smoke test ---------------------------------------------------------------
echo "==> smoke test (realesrgan-x4plus on the bundled sample)"
SMOKE_OUT="$TOOLS/smoke_test_x4.png"
rm -f "$SMOKE_OUT"
( cd "$DEST" && ./realesrgan-ncnn-vulkan -i input.jpg -o "$SMOKE_OUT" -n realesrgan-x4plus -s 4 -m models ) >/dev/null 2>"$TOOLS/smoke_test.log" || {
  echo "Real-ESRGAN smoke test failed; see $TOOLS/smoke_test.log" >&2; exit 1; }
.venv/bin/python - "$DEST/input.jpg" "$SMOKE_OUT" <<'EOF'
import sys
from PIL import Image
a, b = Image.open(sys.argv[1]), Image.open(sys.argv[2])
assert b.size == (a.width * 4, a.height * 4), (a.size, b.size)
print(f"    {a.size[0]}x{a.size[1]} -> {b.size[0]}x{b.size[1]} OK")
EOF
rm -f "$SMOKE_OUT" "$TOOLS/smoke_test.log"
echo "==> setup complete. Next:  .venv/bin/python -m upscale run --limit 3"
