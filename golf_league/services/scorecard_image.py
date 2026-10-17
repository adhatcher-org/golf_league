"""Scorecard photo preparation (R-SCORES "Upload").

An upload is judged by its content, never by its name or declared type. What
comes out is a plain JPEG: upright, RGB, long edge at most 2048 px, and with
none of the original's EXIF, GPS or other metadata. Nothing here stores,
logs or sends the image.

This module imports no `fastapi` (enforced by `tests/test_architecture.py`).
"""

import io

import pillow_heif
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 50_000_000
MAX_LONG_EDGE = 2048
JPEG_QUALITY = 90
# The decoders that may open an upload; HEIC photos decode as "HEIF".
OPEN_FORMATS = ("JPEG", "PNG", "HEIF")
# Pillow reports a JPEG that carries a second picture (an HDR gain map, as many
# phone cameras write) as "MPO". It is still a JPEG and only the first picture
# is used.
ACCEPTED_FORMATS = (*OPEN_FORMATS, "MPO")

pillow_heif.register_heif_opener()


class ImageRefused(Exception):
    """A refused upload. The message is the stable code: no file content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def prepare_image(raw: bytes) -> bytes:
    """Return the upload as a clean JPEG, or raise `ImageRefused`.

    Codes: `too_large` (over 10 MB), `unsupported_type` (not a JPEG, PNG or
    HEIC by content, or corrupt) and `too_many_pixels` (over 50 million).
    """
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ImageRefused("too_large")
    try:
        image = Image.open(io.BytesIO(raw), formats=OPEN_FORMATS)
    except Image.DecompressionBombError:
        raise ImageRefused("too_many_pixels") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError):
        raise ImageRefused("unsupported_type") from None
    with image:
        if image.format not in ACCEPTED_FORMATS:
            raise ImageRefused("unsupported_type")
        width, height = image.size
        if width * height > MAX_PIXELS:
            raise ImageRefused("too_many_pixels")
        try:
            image.load()
            upright = ImageOps.exif_transpose(image)
        except Image.DecompressionBombError:
            raise ImageRefused("too_many_pixels") from None
        except Exception:  # truncated or corrupt data fails in many decoder-specific ways
            raise ImageRefused("unsupported_type") from None
        if upright.mode in ("RGBA", "LA", "PA") or "transparency" in upright.info:
            # A transparent PNG becomes black with a plain convert; paste it on white.
            layer = upright.convert("RGBA")
            flat = Image.new("RGB", layer.size, (255, 255, 255))
            flat.paste(layer, mask=layer.getchannel("A"))
        else:
            flat = upright.convert("RGB")
        flat.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE), Image.Resampling.LANCZOS)
        # Copy the pixels into a new image so nothing from the original's
        # `info` (EXIF, GPS, ICC profile, comments) can reach the encoder.
        clean = Image.new("RGB", flat.size)
        clean.paste(flat)
        out = io.BytesIO()
        clean.save(out, format="JPEG", quality=JPEG_QUALITY)
        return out.getvalue()
