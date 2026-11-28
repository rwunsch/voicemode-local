#!/usr/bin/env python3
"""Prepare the approved FELIX still and 60-frame export for the 40 pt Voice Orb.

Requires Pillow for asset preparation only; the Pill reads the checked-in files.
"""
import argparse
import math
from pathlib import Path
from statistics import median

from PIL import Image


def crop_circle(source, box, radius):
    orb = source.convert("RGBA").crop(box)
    width, height = orb.size
    alpha = Image.new("L", orb.size)
    alpha.putdata([
        round(255 * min(1.0, max(0.0, radius + .5 - math.hypot(x + .5 - width / 2, y + .5 - height / 2))))
        for y in range(height) for x in range(width)
    ])
    orb.putalpha(alpha)
    return orb


def lift_existing_star_peaks(orb):
    """Gently lift existing pinpoints without making a new spot or painting a halo."""
    light = orb.convert("L").load()
    width, height = orb.size
    color = orb.load()
    interior = (min(width, height) * .44) ** 2  # leave the bright circular rim alone
    axes = ((2, 0), (0, 2), (2, 2), (2, -2))
    neighbors = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]
    for y in range(2, height - 2):
        for x in range(2, width - 2):
            if (x + .5 - width / 2) ** 2 + (y + .5 - height / 2) ** 2 > interior:
                continue
            peak = light[x, y]
            if any(light[x + dx, y + dy] > peak for dx, dy in neighbors):
                continue
            # Opposite samples cancel ordinary color gradients. Only lift a
            # local maximum with enough contrast to be an existing speckle.
            surround = median((light[x + dx, y + dy] + light[x - dx, y - dy]) / 2 for dx, dy in axes)
            contrast = peak - surround
            if contrast < 7:
                continue
            amount = min(9, round(contrast * .16))
            red, green, blue, alpha = color[x, y]
            color[x, y] = (min(255, red + round(amount * .83)),
                           min(255, green + round(amount * .79)),
                           min(255, blue + amount), alpha)
    return orb


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("still", type=Path, help="the approved felix-orb.png export")
    parser.add_argument("frames", type=Path, help="the approved motion-frames-felix directory")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "ui/pill")
    args = parser.parse_args()
    source_frames = sorted(args.frames.glob("*.png"))
    if len(source_frames) != 60:
        parser.error("expected the original 60 frames")

    with Image.open(args.still) as original:
        if original.size != (156, 158):
            parser.error("unexpected still export dimensions")
        still = crop_circle(original, (0, 1, 156, 157), 77.1)
        lift_existing_star_peaks(still).save(args.output / "orb-felix.png")

    tile = 96
    sheet = Image.new("RGBA", (tile * 10, tile * 6))
    for index, path in enumerate(source_frames):
        with Image.open(path) as frame:
            if frame.size != (360, 360):
                parser.error(f"unexpected frame dimensions: {path.name}")
            face = crop_circle(frame, (126, 128, 233, 235), 53.15)
            face = lift_existing_star_peaks(face.resize((tile, tile), Image.Resampling.LANCZOS))
            sheet.paste(face, (index % 10 * tile, index // 10 * tile))
    sheet.save(args.output / "orb-felix-motion.webp", lossless=True, method=6)


if __name__ == "__main__":
    main()
