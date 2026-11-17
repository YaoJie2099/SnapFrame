"""Shared SnapFrame branding and bundled icon."""
import sys
from pathlib import Path
from PIL import Image
APP = "SnapFrame"
VERSION = "0.2.0"
def make_icon_image(size=256):
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    with Image.open(root / "assets" / "snapframe.ico") as image:
        return image.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
