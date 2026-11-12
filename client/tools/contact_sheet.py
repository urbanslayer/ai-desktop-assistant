#!/usr/bin/env python3
"""Contact sheet + metrics table for a capture_frames.py run.

For each <outdir>/<stem>.png with a sibling <stem>.json (the page's
rikoMeasure payload), measures the SUBJECT'S real pixel footprint from the
alpha channel (rows/cols where the avatar is nearly opaque) and labels the
tile with the interesting metrics. For the `zoom` scenario the useful
invariants are:

  bodyPx / visualScale   constant  -> pixels-per-metre depends only on the
                                      visual scale, never on canvas height
  footFromBottom         constant  -> feet stay anchored across the
                                      headroom grow and the end-of-gesture
                                      fit (no visible jump at the resize)

Usage: python client/tools/contact_sheet.py <outdir> [outfile]
"""
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw


def alpha_box(png, thresh=24):
    im = Image.open(png).convert("RGBA")
    a = im.getchannel("A")
    a = a.point(lambda v: 255 if v > thresh else 0)
    bbox = a.getbbox()
    if not bbox:
        return im, None
    x0, y0, x1, y1 = bbox
    return im, dict(top=y0, bottom=y1, left=x0, right=x1,
                    bodyPx=y1 - y0 + 1, widthPx=x1 - x0 + 1,
                    footFromBottom=im.height - 1 - y1, topFromTop=y0)


def main():
    outdir = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else outdir / "contact-sheet.png"
    pairs = sorted(p.with_suffix(".png") for p in outdir.glob("*.json"))
    if not pairs:
        pairs = sorted(p for p in outdir.glob("*.png") if p.name != out.name)
    tiles, rows = [], []
    for png in pairs:
        mj = png.with_suffix(".json")
        met = {}
        if mj.exists():
            met = (json.loads(mj.read_text()).get("metrics") or {})
        im, box = alpha_box(png)
        row = dict(frame=png.name, canvas=f"{im.width}x{im.height}",
                   visualScale=met.get("visualScale"),
                   windowScale=met.get("windowScale"), **({} | (box or {})))
        if box and isinstance(met.get("visualScale"), (int, float)):
            row["pxPerScale"] = round(box["bodyPx"] / met["visualScale"], 1)
        rows.append(row)
        # label the tile
        tile = im.convert("RGB")
        d = ImageDraw.Draw(tile)
        leg = met.get("legQA") or {}
        leg_lines = []
        for side in ("left", "right"):
            s = leg.get(side) or {}
            if s:
                leg_lines.append(
                    f"{side[0].upper()} knee={s.get('kneeFlexDeg')}deg"
                    f" thigh={s.get('thighFlexDeg')}deg"
                    f" footDy={s.get('footDyFromRestM')}m"
                    f" xz={s.get('footXzDriftM')}m {s.get('mode') or ''}")
        lines = [f"t={png.name.split('-t')[1][:6] if '-t' in png.name else png.name}",
                 f"v={met.get('visualScale')} win={met.get('windowScale')}",
                 f"canvas={im.width}x{im.height}" + (
                     f"  body={box['bodyPx']}px  foot@{box['footFromBottom']}"
                     if box else "")] + leg_lines
        band = 20 + 17 * len(lines)
        d.rectangle([0, 0, tile.width, band], fill=(12, 12, 18))
        for i, ln in enumerate(lines):
            d.text((6, 4 + i * 17), ln, fill=(120, 230, 160))
        tiles.append(tile)
    # 2 columns, tiles scaled to a common height
    th = max(t.height for t in tiles)
    tw = max(t.width for t in tiles)
    cols = 2
    rowsn = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tw + 8) + 8, rowsn * (th + 8) + 8), (10, 10, 14))
    for i, t in enumerate(tiles):
        c, r = i % cols, i // cols
        sheet.paste(t, (8 + c * (tw + 8), 8 + r * (th + 8)))
    sheet.save(out)
    print(f"[sheet] {out}  ({len(tiles)} frames)")
    for r in rows:
        print(json.dumps(r))


if __name__ == "__main__":
    main()
