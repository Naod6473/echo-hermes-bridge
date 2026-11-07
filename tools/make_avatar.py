#!/usr/bin/env python3
"""Turn a two-tone drawing into an animated avatar for the TV screen (screen/avatar.js).

    pip install potracer scipy pillow numpy
    python tools/make_avatar.py portrait.png --rig rig.json -o avatar.json

The image can be white lines on a transparent background, or black lines on white: both give the
same "ink". It is fitted into a 640 x 640 square and traced into vector paths (sharp at any size).

rig.json says what moves, in that 640 x 640 square (open the traced avatar at /screen/?demo=1 to check):

    {
      "eyes": [
        {"box": [200, 240, 300, 312], "line": 294, "lid": [[208, 288], [250, 312], [294, 286]], "w": 5},
        {"box": [120, 255, 168, 291], "line": 283, "lid": [[129, 277], [146, 288], [161, 285]], "w": 3.5, "cut": true}
      ],
      "lip": [152, 399, 34, 21],
      "neck": [320, 560]
    }

- eyes: every ink shape fully inside `box` blinks, closing onto the y of `line`; once closed, `lid`
  (start, curve control, end) is drawn as the lash line, `w` thick. Use "cut": true for an eye drawn
  as a gap in the ink (taken from the non-ink shapes inside the box).
- lip: x, y, width, height of the lower lip; it drops with the voice.
- neck: the point the head tilts around.

Copy avatar.json next to bridge.py (or point AVATAR_FILE at it) and pick a style on the screen.
"""
import argparse
import json

import numpy as np
from PIL import Image, ImageFilter

S = 3                                                   # trace at 3x for smooth curves


def load_ink(path: str) -> np.ndarray:
    im = Image.open(path).convert("RGBA")
    a = np.array(im)
    alpha = a[..., 3]
    if (alpha < 128).mean() > 0.05:                      # transparent background: the opaque pixels are ink
        ink = Image.fromarray(alpha)
    else:                                               # opaque: dark pixels are ink
        ink = Image.fromarray((255 - np.array(im.convert("L"))).astype(np.uint8))
    side = max(ink.size)
    square = Image.new("L", (side, side), 0)
    square.paste(ink, ((side - ink.width) // 2, (side - ink.height) // 2))
    big = square.resize((640 * S, 640 * S), Image.BICUBIC).filter(ImageFilter.GaussianBlur(1.2))
    return np.array(big) > 127


def trace(mask: np.ndarray) -> str:
    import potrace                                       # potracer; it traces the False pixels
    out = []
    f = lambda p: f"{p.x / S:.2f} {p.y / S:.2f}"        # noqa: E731
    for c in potrace.Bitmap(~mask).trace(turdsize=6, alphamax=1.0, opticurve=True, opttolerance=0.2):
        d = [f"M{f(c.start_point)}"]
        for s in c.segments:
            d.append(f"L{f(s.c)}L{f(s.end_point)}" if s.is_corner else f"C{f(s.c1)} {f(s.c2)} {f(s.end_point)}")
        out.append("".join(d) + "Z")
    return "".join(out)


def shapes_inside(mask: np.ndarray, box) -> np.ndarray:
    from scipy import ndimage
    x0, y0, x1, y1 = [int(v * S) for v in box]
    lab, _ = ndimage.label(mask)
    keep = np.zeros_like(mask)
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl and sl[0].start >= y0 and sl[0].stop <= y1 and sl[1].start >= x0 and sl[1].stop <= x1:
            keep |= lab == i
    return keep


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("image")
    p.add_argument("--rig", help="rig.json (eyes, lip, neck); without it the avatar only breathes and talks")
    p.add_argument("-o", "--out", default="avatar.json")
    a = p.parse_args()
    ink = load_ink(a.image)
    rig = json.load(open(a.rig, encoding="utf-8")) if a.rig else {}
    base = ink.copy()
    eyes = []
    for e in rig.get("eyes", []):
        part = shapes_inside(~ink if e.get("cut") else ink, e["box"])
        if not part.any():
            raise SystemExit(f"no shape fully inside the eye box {e['box']}")
        base = (base | part) if e.get("cut") else (base & ~part)
        eyes.append({k: v for k, v in e.items() if k != "box"} | {"d": trace(part)})
    data = {"version": 1, "base": trace(base), "eyes": eyes, "lip": rig.get("lip"), "neck": rig.get("neck", [320, 560])}
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"{a.out}: {len(eyes)} eye(s), {sum(len(x['d']) for x in eyes) + len(data['base'])} bytes of paths")


if __name__ == "__main__":
    main()
