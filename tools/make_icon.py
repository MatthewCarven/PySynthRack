"""Build the app icon from Matthew's screenshot plus a drawn small glyph.

    python tools/make_icon.py          (needs Pillow: pip install pillow)

Writes ``packaging/icon.ico`` (every size Windows asks for) and
``packaging/icon.png`` (256 px, for anything that wants a PNG).

A screenshot of the rack reads beautifully large and turns to grey mush at
taskbar sizes -- the text and knob values can't survive 16 px. An ``.ico``
holds separate artwork per size, so each size gets what reads there:

* 256 / 128 -- the node canvas from ``packaging/icon_source.png``, cropped
  past the title bar, toolbar and hint line.
* 64 / 48  -- a tighter crop on the nodes and their cables.
* 32 / 24 / 16 -- a drawn glyph in the app's own colours (sampled from the
  screenshot): two nodes, one with the selected-node blue title bar, joined
  by a cable between yellow jacks. No text.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "packaging" / "icon_source.png"
OUT_ICO = ROOT / "packaging" / "icon.ico"
OUT_PNG = ROOT / "packaging" / "icon.png"

# Sampled from icon_source.png.
CANVAS = (35, 35, 35)
GRID = (46, 46, 46)
NODE = (62, 62, 62)
NODE_EDGE = (100, 100, 100)
TITLE = (37, 37, 38)
BLUE = (30, 101, 150)
JACK = (199, 199, 41)
CABLE = (208, 208, 208)

# Source-pixel crop boxes (left, top, right, bottom), each square.
BIG_CROP = (0, 105, 680, 785)       # the canvas below the hint line
MID_CROP = (20, 262, 540, 782)      # oscillator + both LFOs and their cables

SS = 8  # supersampling factor for the drawn glyph


def _rounded_mask(size: int, radius_frac: float) -> Image.Image:
    """An anti-aliased rounded-square alpha mask."""
    big = size * SS
    m = Image.new("L", (big, big), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        (0, 0, big - 1, big - 1), radius=int(big * radius_frac), fill=255)
    return m.resize((size, size), Image.LANCZOS)


def crop_icon(src: Image.Image, box, size: int) -> Image.Image:
    im = src.crop(box).resize((size, size), Image.LANCZOS)
    im.putalpha(_rounded_mask(size, 0.10))
    return im


def glyph(size: int) -> Image.Image:
    """Two nodes and a cable, drawn at ``size`` px (supersampled)."""
    s = size * SS
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    def px(v: float) -> int:
        return int(round(v * s))

    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=px(0.14), fill=CANVAS)
    if size >= 24:                       # the canvas grid, where it reads
        for g in (0.25, 0.5, 0.75):
            d.line((px(g), 0, px(g), s), fill=GRID, width=max(1, SS // 2))
            d.line((0, px(g), s, px(g)), fill=GRID, width=max(1, SS // 2))

    edge = max(SS, px(0.03))
    title_h = 0.17

    def node(x0, y0, x1, y1, title_fill):
        d.rectangle((px(x0), px(y0), px(x1), px(y1)), fill=NODE,
                    outline=NODE_EDGE, width=edge)
        d.rectangle((px(x0), px(y0), px(x1), px(y0 + title_h)),
                    fill=title_fill, outline=NODE_EDGE, width=edge)

    # Upper-left node (plain title) and lower-right node (selected: blue).
    a = (0.07, 0.07, 0.43, 0.52)
    b = (0.57, 0.48, 0.93, 0.93)
    node(*a, TITLE)
    node(*b, BLUE)

    # Cable: out of A's right-edge jack, looping down into B's left-edge
    # jack -- the same droop the editor draws.
    out_j = (a[2], 0.40)
    in_j = (b[0], 0.66)
    pts = []
    c1 = (out_j[0] + 0.16, out_j[1])
    c2 = (in_j[0] - 0.16, in_j[1])
    for i in range(33):
        t = i / 32
        u = 1 - t
        x = u**3 * out_j[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t**3 * in_j[0]
        y = u**3 * out_j[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t**3 * in_j[1]
        pts.append((px(x), px(y)))
    d.line(pts, fill=CABLE, width=max(SS, px(0.065)), joint="curve")

    r = px(0.058)
    for jx, jy in (out_j, in_j):
        d.ellipse((px(jx) - r, px(jy) - r, px(jx) + r, px(jy) + r), fill=JACK)

    return im.resize((size, size), Image.LANCZOS)


def build() -> dict[int, Image.Image]:
    src = Image.open(SOURCE).convert("RGB")
    art = {}
    for size in (256, 128):
        art[size] = crop_icon(src, BIG_CROP, size)
    for size in (64, 48):
        art[size] = crop_icon(src, MID_CROP, size)
    for size in (32, 24, 16):
        art[size] = glyph(size)
    return art


def main() -> None:
    art = build()
    sizes = sorted(art, reverse=True)
    first = art[sizes[0]]
    first.save(OUT_ICO, format="ICO", sizes=[(n, n) for n in sizes],
               append_images=[art[n] for n in sizes[1:]])
    first.save(OUT_PNG)
    print(f"wrote {OUT_ICO.relative_to(ROOT)} ({', '.join(map(str, sizes))} px) "
          f"and {OUT_PNG.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
