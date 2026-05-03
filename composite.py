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
    (180, 20,  1440, 270),
    (180, 310, 1440, 270),
    (180, 600, 1440, 270),
    (180, 890, 1440, 270),
]

GRID_ZONES = [
    # 2×2 grid
    # Each zone: (x, y, width, height)
    (40,  40,  860, 550),
    (900, 40,  860, 550),
    (40,  610, 860, 550),
    (900, 610, 860, 550),
]


def build_strip(raw_paths: list[str], overlay_path: str | None, output_path: str):
    """
    Composite 4 photos into a strip or grid layout with an overlay.

    Args:
        raw_paths:    List of 4 captured JPEG paths.
        overlay_path: Path to the overlay PNG (or None).
        output_path:  Where to save the final composited JPEG.
    """
    canvas = Image.new('RGB', CANVAS_SIZE, BG_COLOR)

    # Determine layout from overlay filename
    zones = GRID_ZONES  # default
    if overlay_path:
        name = Path(overlay_path).stem.lower()
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


def generate_test_overlay(output_path: str, style: str = 'classic'):
    """
    Generate a simple test overlay PNG for development.
    Run this directly to create overlays/ files for testing:

        python -c "from composite import generate_test_overlay; \\
                   generate_test_overlay('overlays/overlay_classic.png', 'classic')"
    """
    overlay = Image.new('RGBA', CANVAS_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    if style == 'classic':
        # Simple dark border
        border = 40
        # Draw border by filling corners/edges with opaque black
        draw.rectangle([0, 0, CANVAS_W, border], fill=(20, 20, 22, 255))
        draw.rectangle([0, CANVAS_H - border, CANVAS_W, CANVAS_H], fill=(20, 20, 22, 255))
        draw.rectangle([0, 0, border, CANVAS_H], fill=(20, 20, 22, 255))
        draw.rectangle([CANVAS_W - border, 0, CANVAS_W, CANVAS_H], fill=(20, 20, 22, 255))
        # Event text at bottom
        draw.text((CANVAS_W // 2, CANVAS_H - 20), "PHOTOBOOTH",
                  fill=(200, 200, 195, 180), anchor='mb')

    elif style == 'gold':
        # Corner accents
        corner_len = 120
        corner_w = 6
        gold = (245, 200, 66, 220)
        # Top-left
        draw.rectangle([30, 30, 30 + corner_len, 30 + corner_w], fill=gold)
        draw.rectangle([30, 30, 30 + corner_w, 30 + corner_len], fill=gold)
        # Top-right
        draw.rectangle([CANVAS_W - 30 - corner_len, 30, CANVAS_W - 30, 30 + corner_w], fill=gold)
        draw.rectangle([CANVAS_W - 30 - corner_w, 30, CANVAS_W - 30, 30 + corner_len], fill=gold)
        # Bottom-left
        draw.rectangle([30, CANVAS_H - 30 - corner_w, 30 + corner_len, CANVAS_H - 30], fill=gold)
        draw.rectangle([30, CANVAS_H - 30 - corner_len, 30 + corner_w, CANVAS_H - 30], fill=gold)
        # Bottom-right
        draw.rectangle([CANVAS_W - 30 - corner_len, CANVAS_H - 30 - corner_w, CANVAS_W - 30, CANVAS_H - 30], fill=gold)
        draw.rectangle([CANVAS_W - 30 - corner_w, CANVAS_H - 30 - corner_len, CANVAS_W - 30, CANVAS_H - 30], fill=gold)

    elif style == 'filmstrip':
        # Sprocket holes + opaque side bars
        bar_w = 80
        draw.rectangle([0, 0, bar_w, CANVAS_H], fill=(20, 20, 22, 255))
        draw.rectangle([CANVAS_W - bar_w, 0, CANVAS_W, CANVAS_H], fill=(20, 20, 22, 255))
        # Sprocket holes (transparent punch-outs in the bars)
        for y in range(20, CANVAS_H, 60):
            draw.rectangle([20, y, 60, y + 30], fill=(0, 0, 0, 0))
            draw.rectangle([CANVAS_W - 60, y, CANVAS_W - 20, y + 30], fill=(0, 0, 0, 0))
        # Divider lines between photo zones
        for y_div in [290, 580, 870]:
            draw.rectangle([bar_w, y_div, CANVAS_W - bar_w, y_div + 20], fill=(20, 20, 22, 255))

    elif style == 'partygrid':
        # Grid lines + diagonal pattern overlay
        mid_x = CANVAS_W // 2
        mid_y = CANVAS_H // 2
        line_w = 20
        draw.rectangle([mid_x - line_w // 2, 0, mid_x + line_w // 2, CANVAS_H], fill=(20, 20, 22, 255))
        draw.rectangle([0, mid_y - line_w // 2, CANVAS_W, mid_y + line_w // 2], fill=(20, 20, 22, 255))
        # Outer border
        border = 20
        draw.rectangle([0, 0, CANVAS_W, border], fill=(20, 20, 22, 255))
        draw.rectangle([0, CANVAS_H - border, CANVAS_W, CANVAS_H], fill=(20, 20, 22, 255))
        draw.rectangle([0, 0, border, CANVAS_H], fill=(20, 20, 22, 255))
        draw.rectangle([CANVAS_W - border, 0, CANVAS_W, CANVAS_H], fill=(20, 20, 22, 255))

    overlay.save(output_path, 'PNG')
    print(f"[OVERLAY] Test overlay generated → {output_path} ({style})")


# ══════════════════════════════════════════════════════════════
#  CLI — generate all test overlays
# ══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys
    out_dir = Path('overlays')
    out_dir.mkdir(exist_ok=True)

    styles = {
        'overlay_classic.png':   'classic',
        'overlay_goldcorners.png': 'gold',
        'overlay_filmstrip.png': 'filmstrip',
        'overlay_partygrid.png': 'partygrid',
    }

    for filename, style in styles.items():
        generate_test_overlay(str(out_dir / filename), style)

    print(f"\nGenerated {len(styles)} test overlays in {out_dir}/")
    print("Replace these with your real Affinity-designed overlays for production.")
