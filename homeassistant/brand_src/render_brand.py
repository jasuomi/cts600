"""Render icon.svg to the integration's brand images.

Home Assistant (2026.x) serves brand images for a custom integration from
custom_components/<domain>/brand/: icon.png (256x256) and icon@2x.png
(512x512); logo and dark_* fall back to the icon. The icon is a round
Finnish-flag badge with its own blue ring, so it works on both light and
dark themes.

Run in a container (cairosvg needs Cairo):
  docker run --rm -v "$PWD/homeassistant:/work" -w /work python:3.14 \
    sh -c "pip install -q cairosvg && python brand_src/render_brand.py"
"""

from pathlib import Path

import cairosvg

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "custom_components" / "cts600" / "brand"

OUT.mkdir(exist_ok=True)
svg = (HERE / "icon.svg").read_bytes()
for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
    cairosvg.svg2png(bytestring=svg, write_to=str(OUT / name), output_width=size, output_height=size)
    print(f"wrote {OUT / name} ({size}x{size})")
