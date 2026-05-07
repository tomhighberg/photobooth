"""
PhotoBooth — composite.py
Pillow-based image compositing for photo booth output.

Two main functions:
  build_single  — one photo + overlay → 4×6 print
  build_strip   — four photos + overlay → 4×6 print (strip or 2×2 grid)

All overlays are PNG with transparency.  The photo sits BEHIND the
overlay, so opaque regions of the overlay frame/border cover the photo
edges.  Transparent regions show the photo through.

Output canvas: 1800 × 1200 px at 300 DPI  →  4 × 6 inches.
The CP1500 prints 4×6 natively, so this maps 1:1 with no driver scaling.
"""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter


# ── Constants ──

CANVAS_W = 1800
CANVAS_H = 1200
CANVAS_SIZE = (CANVAS_W, CANVAS_H)
BG_COLOR = (20, 20, 22)          # near-black background
JPEG_QUALITY = 92


# ══════════════════════════════════════════════════════════════
#  build_single
#  One raw photo composited with an overlay onto a 4×6 canvas.
# ══════════════════════════════════════════════════════════════

def build_single(raw_path: str, overlay_path: str | None, output_path: str):
    """
    Composite a single photo with an overlay.

    Args:
        raw_path:     Path to the captured JPEG from the camera.
        overlay_path: Path to the overlay PNG (or None for no overlay).
        output_path:  Where to save the final composited JPEG.
    """
    canvas = Image.new('RGB', CANVAS_SIZE, BG_COLOR)

    # Load and resize the photo to fill the canvas
    photo = Image.open(raw_path).convert('RGB')
    photo = _cover_fit(photo, CANVAS_W, CANVAS_H)

    # Place the photo
    canvas.paste(photo, (0, 0))

    # Apply overlay on top
    if overlay_path and Path(overlay_path).exists():
        overlay = Image.open(overlay_path).convert('RGBA')
        overlay = overlay.resize(CANVAS_SIZE, Image.LANCZOS)
        canvas.paste(overlay, (0, 0), mask=overlay)

    canvas.save(output_path, 'JPEG', quality=JPEG_QUALITY)
    print(f"[COMPOSITE] Single → {output_path}")


# ══════════════════════════════════════════════════════════════
#  build_strip
#  Four raw photos arranged in a layout, composited with overlay.
#  Layout is determined by the overlay design:
#    - Vertical strip (1×4)  for film-strip style
#    - Grid (2×2)            for party-grid style
#  The overlay's transparent zones define where photos show through.
#  We detect layout from the overlay filename or fall back to grid.
# ══════════════════════════════════════════════════════════════

# Photo placement zones — coordinates within the 1800×1200 canvas.
# Adjust these to match your Affinity overlay designs.

STRIP_ZONES = [
    # Vertical strip: 4 landscape photos stacked
    # Each zone: (x, y, width, height)
    (0,    0, 1200, 450),
    (0,  450, 1200, 450),
    (0,  900, 1200, 450),
    (0, 1350, 1200, 450),
]

GRID_ZONES = [
    # 2×2 grid
    # Each zone: (x, y, width, height)
    (0,   0,   900, 600),
    (900, 0,   900, 600),
    (0,   600, 900, 600),
    (900, 600, 900, 600),
]


def build_strip(raw_paths: list[str], overlay_path: str | None, output_path: str):
    """
    Composite 4 photos into a strip or grid layout with an overlay.

    Args:
        raw_paths:    List of 4 captured JPEG paths.
        overlay_path: Path to the overlay PNG (or None).
        output_path:  Where to save the final composited JPEG.
    """
    # allow film strip to be portrait orientation instead of landscape
    name = Path(overlay_path).stem.lower()
    if 'strip' in name or 'film' in name:
        zones = STRIP_ZONES
        canvas_size = (1200, 1800)
    else:
        zones = GRID_ZONES
        canvas_size = CANVAS_SIZE  # 1800×1200

    canvas = Image.new('RGB', canvas_size, BG_COLOR)

    # Determine layout from overlay filename
    zones = GRID_ZONES  # default
    if overlay_path:
        if 'strip' in name or 'film' in name:
            zones = STRIP_ZONES

    # Place each photo in its zone
    for i, raw_path in enumerate(raw_paths[:4]):
        if not Path(raw_path).exists():
            continue
        zone = zones[i] if i < len(zones) else zones[-1]
        x, y, w, h = zone

        photo = Image.open(raw_path).convert('RGB')
        photo = _cover_fit(photo, w, h)
        canvas.paste(photo, (x, y))

    # Apply overlay on top
    if overlay_path and Path(overlay_path).exists():
        overlay = Image.open(overlay_path).convert('RGBA')
        overlay = overlay.resize(CANVAS_SIZE, Image.LANCZOS)
        canvas.paste(overlay, (0, 0), mask=overlay)

    canvas.save(output_path, 'JPEG', quality=JPEG_QUALITY)
    print(f"[COMPOSITE] Strip/Grid → {output_path}")


# ══════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════

def _cover_fit(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """
    Resize and centre-crop an image to exactly fill target_w × target_h,
    like CSS `object-fit: cover`.
    """
    src_w, src_h = img.size
    src_ratio = src_w / src_h
    tgt_ratio = target_w / target_h

    if src_ratio > tgt_ratio:
        # Source is wider — scale by height, crop sides
        new_h = target_h
        new_w = int(src_w * (target_h / src_h))
    else:
        # Source is taller — scale by width, crop top/bottom
        new_w = target_w
        new_h = int(src_h * (target_w / src_w))

    img = img.resize((new_w, new_h), Image.LANCZOS)

    # Centre crop
    left = (new_w - target_w) // 2
    top  = (new_h - target_h) // 2
    img = img.crop((left, top, left + target_w, top + target_h))

    return img

