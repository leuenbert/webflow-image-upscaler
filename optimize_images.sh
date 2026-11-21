#!/usr/bin/env bash
#
# optimize_images.sh — convert photos to web-ready WebP, sorted by resolution.
#
# For every JPG/PNG/TIFF in the input folder (subfolders included):
#   1. Convert colors to sRGB (the web's standard color space)
#   2. Downscale so the longest edge is at most MAX_EDGE px (never upscales)
#   3. Encode to WebP with high-quality lossy settings
#   4. Strip ALL metadata (EXIF, GPS, camera info, XMP, ICC profile)
#   5. Sort the result into one of three output folders:
#        LOWRES     original's longest edge is below Full HD (< 1920 px)
#        HIGHRES    output was capped at MAX_EDGE (original >= 2560 px)
#        NORMALRES  everything in between (1920 – 2559 px)
#
# Uses only macOS built-in `sips` plus `cwebp` (brew install webp).
#
# Usage:
#   ./optimize_images.sh [input_dir] [output_dir]
#   MAX_EDGE=2560 FULLHD_EDGE=1920 QUALITY=82 ./optimize_images.sh
#
# Defaults: input = ./UNPROCESSED, output = ./OUTPUT
# Output files go flat into OUTPUT/{LOWRES,NORMALRES,HIGHRES}/<name>.webp.
# Files whose .webp is already newer than the source are skipped.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INPUT_DIR="${1:-$SCRIPT_DIR/UNPROCESSED}"
OUTPUT_DIR="${2:-$SCRIPT_DIR/OUTPUT}"
MAX_EDGE="${MAX_EDGE:-2560}"         # longest side of output in px
FULLHD_EDGE="${FULLHD_EDGE:-1920}"   # below this (longest side) = LOWRES
QUALITY="${QUALITY:-82}"             # WebP quality 0-100
SRGB_PROFILE="/System/Library/ColorSync/Profiles/sRGB Profile.icc"

command -v cwebp >/dev/null || { echo "cwebp not found. Install with: brew install webp" >&2; exit 1; }
command -v sips  >/dev/null || { echo "sips not found (this script needs macOS)." >&2; exit 1; }
[ -d "$INPUT_DIR" ] || { echo "Input folder not found: $INPUT_DIR" >&2; exit 1; }

mkdir -p "$OUTPUT_DIR/LOWRES" "$OUTPUT_DIR/NORMALRES" "$OUTPUT_DIR/HIGHRES"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

human() { awk -v b="$1" 'BEGIN{ split("B KB MB GB",u); i=1; while(b>=1024&&i<4){b/=1024;i++} printf "%.1f %s", b, u[i] }'; }

total_in=0; total_out=0; count=0; skipped=0; failed=0
n_low=0; n_normal=0; n_high=0
CLAIMED="$TMP_DIR/claimed.txt"; : > "$CLAIMED"   # output paths used in this run

while IFS= read -r -d '' src; do
  rel="${src#"$INPUT_DIR"/}"
  base="$(basename "${src%.*}")"
  ext="${src##*.}"

  # Original dimensions decide both resizing and the target folder.
  w=$(sips -g pixelWidth  "$src" 2>/dev/null | awk '/pixelWidth/  {print $2}')
  h=$(sips -g pixelHeight "$src" 2>/dev/null | awk '/pixelHeight/ {print $2}')
  if [ -z "$w" ] || [ -z "$h" ]; then
    echo "FAILED (unreadable): $rel" >&2; failed=$((failed + 1)); continue
  fi
  longest=$(( w > h ? w : h ))

  if   [ "$longest" -lt "$FULLHD_EDGE" ]; then category=LOWRES
  elif [ "$longest" -ge "$MAX_EDGE" ];    then category=HIGHRES
  else                                         category=NORMALRES
  fi

  # Flat output: if two sources share a name (e.g. photo.jpg + photo.tif),
  # the second one gets its extension appended (photo-tif.webp).
  dest="$OUTPUT_DIR/$category/$base.webp"
  if grep -Fxq "$dest" "$CLAIMED"; then
    dest="$OUTPUT_DIR/$category/$base-$(echo "$ext" | tr '[:upper:]' '[:lower:]').webp"
  fi
  echo "$dest" >> "$CLAIMED"

  if [ -f "$dest" ] && [ "$dest" -nt "$src" ]; then
    skipped=$((skipped + 1)); continue
  fi

  resize_args=()
  [ "$longest" -gt "$MAX_EDGE" ] && resize_args=(-Z "$MAX_EDGE")

  # Step 1+2: color-convert to sRGB and resize into a lossless PNG intermediate.
  tmp="$TMP_DIR/work.png"
  if ! sips -m "$SRGB_PROFILE" ${resize_args[@]+"${resize_args[@]}"} -s format png "$src" --out "$tmp" >/dev/null 2>&1; then
    echo "FAILED (sips): $rel" >&2; failed=$((failed + 1)); continue
  fi

  # Step 3+4: encode WebP, drop every bit of metadata.
  #   -q              quality
  #   -m 6            slowest/best compression method (smaller files, same quality)
  #   -sharp_yuv      sharper, more accurate color edges when converting RGB->YUV
  #   -metadata none  strip EXIF, XMP and ICC
  #   -mt             use multiple threads
  if ! cwebp -quiet -q "$QUALITY" -m 6 -sharp_yuv -metadata none -mt "$tmp" -o "$dest"; then
    echo "FAILED (cwebp): $rel" >&2; failed=$((failed + 1)); continue
  fi

  case "$category" in
    LOWRES) n_low=$((n_low + 1)) ;; NORMALRES) n_normal=$((n_normal + 1)) ;; HIGHRES) n_high=$((n_high + 1)) ;;
  esac
  in_size=$(stat -f%z "$src"); out_size=$(stat -f%z "$dest")
  total_in=$((total_in + in_size)); total_out=$((total_out + out_size)); count=$((count + 1))
  printf '%-10s %-45s %5sx%-5s %10s -> %9s\n' "$category" "$rel" "$w" "$h" "$(human "$in_size")" "$(human "$out_size")"
done < <(find "$INPUT_DIR" -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.tif' -o -iname '*.tiff' \) -print0 | sort -z)

echo
echo "Converted: $count  (LOWRES $n_low, NORMALRES $n_normal, HIGHRES $n_high)"
echo "Skipped (up to date): $skipped   Failed: $failed"
if [ "$count" -gt 0 ]; then
  echo "Total: $(human "$total_in") -> $(human "$total_out")  ($(awk -v a="$total_in" -v b="$total_out" 'BEGIN{printf "%.1f", 100*(1-b/a)}')% smaller)"
fi
echo "Output: $OUTPUT_DIR/{LOWRES,NORMALRES,HIGHRES}"
