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


def test_generate_qr_with_label_no_logo_keeps_size_but_changes_centre_pixels() -> None:
    bare = generate_qr("hello", box_size=10, border=4)
    labeled = generate_qr("hello", label="42", box_size=10, border=4)

    # Without a logo the label is drawn as a centred badge ON the QR
    # pattern (same rounded white pad as the logo embed), so the
    # overall image size is preserved while the centre pixels change.
    assert labeled.size == bare.size
    # The pixels must differ (the centre badge changes the centre
    # region from the bare QR's monochrome modules to a white pad
    # carrying the label glyph).
    assert labeled.tobytes() != bare.tobytes()

    # Round-trip via PNG bytes to confirm the image is a real PNG.
    buf = io.BytesIO()
    labeled.save(buf, format="PNG")
    data = buf.getvalue()
    assert data.startswith(PNG_MAGIC)

    reopened = Image.open(io.BytesIO(data))
    reopened.load()
    assert reopened.size == labeled.size


def test_generate_qr_with_label_and_logo_increases_height() -> None:
    """When both a label and a logo are supplied the logo occupies the
    centre and the label is rendered as a clean white band below the
    QR, so the image height grows by the band's height."""
    logo = Image.new("RGB", (32, 32), (255, 165, 0))
    bare = generate_qr("hello", logo=logo, box_size=10, border=4)
    labeled = generate_qr(
        "hello", label="42", logo=logo, box_size=10, border=4,
    )

    assert labeled.size[0] == bare.size[0]
    assert labeled.size[1] > bare.size[1]
    assert labeled.tobytes() != bare.tobytes()

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
    # The labelled image (no logo, no template) keeps the bare QR's
    # size: the label is drawn as a centred badge on the QR rather
    # than in a band below it, so width and height both match the
    # bare render. The pixel content must differ (proving the badge
    # was actually drawn rather than the bare QR being returned).
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


# --- Custom QR designs: review v1 follow-ups -----------------------


def test_pad_logo_keeps_white_margin_around_logo_corners() -> None:
    """Review v1 issue 2: ``_pad_logo`` must keep a continuous white
    margin around the entire logo, including its four corners.

    The previous implementation drew a rounded rectangle with a fixed
    ``target_size_px // 8`` corner radius edge-to-edge on the canvas,
    while the logo only occupied the inner ~80%. At
    ``target_size_px=256`` the corner radius was 32 px but the offset
    between the canvas edge and the logo was only 26 px, so the
    rounded curve cut inward past the logo's bounding box and left
    each corner without the promised white ring.

    This test:

    1. Builds a small saturated-orange logo and pads it.
    2. Asserts the four canvas corners are transparent (the rounded
       cut still works).
    3. Asserts each of the four logo corners themselves is solid
       orange (the logo was actually pasted).
    4. Asserts each pixel just outside the logo's corner (one or two
       pixels into the margin in both axes) is solid white, which is
       what proves the rounded curve does not eat into the margin
       around the logo.
    """
    from qr_generator import LOGO_WORK_SIZE, _pad_logo

    logo = Image.new("RGB", (100, 100), (255, 165, 0))
    pad = _pad_logo(logo, LOGO_WORK_SIZE)
    assert pad.mode == "RGBA"
    assert pad.size == (LOGO_WORK_SIZE, LOGO_WORK_SIZE)

    # Canvas corners must be cut by the rounded curve (transparent).
    for corner in [(0, 0), (LOGO_WORK_SIZE - 1, 0),
                   (0, LOGO_WORK_SIZE - 1),
                   (LOGO_WORK_SIZE - 1, LOGO_WORK_SIZE - 1)]:
        pixel = pad.getpixel(corner)
        assert pixel[3] == 0, (
            f"canvas corner {corner} should be transparent, got {pixel}"
        )

    # The logo lands at offset = (W - 100) // 2 = 78, so its corners
    # are at (78, 78), (177, 78), (78, 177), (177, 177).
    offset = (LOGO_WORK_SIZE - 100) // 2
    far = offset + 99
    logo_corners = [
        (offset, offset),
        (far, offset),
        (offset, far),
        (far, far),
    ]
    for px, py in logo_corners:
        pixel = pad.getpixel((px, py))
        assert pixel == (255, 165, 0, 255), (
            f"logo corner ({px},{py}) should be solid orange, got {pixel}"
        )

    # The pixel two steps outside each logo corner (diagonally into
    # the margin, both axes) must be solid white. If the rounded
    # curve still cut inward past the logo's bounding box, this would
    # be transparent (or partially transparent due to anti-aliasing)
    # rather than fully opaque white.
    margin_corners = [
        (offset - 2, offset - 2),
        (far + 2, offset - 2),
        (offset - 2, far + 2),
        (far + 2, far + 2),
    ]
    for px, py in margin_corners:
        pixel = pad.getpixel((px, py))
        assert pixel == (255, 255, 255, 255), (
            f"margin corner ({px},{py}) should be solid white, got {pixel}"
        )


def test_square_gradient_template_renders() -> None:
    """Review v1 issue 3: the registry must include at least one
    template that exercises the ``square_gradient`` colour-mask branch
    so ``_resolve_color_mask`` does not carry a dead path. The template
    must round-trip through ``generate_qr`` and produce a valid image
    whose pixels are not the legacy plain-black-on-white render."""
    from qr_generator import generate_qr, get_template, list_templates

    matching = [
        t for t in list_templates()
        if t["spec"].get("color_mask_kind") == "square_gradient"
    ]
    assert matching, (
        "expected at least one template with color_mask_kind=square_gradient"
    )

    template_id = matching[0]["id"]
    # Sanity check the registry resolves the spec.
    assert get_template(template_id)["spec"]["color_mask_kind"] == "square_gradient"

    legacy = generate_qr("hello").convert("RGB")
    styled = generate_qr("hello", template_id=template_id).convert("RGB")
    assert legacy.size == styled.size
    # A known-active QR module on the legacy render must not be plain
    # black on the styled render. box_size=10, border=4 -> module 0
    # starts at pixel 40.
    sample = (60, 60)
    assert legacy.getpixel(sample) == (0, 0, 0)
    assert styled.getpixel(sample) != (0, 0, 0)


# --- Premium label band below QR (FEAT-002) ------------------------


def test_label_band_is_white_with_no_outline() -> None:
    """The label band drawn below the QR (when a label is supplied
    alongside a logo) must be a clean white region with no surrounding
    outline rectangle. Sample pixels just under the QR pattern at the
    leftmost and rightmost columns: both must be pure white. If a
    rectangle outline were drawn around the band the leftmost/rightmost
    pixel of that row would be the outline colour."""
    logo = Image.new("RGB", (32, 32), (255, 165, 0))
    bare = generate_qr("hello", logo=logo)
    labelled = generate_qr("hello", label="42", logo=logo).convert("RGB")
    qr_w, qr_h = bare.size

    sample_y = qr_h + 2
    assert labelled.getpixel((0, sample_y)) == (255, 255, 255)
    assert labelled.getpixel((qr_w - 1, sample_y)) == (255, 255, 255)
    # Mid-row sample: also pure white in a region that does not contain
    # the glyph (the glyph sits a few pixels lower, after pad_y).
    assert labelled.getpixel((qr_w // 4, sample_y)) == (255, 255, 255)


def test_label_text_uses_template_foreground_colour() -> None:
    """When a template is supplied the label text is drawn in the
    template's foreground colour. ``running-track`` is solid red with
    front_color = (211, 47, 47).

    This contract holds across BOTH layouts:
    * (label, no logo): the centre badge sits on a white rounded pad
      and the glyph is drawn in red. We sample inside a small box
      centred on the QR's centre and require many red-ish pixels and
      zero pure-black pixels (any glyph pixel must be red, never
      plain black).
    * (label, with logo): the band below the QR uses the same red.
    """
    # --- (a) centre-badge layout (no logo) -------------------------
    bare = generate_qr("hello", template_id="running-track")
    labelled = generate_qr(
        "hello", label="42", template_id="running-track",
    ).convert("RGB")
    qr_w, qr_h = bare.size
    assert labelled.size == bare.size

    # Sample a centred box ~22% of the image (matches the
    # embedded_image_ratio used by StyledPilImage).
    half = max(8, int(min(qr_w, qr_h) * 0.22) // 2)
    cx, cy = qr_w // 2, qr_h // 2
    red_count = 0
    black_count = 0
    for y in range(cy - half, cy + half):
        for x in range(cx - half, cx + half):
            r, g, b = labelled.getpixel((x, y))
            if r > 150 and g < 100 and b < 100:
                red_count += 1
            if (r, g, b) == (0, 0, 0):
                black_count += 1
    assert red_count >= 50, (
        f"expected many red-ish pixels inside the centre badge region; "
        f"got {red_count}"
    )
    assert black_count == 0, (
        f"centre badge must not contain any pure-black pixels (text "
        f"must be drawn in the template's foreground colour, not "
        f"black); got {black_count}"
    )

    # --- (b) band-below layout (with logo) -------------------------
    logo = Image.new("RGB", (32, 32), (255, 165, 0))
    bare_band = generate_qr("hello", template_id="running-track", logo=logo)
    labelled_band = generate_qr(
        "hello", label="42", template_id="running-track", logo=logo,
    ).convert("RGB")
    bw, bh = bare_band.size
    band_red = 0
    band_black = 0
    for y in range(bh, labelled_band.size[1]):
        for x in range(bw):
            r, g, b = labelled_band.getpixel((x, y))
            if r > 150 and g < 100 and b < 100:
                band_red += 1
            if (r, g, b) == (0, 0, 0):
                band_black += 1
    assert band_red >= 50, (
        f"expected the label glyphs to produce many red-ish pixels in "
        f"the band; got {band_red}"
    )
    assert band_black == 0, (
        f"label band must not contain any pure-black pixels; "
        f"got {band_black}"
    )


def test_label_uses_bundled_truetype_font_not_bitmap_default() -> None:
    """The label is rendered with the bundled Plus Jakarta Sans Bold
    TTF, not PIL's bitmap default font. TTF glyphs are anti-aliased
    and therefore introduce many partial-coverage pixels along the
    strokes; PIL's bitmap default font renders only pure black or
    pure white at the glyph boundary. We compare the count of
    non-pure-white, non-pure-black pixels inside the centre badge
    region: the production (TTF) render must produce at least 3x as
    many as a control rendered with the bitmap default font centred
    at the same coordinates."""
    from PIL import Image as _Image
    from PIL import ImageDraw as _ImageDraw
    from PIL import ImageFont as _ImageFont

    label = "42"
    # No logo -> the label lands as a centred badge on the QR.
    labelled = generate_qr("hello", label=label).convert("RGB")
    qr_w, qr_h = labelled.size

    # Build a control RGBA canvas of the same overall image size and
    # centre-draw the same label with the bitmap default font at the
    # QR's centre.
    control = _Image.new("RGB", (qr_w, qr_h), (255, 255, 255))
    cdraw = _ImageDraw.Draw(control)
    default_font = _ImageFont.load_default()
    bbox = cdraw.textbbox((0, 0), label, font=default_font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_x = (qr_w - text_w) // 2 - bbox[0]
    text_y = (qr_h - text_h) // 2 - bbox[1]
    cdraw.text((text_x, text_y), label, fill=(0, 0, 0), font=default_font)

    # The centre badge sits inside a square ~22% of the image (matches
    # embedded_image_ratio). Count anti-aliased pixels in that box on
    # both images.
    half = max(8, int(min(qr_w, qr_h) * 0.22) // 2)
    cx, cy = qr_w // 2, qr_h // 2

    def _count_anti_aliased(img: _Image.Image) -> int:
        count = 0
        for y in range(cy - half, cy + half):
            for x in range(cx - half, cx + half):
                px = img.getpixel((x, y))
                if px not in {(0, 0, 0), (255, 255, 255)}:
                    count += 1
        return count

    ttf_count = _count_anti_aliased(labelled)
    default_count = _count_anti_aliased(control)

    assert ttf_count >= 3 * max(1, default_count), (
        f"expected anti-aliased TTF glyphs to produce at least 3x the "
        f"non-pure-black/white pixel count of PIL's bitmap default "
        f"font; got ttf={ttf_count}, default={default_count}"
    )


def test_label_color_from_spec_picks_correct_stop() -> None:
    """Each ``color_mask_kind`` maps onto its documented stop. An
    unknown kind raises ``ValueError`` mirroring the closed-set policy
    of ``_resolve_color_mask``."""
    from qr_generator import _label_color_from_spec

    assert _label_color_from_spec(
        {"color_mask_kind": "solid", "front_color": (1, 2, 3)},
    ) == (1, 2, 3)
    assert _label_color_from_spec(
        {"color_mask_kind": "radial_gradient", "center_color": (4, 5, 6)},
    ) == (4, 5, 6)
    assert _label_color_from_spec(
        {"color_mask_kind": "square_gradient", "center_color": (7, 8, 9)},
    ) == (7, 8, 9)
    assert _label_color_from_spec(
        {"color_mask_kind": "horizontal_gradient", "left_color": (10, 11, 12)},
    ) == (10, 11, 12)
    assert _label_color_from_spec(
        {"color_mask_kind": "vertical_gradient", "top_color": (13, 14, 15)},
    ) == (13, 14, 15)

    with pytest.raises(ValueError, match="unknown color_mask_kind"):
        _label_color_from_spec({"color_mask_kind": "rainbow"})


# --- Centre-badge label (FEAT-002) ---------------------------------


def test_label_centre_badge_white_pad_around_glyph() -> None:
    """The centre badge sits on a white rounded pad behind the glyph
    so the QR remains scannable. Sample several pixels inside the
    centre region but outside the glyph strokes (a small ring around
    the centre on the diagonal axes that should miss the strokes of a
    short numeric label) and assert at least one of them is pure
    white. This locks the white-backdrop-for-scannability behaviour
    in place."""
    labelled = generate_qr("hello", label="42").convert("RGB")
    qr_w, qr_h = labelled.size
    cx, cy = qr_w // 2, qr_h // 2

    # Pick points on the diagonal axes around the centre at radius
    # ~7% of the QR width. For a short numeric label like "42" sitting
    # inside the inner ~80% of a 22%-of-the-image badge, points off
    # the diagonal axes at this radius should land on the white pad
    # rather than on the glyph strokes.
    radius = max(4, int(qr_w * 0.07))
    sample_points = [
        (cx + radius, cy + radius),
        (cx - radius, cy + radius),
        (cx + radius, cy - radius),
        (cx - radius, cy - radius),
    ]
    pixels = [labelled.getpixel(p) for p in sample_points]
    assert any(p == (255, 255, 255) for p in pixels), (
        f"expected at least one pure-white pixel on the centre-badge "
        f"diagonal ring (the white rounded pad behind the glyph); got "
        f"{pixels}"
    )


def test_label_in_centre_bumps_error_correction_to_h() -> None:
    """A centre label upgrades error correction to H, mirroring the
    logo path. A payload that fits at M (no label) overflows at H
    (centre label)."""
    # Without the label we stay at M and 'A' * 2000 fits at version 40.
    img = generate_qr("A" * 2000)
    assert isinstance(img, Image.Image)

    # With a centre label we move to H and the same payload exceeds
    # the H-mode capacity, surfacing as a ValueError from the
    # underlying qrcode library.
    with pytest.raises(ValueError):
        generate_qr("A" * 2000, label="42")


def test_label_centre_badge_long_label_at_floor_still_legible_and_white_padded() -> None:
    """Auto-fit hits the 24 px font floor with a label long enough
    that even at the floor the rendered text is wider than the inner
    pad, so ``_pad_logo``'s ``thumbnail`` step downscales the scratch
    image. Pin three things at the floor: (a) the bare QR's size is
    preserved (the centre-badge dispatch is taken, not the band-below
    layout), (b) the white rounded pad is still present behind the
    glyph so the QR remains scannable, and (c) the glyph itself
    survives the LANCZOS downscale so the label is actually
    legible (not just a blank white pad). Without (c) a regression
    that produced an empty rounded white square would silently pass
    the white-pad assertion alone."""
    long_label = "ABCDEFGHIJKLMNOP" * 2  # 32 chars
    bare = generate_qr("hello")
    labelled = generate_qr("hello", label=long_label).convert("RGB")
    assert labelled.size == bare.size

    qr_w, qr_h = labelled.size
    cx, cy = qr_w // 2, qr_h // 2
    half = max(8, int(min(qr_w, qr_h) * 0.22) // 2)

    # The pad is mostly white (the glyph occupies a small fraction of
    # the badge area even when downscaled). Require a generous count
    # of pure-white pixels inside the centre region so a regression
    # that lost the white pad would fail loudly. The symmetric
    # assertion below pins the glyph itself: a minimum count of
    # non-white pixels inside the same region, so a regression that
    # turned the badge into a blank rounded-white square (e.g. the
    # downscale destroying the glyph entirely) would fail too.
    # Empirical observation in this fixture: ~3 567 white and ~277
    # non-white (anti-aliased glyph halo) pixels in a 3 844-pixel
    # sample; the floors below leave comfortable margin.
    white = 0
    nonwhite = 0
    for y in range(cy - half, cy + half):
        for x in range(cx - half, cx + half):
            pixel = labelled.getpixel((x, y))
            if pixel == (255, 255, 255):
                white += 1
            else:
                nonwhite += 1
    sample = (2 * half) * (2 * half)
    assert white >= 200, (
        f"expected the white rounded pad to dominate the centre region "
        f"even at the 24 px font floor; got {white} white pixels in a "
        f"{sample}-pixel sample"
    )
    assert nonwhite >= 30, (
        f"expected the glyph to survive the LANCZOS downscale at the "
        f"24 px font floor (non-white pixels carry the rendered text); "
        f"got only {nonwhite} non-white pixels in a {sample}-pixel "
        f"sample, suggesting the badge degraded into a blank white pad"
    )


def test_label_centre_badge_uses_gradient_template_centre_stop() -> None:
    """The centre badge colour follows the template's representative
    stop for non-solid masks too. ``marathon-fire`` is a
    ``radial_gradient`` with ``center_color=(255, 87, 34)`` (deep
    orange), so the badge text on top of the white pad must include
    many orange-ish pixels and never plain black. This guards the
    gradient branches of ``_label_color_from_spec`` (which feeds the
    badge), which the existing ``running-track`` arm of
    ``test_label_text_uses_template_foreground_colour`` does not
    exercise (solid mask only)."""
    bare = generate_qr("hello", template_id="marathon-fire")
    labelled = generate_qr(
        "hello", label="42", template_id="marathon-fire",
    ).convert("RGB")
    qr_w, qr_h = labelled.size
    assert labelled.size == bare.size

    half = max(8, int(min(qr_w, qr_h) * 0.22) // 2)
    cx, cy = qr_w // 2, qr_h // 2
    orange = 0
    black = 0
    for y in range(cy - half, cy + half):
        for x in range(cx - half, cx + half):
            r, g, b = labelled.getpixel((x, y))
            # The center_color is (255, 87, 34); allow anti-aliasing
            # halos by accepting any pixel where red dominates and
            # blue stays low.
            if r > 200 and 50 < g < 150 and b < 80:
                orange += 1
            if (r, g, b) == (0, 0, 0):
                black += 1
    assert orange >= 20, (
        f"expected gradient template's center_color (orange) to drive "
        f"the centre-badge glyph colour; got {orange} orange-ish pixels"
    )
    assert black == 0, (
        f"centre badge must not contain any pure-black pixels for a "
        f"non-default template; got {black}"
    )


def test_label_centre_badge_at_small_box_size_carries_template_colour() -> None:
    """At a small ``box_size`` the 256 px badge canvas is downscaled
    aggressively to fit inside the QR's 22% centre region. Confirm
    the badge still carries the template's foreground colour and
    sits on a white pad even at the small render used by
    :func:`render_template_preview` (``box_size=4, border=2``).
    This pins the tiny-QR legibility floor for the centre-badge
    layout."""
    img = generate_qr(
        "hello",
        label="42",
        box_size=4,
        border=2,
        template_id="running-track",
    ).convert("RGB")
    qr_w, qr_h = img.size
    assert qr_w == qr_h

    cx, cy = qr_w // 2, qr_h // 2
    half = max(4, int(min(qr_w, qr_h) * 0.22) // 2)
    red = 0
    white = 0
    black = 0
    for y in range(cy - half, cy + half):
        for x in range(cx - half, cx + half):
            r, g, b = img.getpixel((x, y))
            if r > 150 and g < 100 and b < 100:
                red += 1
            if (r, g, b) == (255, 255, 255):
                white += 1
            if (r, g, b) == (0, 0, 0):
                black += 1
    assert red >= 5, (
        f"expected the template's red foreground to reach the badge "
        f"glyph at small box_size; got {red} red-ish pixels"
    )
    assert white >= 50, (
        f"expected the white rounded pad to remain visible behind the "
        f"glyph at small box_size; got {white} white pixels"
    )
    assert black == 0, (
        f"centre badge must not contain any pure-black pixels for a "
        f"templated render; got {black}"
    )
