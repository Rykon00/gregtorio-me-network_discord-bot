#!/usr/bin/env python3
"""Build the Discord server icon and the bot avatar from the two mod thumbnails.

    python tools/make_icons.py --gregtorio ../Gregtorio/thumbnail.png \\
        --me-network ../me-network/thumbnail.png --out assets

Both pictures are a crossover of the same parts: the Gregtorio lettering and
gear, cut out of its thumbnail by colour, and the drive, terminal and cable
loop of the ME Network thumbnail.

  server-icon.png  the lettering above the ME machines, the gear resting on the terminal
  bot-avatar.png   the ME scene with the gear in its cable loop; nothing important
                   sits in the corners, so it survives Discord's round avatar crop

The parts are placed at their native pixel size and the result is scaled up by a
whole factor, so the pixel art stays sharp. Needs Pillow and NumPy (not part of
requirements.txt: the sync tool does not need them).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

BACKGROUND = (18, 16, 26, 255)  # the background colour of the ME Network thumbnail


def _morph(mask, kind):
    image = Image.fromarray((mask * 255).astype("uint8"))
    size_filter = ImageFilter.MaxFilter if kind == "grow" else ImageFilter.MinFilter
    return np.asarray(image.filter(size_filter(3))) > 0


def _components(mask):
    """4-connected components as a label array (0 = not in the mask)."""
    labels = np.zeros(mask.shape, int)
    count = 0
    height, width = mask.shape
    for y, x in zip(*np.nonzero(mask)):
        if labels[y, x]:
            continue
        count += 1
        labels[y, x] = count
        stack = [(y, x)]
        while stack:
            cy, cx = stack.pop()
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not labels[ny, nx]:
                    labels[ny, nx] = count
                    stack.append((ny, nx))
    return labels, count


def _has_open_neighbour(mask):
    padded = np.pad(mask, 1)
    return ~(padded[:-2, 1:-1] & padded[2:, 1:-1] & padded[1:-1, :-2] & padded[1:-1, 2:])


def cut_out_logo(thumbnail):
    """The orange lettering and gear with their white and black outlines, as RGBA."""
    source = thumbnail.convert("RGB")
    rgb = np.asarray(source).astype(float) / 255
    red, green, blue = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    high, low = rgb.max(-1), rgb.min(-1)
    spread = np.maximum(high - low, 1e-6)
    saturation = np.where(high > 0, (high - low) / np.maximum(high, 1e-6), 0)
    hue = 60 * np.where(
        high == red, ((green - blue) / spread) % 6,
        np.where(high == green, (blue - red) / spread + 2, (red - green) / spread + 4),
    )
    orange = (hue >= 5) & (hue <= 50) & (saturation > 0.45) & (high > 0.08)
    white = low > 0.93
    dark = high < 0.10
    core = orange | white
    mask = core | (dark & _morph(core, "grow"))
    # close one-pixel seams between the fill and its outlines
    mask = mask | _morph(_morph(mask, "grow"), "shrink")
    # fill the small gaps the shading of the gear leaves; real counters stay open
    holes, count = _components(~mask)
    for index in range(1, count + 1):
        ys, xs = np.nonzero(holes == index)
        inside = ys.min() > 0 and xs.min() > 0 and ys.max() < mask.shape[0] - 1 and xs.max() < mask.shape[1] - 1
        if inside and len(ys) <= 6:
            mask[holes == index] = True
    # the seam closing may have pulled in background pixels at the rim: drop them
    logo_colour = orange | white | dark
    for _ in range(3):
        mask &= ~(_has_open_neighbour(mask) & ~logo_colour)
    # drop loose pixels and specks
    for _ in range(2):
        padded = np.pad(mask, 1).astype(int)
        neighbours = sum(
            padded[1 + dy:padded.shape[0] - 1 + dy, 1 + dx:padded.shape[1] - 1 + dx]
            for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy, dx) != (0, 0)
        )
        mask &= neighbours >= 3
    parts, count = _components(mask)
    for index in range(1, count + 1):
        if (parts == index).sum() < 40:
            mask[parts == index] = False
    result = np.zeros(mask.shape + (4,), "uint8")
    result[..., :3] = np.asarray(source)
    result[..., 3] = mask * 255
    return Image.fromarray(result, "RGBA")


def split_gear(logo):
    """Return (lettering, gear, gear_position): the gear is the 'O' at the right of the second line."""
    pixels = np.asarray(logo).copy()
    # The gear touches the "I" next to it, so it is not a component of its own. It fills the
    # right 3/8 of the picture: the largest component there is the gear.
    cut = logo.width * 5 // 8
    right_part = pixels[..., 3] > 0
    right_part[:, :cut] = False
    labels, count = _components(right_part)
    sizes = [(labels == index).sum() for index in range(1, count + 1)]
    if not sizes or max(sizes) < 400:
        raise SystemExit("could not find the gear in the Gregtorio thumbnail")
    best = 1 + int(np.argmax(sizes))
    gear_pixels = pixels.copy()
    gear_pixels[..., 3] = np.where(labels == best, 255, 0)
    pixels[..., 3] = np.where(labels == best, 0, pixels[..., 3])
    # the gear has a white rim; dark pixels outside of it are leftovers of the old background
    alpha = gear_pixels[..., 3] > 0
    dark = gear_pixels[..., :3].max(-1) < 77
    while True:
        rim = alpha & dark & _has_open_neighbour(alpha)
        if not rim.any():
            break
        alpha &= ~rim
    # ... and one-pixel tails that the removed background leaves on the rim
    while True:
        padded = np.pad(alpha, 1).astype(int)
        neighbours = padded[:-2, 1:-1] + padded[2:, 1:-1] + padded[1:-1, :-2] + padded[1:-1, 2:]
        tail = alpha & (neighbours <= 1)
        if not tail.any():
            break
        alpha &= ~tail
    # the white rim hugs the gear: white further than two pixels from its body is a leftover
    whitish = gear_pixels[..., :3].min(-1) > 200
    body = alpha & ~whitish
    alpha &= ~whitish | _morph(_morph(body, "grow"), "grow")
    gear_pixels[..., 3] = alpha * 255
    gear = Image.fromarray(gear_pixels, "RGBA")
    box = gear.getbbox()
    return Image.fromarray(pixels, "RGBA"), gear.crop(box), (box[0], box[1])


def me_parts(thumbnail):
    """Bounding boxes of the machines and the cable loop of the ME Network thumbnail."""
    rgb = np.asarray(thumbnail.convert("RGB")).astype(int)
    purple = (rgb[..., 2] > 150) & (rgb[..., 0] > 90) & (rgb[..., 1] < 110)
    lit = rgb.max(-1) > 0x48
    ys, xs = np.nonzero(lit)
    left, right = int(xs.min()), int(xs.max()) + 1
    top = int(ys.min())
    # the machines end with the last row that is lit across (almost) their whole width
    def extent(row):
        columns = np.nonzero(lit[row])[0]
        return columns.max() - columns.min() + 1 if len(columns) else 0

    machines_bottom = max(y for y in range(top, lit.shape[0]) if extent(y) >= 0.9 * (right - left)) + 1
    cable_ys, cable_xs = np.nonzero(purple[machines_bottom:])
    cable_left, cable_right = int(cable_xs.min()), int(cable_xs.max()) + 1
    cable_bottom = machines_bottom + int(cable_ys.max()) + 1
    # the horizontal pipe starts where the purple spans (almost) the whole loop
    pipe_top = next(
        y for y in range(machines_bottom, cable_bottom) if purple[y, cable_left:cable_right].sum() > (cable_right - cable_left) // 4
    )
    return {
        "machines": (left, top, right, machines_bottom),
        "cables": (cable_left, machines_bottom, cable_right, pipe_top),
        "pipe": (cable_left, pipe_top, cable_right, cable_bottom),
    }


def bot_avatar(me, gear):
    """The ME scene with the Gregtorio gear resting in its cable loop."""
    parts = me_parts(me)
    picture = me.convert("RGBA").copy()
    left, top, right, bottom = parts["cables"][0], parts["cables"][1], parts["pipe"][2], parts["pipe"][3]
    centre_x = (left + right) // 2
    centre_y = (top + bottom) // 2
    picture.alpha_composite(gear, (centre_x - gear.width // 2, centre_y - gear.height // 2))
    return picture


def server_icon(me, lettering, gear, gear_position):
    """The lettering above the ME machines; the gear overlaps the terminal's corner."""
    parts = me_parts(me)
    me = me.convert("RGBA")
    word_box = lettering.getbbox()
    word = lettering.crop(word_box)
    machines = me.crop(parts["machines"])
    cables = me.crop(parts["cables"]).crop((0, 0, parts["cables"][2] - parts["cables"][0], 4))
    pipe = me.crop(parts["pipe"])
    gear_x, gear_y = gear_position[0] - word_box[0], gear_position[1] - word_box[1]
    content_width = max(gear_x + gear.width, word.width, machines.width)
    gap = 3
    content_height = word.height + gap + machines.height + cables.height + pipe.height
    size = max(content_width, content_height) + 12
    picture = Image.new("RGBA", (size, size), BACKGROUND)
    x0 = (size - content_width) // 2
    y0 = (size - content_height) // 2
    machines_x = (size - machines.width) // 2
    machines_y = y0 + word.height + gap
    picture.alpha_composite(machines, (machines_x, machines_y))
    cable_x = machines_x + parts["cables"][0] - parts["machines"][0]
    picture.alpha_composite(cables, (cable_x, machines_y + machines.height))
    picture.alpha_composite(pipe, (cable_x, machines_y + machines.height + cables.height))
    picture.alpha_composite(word, (x0, y0))
    picture.alpha_composite(gear, (x0 + gear_x, y0 + gear_y))
    return picture


def upscale(picture, at_least=1024):
    factor = -(-at_least // picture.width)
    return picture.convert("RGB").resize((picture.width * factor, picture.height * factor), Image.NEAREST)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--gregtorio", required=True, help="thumbnail.png of Gregtorio Continued")
    parser.add_argument("--me-network", required=True, help="thumbnail.png of ME Network")
    parser.add_argument("--out", default="assets")
    args = parser.parse_args()

    me = Image.open(args.me_network)
    lettering, gear, gear_position = split_gear(cut_out_logo(Image.open(args.gregtorio)))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, picture in (
        ("server-icon.png", server_icon(me, lettering, gear, gear_position)),
        ("bot-avatar.png", bot_avatar(me, gear)),
    ):
        final = upscale(picture)
        final.save(out / name, optimize=True)
        print(f"{out / name}: {picture.width}x{picture.height} drawn, saved as {final.width}x{final.height}")


if __name__ == "__main__":
    main()
