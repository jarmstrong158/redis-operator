"""
build_icon.py — generates redis_operator.ico for the Windows installer.
Run once before building: python build_icon.py
"""
from PIL import Image, ImageDraw, ImageFont


def make_frame(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Outer glow (soft ring)
    glow = max(1, size // 12)
    draw.ellipse([glow, glow, size - glow - 1, size - glow - 1],
                 fill=(0, 80, 200, 60))

    # Main blue circle
    margin = max(2, size // 8)
    draw.ellipse([margin, margin, size - margin - 1, size - margin - 1],
                 fill=(0, 100, 230, 255))

    # Lighter top-left highlight for depth
    hl = margin + max(1, size // 16)
    hl_size = size // 3
    draw.ellipse([hl, hl, hl + hl_size, hl + hl_size],
                 fill=(80, 160, 255, 80))

    # "RO" text
    font_size = max(6, size // 3)
    font = None
    for face in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(face, font_size)
            break
        except Exception:
            pass
    if font is None:
        font = ImageFont.load_default()

    draw.text((size // 2, size // 2), "RO",
              fill=(255, 255, 255, 255), font=font, anchor="mm")
    return img


sizes = [16, 32, 48, 64, 128, 256]
frames = [make_frame(s) for s in sizes]

out = "redis_operator.ico"
frames[0].save(
    out,
    format="ICO",
    sizes=[(s, s) for s in sizes],
    append_images=frames[1:],
)
print(f"Generated {out}  ({len(sizes)} sizes: {sizes})")
