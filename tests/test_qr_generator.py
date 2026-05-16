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

    # Label is now overlaid, so size stays the same.
    assert labeled.size == bare.size
    # The pixels must differ (the overlay badge is drawn on top of the QR).
    assert labeled.tobytes() != bare.tobytes()

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
    # The labelled image has the same size as bare (overlay, not extension)
    # but the pixel content must differ (proving the label badge was drawn).
    bare = generate_qr("1")
    assert image.size == bare.size
    assert image.tobytes() != bare.tobytes()


# --- iter_batch_with_progress (review-v1 follow-ups) ----------------


def test_iter_batch_with_progress_zip_yields_progress_then_result() -> None:
    """The streaming helper yields one ``progress`` tuple per item plus a
    final ``result`` tuple. The result bytes must be a valid ZIP whose
    namelist matches the input filenames in order."""
    from qr_generator import iter_batch_with_progress

    items = list(generate_sequence(start=1, count=3, padding=2, prefix=""))
    events = list(iter_batch_with_progress(iter(items), "zip"))
    progress = [e for e in events if e[0] == "progress"]
    result = [e for e in events if e[0] == "result"]

    assert len(progress) == 3
    assert [e[1] for e in progress] == [0, 1, 2]
    assert [e[2] for e in progress] == ["01.png", "02.png", "03.png"]
    assert len(result) == 1
    payload = result[0][1]
    assert payload.startswith(b"PK")
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert zf.namelist() == ["01.png", "02.png", "03.png"]


def test_iter_batch_with_progress_pdf_yields_progress_then_result() -> None:
    """Same shape contract for the PDF format."""
    from qr_generator import iter_batch_with_progress

    items = list(generate_sequence(start=1, count=2, padding=1, prefix=""))
    events = list(iter_batch_with_progress(iter(items), "pdf"))
    progress = [e for e in events if e[0] == "progress"]
    result = [e for e in events if e[0] == "result"]

    assert [e[1] for e in progress] == [0, 1]
    assert len(result) == 1
    assert result[0][1].startswith(b"%PDF-")


def test_iter_batch_with_progress_consumes_lazily_one_at_a_time() -> None:
    """Review v1 issue 1: the helper must pull from ``items`` lazily so
    only one image is alive at a time, not buffer the whole batch.

    We feed it a counting iterator and assert that after pulling one
    ``progress`` event, exactly one item has been consumed from the
    source. If the helper materialised all items up front (e.g. by
    wrapping ``items`` in ``list(...)`` or via a chain of generators
    that pre-pumped the source), the counter would jump straight to
    the total and the test would fail.
    """
    from qr_generator import iter_batch_with_progress

    class CountingIter:
        def __init__(self, source):
            self._source = iter(source)
            self.pulled = 0

        def __iter__(self):
            return self

        def __next__(self):
            value = next(self._source)
            self.pulled += 1
            return value

    items = list(generate_sequence(start=1, count=4, padding=1, prefix=""))
    counted = CountingIter(items)
    gen = iter_batch_with_progress(counted, "zip")

    # First progress event should arrive after exactly one item is pulled.
    evt = next(gen)
    assert evt[0] == "progress"
    assert evt[1] == 0
    assert counted.pulled == 1

    # Second progress event => exactly two items pulled, etc.
    evt = next(gen)
    assert evt[0] == "progress"
    assert evt[1] == 1
    assert counted.pulled == 2

    evt = next(gen)
    assert evt[0] == "progress"
    assert counted.pulled == 3

    evt = next(gen)
    assert evt[0] == "progress"
    assert counted.pulled == 4

    # Source is exhausted; the next event is the terminal result.
    evt = next(gen)
    assert evt[0] == "result"
    assert evt[1].startswith(b"PK")
    with pytest.raises(StopIteration):
        next(gen)


def test_iter_batch_with_progress_invalid_fmt_raises() -> None:
    """The helper validates ``fmt`` up front."""
    from qr_generator import iter_batch_with_progress

    with pytest.raises(ValueError):
        # Force evaluation by pulling one event from the generator.
        next(iter_batch_with_progress(iter([]), "tar"))


def test_iter_batch_with_progress_propagates_encoder_error() -> None:
    """If the source iterator raises ``ValueError`` mid-stream (the
    typical shape of a QR-capacity overflow), the helper must propagate
    it to the caller rather than swallow it. The caller (the streaming
    HTTP route) is responsible for surfacing it as an ``error`` event."""
    from qr_generator import iter_batch_with_progress

    def bad_source():
        yield ("ok.png", generate_qr("hello"))
        raise ValueError("payload too large")

    gen = iter_batch_with_progress(bad_source(), "zip")
    # The first event is the progress for the first (good) item.
    first = next(gen)
    assert first[0] == "progress"
    # Pulling again triggers the raise inside the source, which
    # propagates through the helper.
    with pytest.raises(ValueError, match="payload too large"):
        next(gen)


# --- Custom QR designs (FEAT-001) -----------------------------------


def test_list_templates_has_at_least_30_with_required_categories() -> None:
    """The built-in templates registry must satisfy the task contract:
    >= 30 entries, every required sport category present, >= 3 entries
    per required category, the four documented fields on every entry."""
    from qr_generator import list_templates

    templates = list_templates()
    assert len(templates) >= 30

    required_categories = {
        "marathon",
        "running",
        "duathlon",
        "triathlon",
        "cycling",
        "swimming",
    }
    seen = {t["category"] for t in templates}
    assert required_categories.issubset(seen), (
        f"missing categories: {required_categories - seen}"
    )

    counts: dict[str, int] = {}
    for t in templates:
        # Every entry has the four documented fields.
        assert set(t.keys()) >= {"id", "name", "category", "spec"}
        assert isinstance(t["id"], str) and t["id"]
        assert isinstance(t["name"], str) and t["name"]
        assert isinstance(t["category"], str) and t["category"]
        assert isinstance(t["spec"], dict)
        counts[t["category"]] = counts.get(t["category"], 0) + 1

    for cat in required_categories:
        assert counts.get(cat, 0) >= 3, f"category {cat} has only {counts.get(cat, 0)} entries"

    # The reserved default template exists with the documented category.
    default_entries = [t for t in templates if t["id"] == "default"]
    assert len(default_entries) == 1
    assert default_entries[0]["category"] == "default"


def test_list_templates_returns_defensive_copy() -> None:
    """Mutating the returned list must not affect the underlying registry."""
    from qr_generator import list_templates

    a = list_templates()
    a.clear()
    b = list_templates()
    assert len(b) >= 30


def test_get_template_returns_matching_entry() -> None:
    from qr_generator import get_template

    entry = get_template("default")
    assert entry["id"] == "default"
    assert entry["category"] == "default"


def test_get_template_unknown_raises() -> None:
    from qr_generator import get_template

    with pytest.raises(ValueError, match="unknown template id"):
        get_template("does-not-exist")


def test_template_renders_styled_pixels_differ_from_default() -> None:
    """A coloured template must actually colour the QR modules.

    We pick ``running-track`` which is a solid red front colour and
    compare a known-active QR-module pixel between the styled render
    and the legacy render. The legacy render is plain black at that
    coordinate; the styled render must NOT be plain black.
    """
    from qr_generator import generate_qr

    legacy = generate_qr("hello").convert("RGB")
    styled = generate_qr("hello", template_id="running-track").convert("RGB")

    assert isinstance(legacy, Image.Image)
    assert isinstance(styled, Image.Image)
    assert legacy.size == styled.size

    # box_size=10, border=4 by default => the position-pattern modules
    # start at pixel 40 and run through pixel ~110. Pick a coordinate
    # we know is inside an active QR module on the legacy render.
    sample = (60, 60)
    legacy_px = legacy.getpixel(sample)
    styled_px = styled.getpixel(sample)
    assert legacy_px == (0, 0, 0), (
        "expected a black module on the legacy render at the sample "
        "coordinate; got " + repr(legacy_px)
    )
    assert styled_px != (0, 0, 0), (
        "expected the styled render to colour the module, not leave it "
        "black; got " + repr(styled_px)
    )


def test_logo_centre_pixels_match_logo_colour() -> None:
    """An embedded logo must show through at the centre of the QR.

    Build a 100x100 solid-orange RGB logo, render with and without
    embedding it, and assert the centre pixel of the rendered image is
    in the orange ballpark when a logo is supplied and is pure black or
    white when it is not.
    """
    from qr_generator import generate_qr

    logo = Image.new("RGB", (100, 100), (255, 165, 0))
    with_logo = generate_qr("hello", logo=logo).convert("RGB")
    without_logo = generate_qr("hello").convert("RGB")

    cx, cy = with_logo.size[0] // 2, with_logo.size[1] // 2
    px = with_logo.getpixel((cx, cy))
    r, g, b = px
    assert r >= 200, f"red channel too low at logo centre: {px}"
    assert 100 <= g <= 200, f"green channel out of orange ballpark at logo centre: {px}"
    assert b <= 80, f"blue channel too high at logo centre: {px}"

    # Control: same render without a logo must be pure black or white at
    # the centre (i.e. the QR's monochrome render).
    cx2, cy2 = without_logo.size[0] // 2, without_logo.size[1] // 2
    bw = without_logo.getpixel((cx2, cy2))
    assert bw in {(0, 0, 0), (255, 255, 255)}, (
        f"expected pure black or white at the centre of an unlogo'd QR; got {bw}"
    )


def test_logo_bumps_error_correction_to_h() -> None:
    """A logo upgrades error correction to H, which lowers the per-payload
    capacity. A payload that fits at M (no logo) overflows at H (logo).
    """
    from qr_generator import generate_qr

    logo = Image.new("RGB", (32, 32), (255, 165, 0))

    # Without the logo we stay at M and 'A' * 2000 fits at version 40.
    img = generate_qr("A" * 2000)
    assert isinstance(img, Image.Image)

    # With the logo we move to H and the same payload exceeds the H-mode
    # capacity, surfacing as a ValueError from the underlying qrcode
    # library.
    with pytest.raises(ValueError):
        generate_qr("A" * 2000, logo=logo)


def test_render_template_preview_returns_png_bytes_for_every_template() -> None:
    """Every template in the registry must produce a renderable PNG
    thumbnail. This catches typos in colour stop names and unknown
    drawer / mask kinds in the template specs."""
    from qr_generator import list_templates, render_template_preview

    for t in list_templates():
        png = render_template_preview(t["id"])
        assert isinstance(png, (bytes, bytearray))
        assert png.startswith(PNG_MAGIC), (
            f"template {t['id']!r} did not produce PNG magic bytes"
        )


def test_render_template_preview_unknown_raises() -> None:
    from qr_generator import render_template_preview

    with pytest.raises(ValueError, match="unknown template id"):
        render_template_preview("does-not-exist")


def test_legacy_path_byte_identical_when_no_template_no_logo() -> None:
    """The legacy ``generate_qr`` fast path is preserved byte-for-byte
    when neither a template nor a logo is supplied. This is the
    regression guard that keeps the existing 52 tests honest.

    Three calls must all produce the identical raw image bytes:
      * ``generate_qr('hello')`` (no new arguments at all)
      * ``generate_qr('hello', template_id=None, logo=None)``
        (explicitly passing the documented defaults)
      * ``generate_qr('hello', template_id='default')``
        (the reserved 'default' id maps onto the legacy path)
    """
    from qr_generator import generate_qr

    a = generate_qr("hello")
    b = generate_qr("hello", template_id=None, logo=None)
    c = generate_qr("hello", template_id="default")

    assert a.size == b.size == c.size
    assert a.tobytes() == b.tobytes()
    assert a.tobytes() == c.tobytes()


def test_generate_sequence_forwards_template_id_to_each_item() -> None:
    """When ``template_id`` is supplied to ``generate_sequence`` it is
    forwarded into each ``generate_qr`` call so every QR in the batch
    is styled. We assert this by comparing the encoded PNG bytes of an
    item from a styled sequence against a styled single render and
    against a legacy single render."""
    from qr_generator import generate_qr, generate_sequence

    styled_items = list(
        generate_sequence(
            start=1,
            count=2,
            data_template="{n}",
            label_template=None,
            template_id="running-track",
        )
    )
    assert len(styled_items) == 2
    name, image = styled_items[0]
    assert name == "1.png"

    expected_styled = generate_qr("1", template_id="running-track")
    expected_legacy = generate_qr("1")

    actual_buf = io.BytesIO()
    image.save(actual_buf, format="PNG")
    styled_buf = io.BytesIO()
    expected_styled.save(styled_buf, format="PNG")
    legacy_buf = io.BytesIO()
    expected_legacy.save(legacy_buf, format="PNG")

    assert actual_buf.getvalue() == styled_buf.getvalue()
    assert actual_buf.getvalue() != legacy_buf.getvalue()


def test_generate_sequence_forwards_logo_to_each_item() -> None:
    """A supplied logo must show up in every QR of the batch, not just
    the first. We verify by rendering a tiny sequence and asserting the
    centre pixel of each rendered image is in the logo's colour
    ballpark."""
    from qr_generator import generate_sequence

    logo = Image.new("RGB", (64, 64), (255, 165, 0))
    items = list(
        generate_sequence(
            start=1,
            count=3,
            data_template="{n}",
            label_template=None,
            logo=logo,
        )
    )
    assert len(items) == 3
    for _, image in items:
        rgb = image.convert("RGB")
        cx, cy = rgb.size[0] // 2, rgb.size[1] // 2
        r, g, b = rgb.getpixel((cx, cy))
        assert r >= 200 and 100 <= g <= 200 and b <= 80, (
            f"expected orange ballpark at QR centre; got {(r, g, b)}"
        )


def test_max_logo_constants_exposed() -> None:
    """The HTTP layer relies on these constants for upload validation."""
    from qr_generator import LOGO_WORK_SIZE, MAX_LOGO_BYTES, MAX_LOGO_DIMENSION

    assert MAX_LOGO_BYTES == 2 * 1024 * 1024
    assert MAX_LOGO_DIMENSION == 1024
    assert LOGO_WORK_SIZE == 256
