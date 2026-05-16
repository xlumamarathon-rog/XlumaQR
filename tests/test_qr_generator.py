"""Tests for the pure-Python core in ``qr_generator``.

These tests deliberately exercise the real functions and assert on real
outputs (PIL image dimensions, PNG magic bytes, ZIP namelists, PDF
magic). No mocking.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from PIL import Image

from qr_generator import (
    compute_range,
    generate_qr,
    generate_sequence,
    images_to_pdf,
    images_to_zip,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_generate_qr_returns_pil_image_with_expected_size() -> None:
    img = generate_qr("hello", box_size=10, border=4)
    assert isinstance(img, Image.Image)
    # No label -> the image should be square (the bare QR code).
    width, height = img.size
    assert width == height
    assert width > 0


def test_generate_qr_with_label_increases_height_and_image_is_decodable_as_png_bytes() -> None:
    bare = generate_qr("hello", box_size=10, border=4)
    labeled = generate_qr("hello", label="42", box_size=10, border=4)

    # Label band must add vertical pixels.
    assert labeled.size[1] > bare.size[1]
    # And the labeled canvas must be at least as wide as the bare QR.
    assert labeled.size[0] >= bare.size[0]

    # Round-trip via PNG bytes to confirm the image is a real PNG.
    buf = io.BytesIO()
    labeled.save(buf, format="PNG")
    data = buf.getvalue()
    assert data.startswith(PNG_MAGIC)

    reopened = Image.open(io.BytesIO(data))
    reopened.load()
    assert reopened.size == labeled.size


def test_compute_range_count_inclusive_start_exclusive_end() -> None:
    r = compute_range(101, count=100)
    assert len(r) == 100
    assert r[0] == "101"
    assert r[-1] == "200"


def test_compute_range_with_end_inclusive() -> None:
    r = compute_range(1, end=3)
    assert r == ["1", "2", "3"]


def test_compute_range_padding() -> None:
    r = compute_range(1, count=5, padding=3)
    assert r == ["001", "002", "003", "004", "005"]


def test_compute_range_invalid_raises_valueerror() -> None:
    with pytest.raises(ValueError):
        compute_range(1, count=0)
    with pytest.raises(ValueError):
        compute_range(10, end=5)
    with pytest.raises(ValueError):
        compute_range(1, count=2, end=5)
    with pytest.raises(ValueError):
        compute_range(1)


def test_generate_sequence_yields_correct_count_and_filenames() -> None:
    items = list(
        generate_sequence(
            start=101,
            count=3,
            padding=4,
            prefix="qr_",
        )
    )
    assert len(items) == 3
    filenames = [name for name, _ in items]
    assert filenames == ["qr_0101.png", "qr_0102.png", "qr_0103.png"]
    for _, image in items:
        assert isinstance(image, Image.Image)
        assert image.size[0] > 0 and image.size[1] > 0


def test_images_to_zip_contains_all_pngs() -> None:
    items = list(generate_sequence(start=1, count=3, padding=2, prefix=""))
    zip_bytes = images_to_zip(items)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        namelist = zf.namelist()
        assert namelist == ["01.png", "02.png", "03.png"]
        for name in namelist:
            entry = zf.read(name)
            assert entry.startswith(PNG_MAGIC), f"{name} is not a PNG"


def test_images_to_pdf_returns_pdf_bytes() -> None:
    items = list(generate_sequence(start=1, count=5, padding=2))
    pdf_bytes = images_to_pdf(items)
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 1024


# --- Hardening regression tests (semantic-review v1) ----------------


def test_compute_range_count_above_max_raises() -> None:
    """Issue 3: ``count`` is bounded by ``MAX_RANGE_SIZE``."""
    from qr_generator import MAX_RANGE_SIZE

    with pytest.raises(ValueError):
        compute_range(0, count=MAX_RANGE_SIZE + 1)


def test_compute_range_end_span_above_max_raises() -> None:
    """Issue 3: ``end - start + 1`` is bounded by ``MAX_RANGE_SIZE``."""
    from qr_generator import MAX_RANGE_SIZE

    with pytest.raises(ValueError):
        compute_range(1, end=MAX_RANGE_SIZE + 1)


def test_generate_sequence_template_with_unknown_placeholder_is_literal() -> None:
    """Issues 1 & 2: templates use ``str.replace`` so ``{m}`` is literal text,
    not a ``KeyError``, and attribute walks like ``{n.__class__}`` do not
    perform attribute access.

    The image bytes for the substituted template must match the bytes
    produced by ``generate_qr`` called directly with the literal payload
    we expect after substitution. If a regression replaced ``str.replace``
    with ``str.format`` (or with a smarter template engine), the encoded
    payload would differ and these byte streams would no longer match.
    """
    items = list(
        generate_sequence(
            start=1,
            count=1,
            data_template="prefix-{m}-{n}-{n.__class__}",
            label_template=None,
        )
    )
    assert len(items) == 1
    name, image = items[0]
    assert name == "1.png"
    assert isinstance(image, Image.Image)

    # Independently render what the encoded payload should be: ``{n}``
    # is replaced by the padded number, but ``{m}`` and ``{n.__class__}``
    # remain literal text.
    expected_payload = "prefix-{m}-1-{n.__class__}"
    expected = generate_qr(expected_payload)

    actual_buf = io.BytesIO()
    image.save(actual_buf, format="PNG")
    expected_buf = io.BytesIO()
    expected.save(expected_buf, format="PNG")
    assert actual_buf.getvalue() == expected_buf.getvalue()


def test_generate_sequence_label_template_with_unknown_placeholder_is_literal() -> None:
    """Issues 1 & 2: same protection applies to ``label_template``."""
    items = list(
        generate_sequence(
            start=1,
            count=1,
            data_template="{n}",
            label_template="L-{m}-{n}",
        )
    )
    assert len(items) == 1
    name, image = items[0]
    assert name == "1.png"
    # The labelled image must be taller than a bare QR (label band added).
    bare = generate_qr("1")
    assert image.size[1] > bare.size[1]
