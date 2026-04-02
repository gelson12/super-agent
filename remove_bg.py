"""
Remove white/glow background from the Bridge logo.
Runs automatically at container startup via entrypoint.sh.
Writes /app/static/bridge.png (transparent background, 3x upscaled for sharpness).
"""
import sys
from pathlib import Path
from PIL import Image, ImageFilter
import numpy as np

# Support both container path and local override
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/app/static/bridge.jpg")
DST = SRC.parent / "bridge.png"

if not SRC.exists():
    print(f"[remove_bg] Source not found: {SRC} — skipping")
    sys.exit(0)

img = Image.open(SRC).convert("RGBA")
w, h = img.size

# Upscale 3x for maximum sharpness before processing
img = img.resize((w * 3, h * 3), Image.LANCZOS)

data = np.array(img, dtype=np.float32)
r, g, b, a = data[:, :, 0], data[:, :, 1], data[:, :, 2], data[:, :, 3]

brightness = (r + g + b) / 3.0
# Gold character: high R-B ratio means warm/golden
gold_character = (r - b) / (brightness + 1.0)

# Background = bright AND not warm/gold
is_background = (brightness > 200) & (gold_character < 0.25)
# Fringe = lighter blue-grey glow pixels at edges
is_fringe = (brightness > 175) & (b > r * 0.80) & (gold_character < 0.15)

mask = (is_background | is_fringe).astype(np.uint8) * 255

# Feather edges with Gaussian blur to avoid harsh cutoff
mask_img = Image.fromarray(mask, mode="L").filter(ImageFilter.GaussianBlur(radius=2))
mask_arr = np.array(mask_img) / 255.0

# Apply inverted mask to alpha channel
data[:, :, 3] = (a * (1.0 - mask_arr)).clip(0, 255).astype(np.uint8)

result = Image.fromarray(data.astype(np.uint8), "RGBA")
result.save(str(DST), "PNG", optimize=True)
print(f"[remove_bg] Saved {DST} at {w*3}x{h*3}")
