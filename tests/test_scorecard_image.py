"""GL-81: scorecard photo preparation. Every image is generated in memory with Pillow."""

import inspect
import io

import pillow_heif
import pytest
from PIL import Image

from golf_league.services import scorecard_image
from golf_league.services.scorecard_image import ImageRefused, prepare_image

pillow_heif.register_heif_opener()

RED, BLUE = (220, 30, 30), (30, 30, 220)


def _picture(size=(3000, 1500), color=RED, mode="RGB") -> Image.Image:
    return Image.new(mode, size, color)


def _encode(image: Image.Image, fmt: str, **options) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def _opened(prepared: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(prepared))
    image.load()
    return image


def _exif_with_gps() -> Image.Exif:
    exif = Image.Exif()
    exif[0x010F] = "Synthetic Camera Maker"
    exif[0x0132] = "2026:08:27 18:00:00"
    exif.get_ifd(0x8825)[2] = (41.0, 30.0, 0.0)  # GPS latitude
    return exif


def _refusal(raw: bytes) -> str:
    with pytest.raises(ImageRefused) as caught:
        prepare_image(raw)
    assert str(caught.value) == caught.value.code  # the message is only the stable code
    return caught.value.code


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "HEIF"])
def test_each_accepted_format_becomes_a_clean_jpeg_within_2048(fmt):
    prepared = _opened(prepare_image(_encode(_picture(), fmt)))
    assert prepared.format == "JPEG" and prepared.mode == "RGB"
    assert prepared.size == (2048, 1024)
    assert len(prepared.getexif()) == 0


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "HEIF"])
def test_exif_gps_and_other_metadata_are_removed(fmt):
    options = {"exif": _exif_with_gps()}
    if fmt == "JPEG":
        options["comment"] = b"synthetic-comment"
    source = _encode(_picture(), fmt, **options)
    assert len(Image.open(io.BytesIO(source)).getexif()) > 0  # the fixture really carries EXIF
    out = prepare_image(source)
    prepared = _opened(out)
    assert len(prepared.getexif()) == 0
    assert set(prepared.info) <= {"jfif", "jfif_version", "jfif_unit", "jfif_density"}
    for leaked in (b"Synthetic Camera Maker", b"synthetic-comment", b"Exif", b"2026:08:27"):
        assert leaked not in out


def test_an_icc_profile_does_not_survive():
    source = _encode(_picture(), "JPEG", icc_profile=b"synthetic-icc-profile-bytes")
    assert b"synthetic-icc-profile-bytes" not in prepare_image(source)


def test_a_rotated_exif_image_comes_out_upright():
    # Stored 400 wide by 200 tall, left half red and right half blue, tagged
    # "rotate 90 degrees clockwise to view" (orientation 6).
    stored = Image.new("RGB", (400, 200), RED)
    stored.paste(BLUE, (200, 0, 400, 200))
    exif = Image.Exif()
    exif[0x0112] = 6
    prepared = _opened(prepare_image(_encode(stored, "JPEG", exif=exif, quality=95)))
    assert prepared.size == (200, 400)
    top, bottom = prepared.getpixel((100, 50)), prepared.getpixel((100, 350))
    assert top[0] > 150 and top[2] < 100  # the stored left edge (red) is now the top
    assert bottom[2] > 150 and bottom[0] < 100


def test_a_small_image_is_not_enlarged():
    assert _opened(prepare_image(_encode(_picture((640, 480)), "PNG"))).size == (640, 480)


def test_a_tall_image_is_scaled_on_its_long_edge():
    assert _opened(prepare_image(_encode(_picture((1000, 4096)), "JPEG"))).size == (500, 2048)


def test_a_transparent_png_is_flattened_onto_white():
    prepared = _opened(prepare_image(_encode(_picture((50, 50), (0, 0, 0, 0), "RGBA"), "PNG")))
    assert all(channel > 240 for channel in prepared.getpixel((25, 25)))


def test_a_phone_jpeg_with_a_second_picture_is_accepted():
    # Many phone cameras append an HDR gain map; Pillow reports the file as MPO.
    source = _encode(_picture(), "MPO", save_all=True, append_images=[_picture((300, 150), BLUE)])
    assert Image.open(io.BytesIO(source)).format == "MPO"
    prepared = _opened(prepare_image(source))
    assert prepared.format == "JPEG" and prepared.size == (2048, 1024)
    assert prepared.getpixel((10, 10))[0] > 150  # the first picture, not the appended one


def test_more_than_10_mb_is_too_large_and_exactly_10_mb_is_judged_by_content():
    assert _refusal(b"\xff\xd8" + b"\x00" * (11 * 1024 * 1024)) == "too_large"
    assert _refusal(b"\x00" * (scorecard_image.MAX_UPLOAD_BYTES + 1)) == "too_large"
    assert _refusal(b"\x00" * scorecard_image.MAX_UPLOAD_BYTES) == "unsupported_type"


@pytest.mark.parametrize("fmt", ["GIF", "BMP", "TIFF", "WEBP"])
def test_other_image_formats_are_unsupported(fmt):
    assert _refusal(_encode(_picture((64, 64)), fmt)) == "unsupported_type"


@pytest.mark.parametrize("raw", [
    b"", b"just some text pretending to be card.jpg\n" * 50, b"\xff\xd8\xff\xe0not really a jpeg",
    b"%PDF-1.7 synthetic", b"<svg xmlns='http://www.w3.org/2000/svg'/>",
])
def test_non_images_are_unsupported(raw):
    assert _refusal(raw) == "unsupported_type"


def test_a_truncated_jpeg_is_unsupported():
    whole = _encode(_picture((800, 600)), "JPEG")
    assert _refusal(whole[: len(whole) // 3]) == "unsupported_type"


def test_the_type_is_judged_from_content_only():
    # There is no file name or declared type to lie with: the bytes are the only input.
    assert list(inspect.signature(prepare_image).parameters) == ["raw"]
    png_bytes = _encode(_picture((64, 64)), "PNG")
    assert _opened(prepare_image(png_bytes)).format == "JPEG"


def test_too_many_pixels_is_refused_before_decoding():
    # 56 million one-bit pixels compress to a few kilobytes.
    huge = _encode(Image.new("1", (8000, 7000)), "PNG")
    assert len(huge) < scorecard_image.MAX_UPLOAD_BYTES
    assert _refusal(huge) == "too_many_pixels"


def test_exactly_the_pixel_limit_is_accepted(monkeypatch):
    monkeypatch.setattr(scorecard_image, "MAX_PIXELS", 100 * 100)
    assert _opened(prepare_image(_encode(_picture((100, 100)), "PNG"))).size == (100, 100)
    assert _refusal(_encode(_picture((101, 100)), "PNG")) == "too_many_pixels"


def test_a_decompression_bomb_is_too_many_pixels(monkeypatch):
    # Pillow itself refuses an image over twice its own limit while opening it.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    assert _refusal(_encode(_picture((64, 64)), "PNG")) == "too_many_pixels"
