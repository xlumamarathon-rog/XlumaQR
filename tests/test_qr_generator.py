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
    assert LOGO_WORK_SIZE == 1024


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


# --- Vector downloads (FEAT-002) -----------------------------------


def test_generate_qr_svg_returns_svg_root_with_no_background_rect() -> None:
    """The SVG output must be well-formed XML with a transparent
    background. Specifically: no ``<rect>`` element whose width and
    height span the whole canvas, since that would re-introduce the
    solid-white background the user complained about.
    """
    import xml.etree.ElementTree as ET

    from qr_generator import generate_qr_svg

    svg = generate_qr_svg("hello")
    assert isinstance(svg, str)

    head = svg.lstrip()
    assert head.startswith("<?xml") or head.startswith("<svg")

    # Well-formed XML.
    root = ET.fromstring(svg)
    # Strip the namespace prefix off the localname so the rect search
    # below can match against the local element name.
    ns = "{http://www.w3.org/2000/svg}"
    assert root.tag == f"{ns}svg"

    width_attr = root.attrib.get("width", "")
    height_attr = root.attrib.get("height", "")

    # Walk every <rect> in the document and assert none of them spans
    # the full canvas. We accept either pixel-equal or 100%-style
    # coverage as a "background rect".
    for rect in root.iter(f"{ns}rect"):
        rw = rect.attrib.get("width", "")
        rh = rect.attrib.get("height", "")
        rx = rect.attrib.get("x", "0")
        ry = rect.attrib.get("y", "0")
        spans_full_canvas = (
            rx in {"0", "0.0"}
            and ry in {"0", "0.0"}
            and rw == width_attr
            and rh == height_attr
        )
        spans_full_via_percent = rw == "100%" and rh == "100%"
        assert not spans_full_canvas, (
            f"found a full-canvas <rect> at ({rx}, {ry}) sized "
            f"{rw}x{rh}; the SVG must keep a transparent background"
        )
        assert not spans_full_via_percent, (
            "found a 100%x100% <rect>; the SVG must keep a "
            "transparent background"
        )


def test_generate_qr_svg_module_count_matches_active_modules() -> None:
    """For the default template (square module drawer) the SVG must
    contain exactly one ``<rect>`` per on-module. We measure the
    on-module count independently via a fresh ``qrcode.QRCode`` and
    only count rects whose ``width`` attribute equals ``box_size`` so
    the optional logo/label rects are excluded."""
    import xml.etree.ElementTree as ET

    import qrcode as _qr

    from qr_generator import generate_qr_svg

    box_size = 10
    border = 4
    qr = _qr.QRCode(box_size=box_size, border=border)
    qr.add_data("hello")
    qr.make(fit=True)
    expected = sum(1 for row in qr.modules for cell in row if cell)

    svg = generate_qr_svg("hello", box_size=box_size, border=border)
    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"

    rects = [
        r for r in root.iter(f"{ns}rect")
        if r.attrib.get("width") == str(box_size)
        and r.attrib.get("height") == str(box_size)
    ]
    assert len(rects) == expected, (
        f"expected {expected} module rects, got {len(rects)}"
    )


def test_generate_qr_svg_solid_template_uses_front_color() -> None:
    """``running-track`` is a solid red template with
    ``front_color=(211, 47, 47)``. The SVG must reference that colour
    on the on-module ``<g fill=...>``."""
    from qr_generator import generate_qr_svg

    svg = generate_qr_svg("hello", template_id="running-track")
    # Accept either ``rgb(211, 47, 47)`` or ``rgb(211,47,47)`` (any
    # whitespace style) so the test does not over-fit the formatting.
    assert (
        'fill="rgb(211, 47, 47)"' in svg
        or 'fill="rgb(211,47,47)"' in svg
    )


def test_generate_qr_svg_radial_gradient_template_emits_radial_def() -> None:
    """``marathon-fire`` uses a radial gradient mask. The SVG must
    emit a ``<radialGradient>`` def and the on-module ``<g>`` must
    reference it via ``url(#...)``."""
    from qr_generator import generate_qr_svg

    svg = generate_qr_svg("hello", template_id="marathon-fire")
    assert "<radialGradient" in svg
    assert 'fill="url(#' in svg


def test_generate_qr_svg_with_logo_embeds_base64_image() -> None:
    """When a logo is supplied the SVG embeds it as a base64 PNG data
    URI inside an ``<image>`` element. The QR pattern around the logo
    stays vector; the trade-off is documented in the function's
    docstring."""
    from qr_generator import generate_qr_svg

    logo = Image.new("RGB", (64, 64), (255, 165, 0))
    svg = generate_qr_svg("hello", logo=logo)
    assert "<image" in svg
    assert "data:image/png;base64," in svg


def test_generate_qr_svg_with_label_no_logo_has_centre_text() -> None:
    """Centre-badge layout: label without logo emits a ``<text>``
    element with ``text-anchor="middle"`` and the literal label
    text."""
    from qr_generator import generate_qr_svg

    svg = generate_qr_svg("hello", label="42")
    assert "<text" in svg
    assert ">42<" in svg
    assert 'text-anchor="middle"' in svg


def test_generate_qr_svg_label_with_logo_extends_viewbox_height() -> None:
    """Band-below layout: label with a logo extends the SVG height
    beyond its width to accommodate the band."""
    import xml.etree.ElementTree as ET

    from qr_generator import generate_qr_svg

    logo = Image.new("RGB", (32, 32), (255, 165, 0))
    svg = generate_qr_svg("hello", label="42", logo=logo)
    root = ET.fromstring(svg)
    viewbox = root.attrib.get("viewBox", "")
    parts = viewbox.split()
    assert len(parts) == 4
    _, _, w, h = parts
    assert float(h) > float(w), (
        f"viewBox height {h} should exceed width {w} for band-below layout"
    )


def test_generate_qr_svg_label_is_xml_escaped() -> None:
    """User-supplied label text containing XML-special characters
    must be escaped before being embedded in the SVG, so the output
    stays well-formed XML."""
    import xml.etree.ElementTree as ET

    from qr_generator import generate_qr_svg

    svg = generate_qr_svg("hello", label="<script>&'\"")
    # Well-formed XML even with unsafe characters in the label.
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    # The literal ``<script>`` token (with its angle brackets) must
    # NOT appear in the output: it would reopen as an element. The
    # escaped form (``&lt;script&gt;``) is what the parser would
    # serialise back to, but ``ET.fromstring`` succeeds either way.
    assert "<script>" not in svg


def test_pack_pdf_vector_no_image_xobjects_for_no_logo_batch() -> None:
    """With no logo every QR module is drawn as a reportlab vector
    primitive, so the PDF body contains zero image XObjects.

    The PDF spec writes the dictionary as ``/Subtype /Image`` (with a
    space) by default in reportlab, but other producers may use
    ``/Subtype/Image`` (no space). We sum both substrings to be robust
    against either form. The image-XObject count is invariant under
    content-stream compression: a raster-embedded QR would still
    produce ``/Subtype /Image`` references in the PDF's resource
    dictionary regardless of whether the content stream is FlateDecode'd.
    That makes this assertion strictly stronger than a substring grep
    for the rectangle operator (which would force the production
    path to disable PDF compression just to keep the test cheap).

    Also asserts a non-trivial body length and a positive page count
    so a regression where ``_pack_pdf_vector`` early-returns an empty
    PDF (or skips the per-module draw loop) cannot ship green: an
    empty/short PDF satisfies the zero-image-XObject and ``%PDF-``
    magic checks but fails the body-size and page-count floors.
    """
    import math

    from qr_generator import _pack_pdf_vector, generate_sequence_render_plan

    count = 3
    plans = list(
        generate_sequence_render_plan(start=1, count=count, padding=2, prefix="")
    )
    events = list(_pack_pdf_vector(iter(plans)))
    result = [e for e in events if e[0] == "result"][0]
    body = result[1]
    assert body.startswith(b"%PDF-")

    image_xobjects = body.count(b"/Subtype /Image") + body.count(b"/Subtype/Image")
    assert image_xobjects == 0, (
        f"expected zero image XObjects in a no-logo vector PDF batch, "
        f"got {image_xobjects}"
    )
    # Positive draw-evidence: a vector PDF with three QR modules
    # encoded comfortably exceeds 2 KB even with FlateDecode applied.
    assert len(body) > 2000, (
        f"expected the vector PDF body to be non-trivial; got {len(body)} bytes"
    )
    # Page-count floor: ``_pack_pdf_vector`` lays the batch out on a
    # 4-row x 3-col grid (12 per page) so a 3-item batch must render
    # at least one page object. A regression that skips the per-item
    # render path entirely would still emit reportlab's PDF skeleton
    # but would not produce ``/Type /Page`` (singular) references.
    per_page = 4 * 3
    expected_pages = math.ceil(count / per_page)
    page_marker_count = body.count(b"/Type /Page\n")
    assert page_marker_count >= expected_pages, (
        f"expected at least {expected_pages} ``/Type /Page`` references "
        f"in the PDF body, got {page_marker_count}"
    )


def test_pack_pdf_vector_logo_batch_has_at_most_one_image_xobject_per_qr() -> None:
    """When a logo is supplied, the centre region is embedded as a
    raster (small region trade-off documented in :func:`_pack_pdf_vector`).
    The image XObject count must equal the batch size: one per QR for
    the centre logo, not one per page worth of modules."""
    from qr_generator import _pack_pdf_vector, generate_sequence_render_plan

    logo = Image.new("RGB", (64, 64), (255, 165, 0))
    plans = list(
        generate_sequence_render_plan(
            start=1, count=3, padding=2, prefix="", logo=logo,
        )
    )
    events = list(_pack_pdf_vector(iter(plans)))
    result = [e for e in events if e[0] == "result"][0]
    body = result[1]
    image_xobjects = body.count(b"/Subtype /Image") + body.count(b"/Subtype/Image")
    # reportlab may dedupe identical embedded images; the contract is
    # "at most one per QR", not "exactly N". With a shared padded logo
    # reused across the batch reportlab emits a single image XObject.
    assert 1 <= image_xobjects <= 3, (
        f"expected 1..3 image XObjects (one per QR upper bound), "
        f"got {image_xobjects}"
    )


# --- Vector downloads (FEAT-002) v1 review fix-up ------------------


@pytest.mark.parametrize(
    "template_id, drawer_kind",
    [
        ("cycling-roadie", "vertical_bars"),
        ("running-track", "horizontal_bars"),
        ("duathlon-relay", "gapped_square"),
        ("marathon-medal", "circle"),
    ],
)
def test_generate_qr_svg_drawer_kind_emits_expected_primitives(
    template_id: str, drawer_kind: str,
) -> None:
    """Each non-square module drawer kind produces SVG primitives of
    the right shape. The bar drawers run-merge consecutive on-modules
    into a single rect (so the rendered <rect> count is strictly less
    than the on-module count for any non-trivial QR), the circle
    drawer emits <circle> elements, and the gapped_square drawer
    emits inset <rect>s (width strictly less than the rendered
    box_size). These four kinds were uncovered by the v1 review."""
    import xml.etree.ElementTree as ET

    import qrcode as _qr

    from qr_generator import generate_qr_svg, get_template

    box_size = 10
    border = 4
    svg = generate_qr_svg(
        "hello",
        box_size=box_size,
        border=border,
        template_id=template_id,
    )
    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"

    # Sanity: the resolved spec really uses the expected drawer kind.
    spec = get_template(template_id)["spec"]
    assert spec["module_drawer_kind"] == drawer_kind

    # Independently count on-modules for the same payload + ECC level
    # so the bar-drawer assertion has a real upper bound.
    qr = _qr.QRCode(box_size=box_size, border=border)
    qr.add_data("hello")
    qr.make(fit=True)
    on_modules = sum(1 for row in qr.modules for cell in row if cell)

    if drawer_kind == "circle":
        # Each on-module is a <circle>; <rect> count is zero.
        circles = list(root.iter(f"{ns}circle"))
        assert len(circles) == on_modules, (
            f"expected {on_modules} circles, got {len(circles)}"
        )
        assert not list(root.iter(f"{ns}rect"))
    elif drawer_kind == "gapped_square":
        # Inset rects: width must be strictly less than box_size and
        # the on-module count is the rect count.
        rects = list(root.iter(f"{ns}rect"))
        assert len(rects) == on_modules
        for r in rects:
            w = float(r.attrib["width"])
            assert w < float(box_size), (
                f"gapped_square rect width {w} should be inset below "
                f"box_size {box_size}"
            )
    else:
        # Bar drawers: run-merge means rect count <= on-module count.
        # We additionally assert the merge actually happened (count is
        # strictly less, since "hello" produces at least one same-row
        # or same-column run of on-modules in a real QR).
        rects = list(root.iter(f"{ns}rect"))
        assert 0 < len(rects) < on_modules, (
            f"expected run-merged {drawer_kind} rect count strictly "
            f"less than on-module count {on_modules}; got {len(rects)}"
        )
        # And the rect dimensions reflect the run direction.
        if drawer_kind == "vertical_bars":
            # Width is exactly box_size; height is a multiple of box_size.
            for r in rects:
                assert r.attrib["width"] == str(box_size)
                assert int(r.attrib["height"]) % box_size == 0
        else:  # horizontal_bars
            for r in rects:
                assert r.attrib["height"] == str(box_size)
                assert int(r.attrib["width"]) % box_size == 0


@pytest.mark.parametrize(
    "template_id",
    ["running-energy", "marathon-sunset"],
)
def test_generate_qr_svg_linear_gradient_template_emits_linear_def(
    template_id: str,
) -> None:
    """Templates with a horizontal or vertical gradient color mask
    emit a ``<linearGradient>`` def in the SVG and reference it via
    ``url(#qr-fill)`` from the on-module ``<g>``. ``running-energy``
    is horizontal (left->right colour sweep) and ``marathon-sunset``
    is vertical (top->bottom). Both gradient kinds were uncovered by
    the v1 review.
    """
    from qr_generator import generate_qr_svg

    svg = generate_qr_svg("hello", template_id=template_id)
    assert "<linearGradient" in svg
    assert "</linearGradient>" in svg
    assert 'fill="url(#' in svg
    # Two stops, one at 0% and one at 100%.
    assert 'offset="0%"' in svg
    assert 'offset="100%"' in svg


def test_generate_qr_svg_no_global_crisp_edges_on_curved_drawers() -> None:
    """``shape-rendering="crispEdges"`` disables anti-aliasing, which
    is right for grid-aligned squares but produces stair-stepped
    arcs on circles and rounded rects. The attribute must NOT sit on
    the root ``<svg>`` (it would scope to every shape including the
    centre logo / badge), and must be absent from the modules ``<g>``
    when the drawer kind is curved (circle, rounded). The v1 review
    flagged this as a visible-quality regression on curved-drawer
    templates.
    """
    from qr_generator import generate_qr_svg

    # Curved drawer: must not carry crispEdges anywhere in the file.
    svg = generate_qr_svg("hello", template_id="marathon-fire")  # circle
    assert 'shape-rendering="crispEdges"' not in svg

    # Square-like drawer: the attribute may appear on the modules
    # group (it is right for grid-aligned squares) but must NOT be on
    # the root <svg>.
    import xml.etree.ElementTree as ET

    svg_square = generate_qr_svg("hello")  # default = square
    root = ET.fromstring(svg_square)
    assert "shape-rendering" not in root.attrib


@pytest.mark.parametrize(
    "template_id, drawer_kind",
    [
        # default template (no template_id) drives the ``square`` drawer.
        (None, "square"),
        ("duathlon-relay", "gapped_square"),
        ("cycling-roadie", "vertical_bars"),
        ("running-track", "horizontal_bars"),
    ],
)
def test_generate_qr_svg_grid_aligned_drawer_emits_crisp_edges(
    template_id: str | None, drawer_kind: str,
) -> None:
    """v2 review issue 3: positive assertion that the grid-aligned
    module drawer kinds opt into ``shape-rendering="crispEdges"`` on
    the modules group. The companion negative test
    (``test_generate_qr_svg_no_global_crisp_edges_on_curved_drawers``)
    pins the curved drawers stay anti-aliased; together they lock the
    full contract so a regression that drops ``crisp_attr`` entirely
    from ``_render_modules_svg`` fails this test instead of silently
    re-AA'ing every grid-aligned QR.
    """
    from qr_generator import generate_qr_svg, get_template

    # Sanity: the resolved spec really uses the expected drawer kind.
    spec_id = template_id if template_id is not None else "default"
    assert get_template(spec_id)["spec"]["module_drawer_kind"] == drawer_kind

    svg = generate_qr_svg("hello", template_id=template_id)
    assert 'shape-rendering="crispEdges"' in svg, (
        f"expected the modules <g> to carry shape-rendering=\"crispEdges\" "
        f"for grid-aligned drawer kind {drawer_kind!r}"
    )


def test_autofit_centre_badge_font_size_tighter_inner_ratio_picks_smaller_size() -> None:
    """v2 review issue 4: the SVG centre-badge path passes
    ``inner_ratio=0.55`` to absorb cross-font glyph-width drift on
    systems without Plus Jakarta Sans installed, while the PIL/PDF
    paths use the default ``0.70``. This test pins that the tighter
    ratio actually engages the autofit step-down loop (i.e. the SVG
    glyph-size is strictly smaller than the PIL/PDF glyph-size for
    the same label and badge area).

    The fixed inputs (label ``"BATCH"``, ``badge_side_px = 400.0``)
    are picked so both calls converge above the 24 px floor: at
    ``inner_ratio=0.70`` the loop returns 80 px and at
    ``inner_ratio=0.55`` it returns 60 px on the bundled Plus Jakarta
    Sans Bold. If the label converged at the floor in both cases the
    assertion would be meaningless (both calls would return 24).
    A future refactor that re-unifies the two callsites to a single
    shared ratio would silently regress and fail this test.
    """
    from qr_generator import _autofit_centre_badge_font_size

    label = "BATCH"
    badge_side_px = 400.0

    loose = _autofit_centre_badge_font_size(label, badge_side_px, inner_ratio=0.70)
    tight = _autofit_centre_badge_font_size(label, badge_side_px, inner_ratio=0.55)

    floor_px = 24
    # Sanity: both calls must clear the floor for the comparison to
    # mean anything (a label that converges at the floor in both
    # cases would yield ``loose == tight == 24`` and pass a strict
    # ``<`` check accidentally).
    assert loose > floor_px, (
        f"label/badge picked so loose autofit clears the {floor_px} px "
        f"floor; got {loose}"
    )
    assert tight > floor_px, (
        f"label/badge picked so tight autofit clears the {floor_px} px "
        f"floor; got {tight}"
    )
    # The contract: a tighter ``inner_ratio`` engages more
    # step-downs and converges on a strictly smaller font size.
    assert tight < loose, (
        f"expected inner_ratio=0.55 to autofit to a smaller font-size "
        f"than inner_ratio=0.70; got tight={tight} loose={loose}"
    )


def test_generate_qr_svg_centre_badge_font_size_uses_tight_ratio() -> None:
    """v2 review issue 4 (integration counterpart): the SVG centre-
    badge ``<text>`` element advertises a ``font-size`` attribute and
    its value matches the tight (0.55) autofit budget rather than the
    PIL/PDF (0.70) budget.

    A future refactor that re-unified the two callsites to a single
    shared ratio (whether 0.70 or any other) would silently regress
    by emitting the loose-budget font size in the SVG. The assertion
    pins the SVG path's choice by comparing against
    :func:`_autofit_centre_badge_font_size` called with the same
    ``inner_ratio=0.55``.
    """
    import re

    from qr_generator import _autofit_centre_badge_font_size, generate_qr_svg

    # ``box_size = 40`` plus a 5-character label (``"BATCH"``)
    # produces a QR canvas where the autofit loop converges above
    # the 24 px floor for both budgets but the tight budget steps
    # down further than the loose one (40 px vs 48 px on the
    # bundled Plus Jakarta Sans Bold). A shorter label like ``"42"``
    # converges identically under both budgets at this scale, and a
    # smaller box_size collapses both budgets to the floor.
    box_size = 40
    border = 4
    svg = generate_qr_svg("hello", label="BATCH", box_size=box_size, border=border)

    m = re.search(r'font-size="(\d+)"', svg)
    assert m is not None, "expected the centre-badge <text> to carry a font-size"
    chosen = int(m.group(1))

    # Recover the QR canvas pixel side from the SVG viewBox so the
    # assertion does not need to know the QR's module count (which
    # depends on the ERROR_CORRECT_H bump that the centre-label path
    # applies internally). The badge side is 22% of the canvas
    # width, matching the SVG layout maths in :func:`generate_qr_svg`.
    vb = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    assert vb is not None
    qr_w = int(vb.group(1))
    badge_side = qr_w * 0.22

    expected_tight = _autofit_centre_badge_font_size(
        "BATCH", badge_side, inner_ratio=0.55,
    )
    expected_loose = _autofit_centre_badge_font_size(
        "BATCH", badge_side, inner_ratio=0.70,
    )
    assert chosen == expected_tight, (
        f"SVG centre-badge font-size should match the tight (0.55) "
        f"autofit budget; got {chosen}, expected {expected_tight}"
    )
    # And the chosen size must actually differ from the loose-budget
    # outcome at this scale; otherwise the test is not exercising the
    # SVG-vs-PIL/PDF divergence.
    assert expected_tight < expected_loose, (
        f"test parameters chosen so the tight budget converges below "
        f"the loose budget; got tight={expected_tight}, loose={expected_loose}"
    )


def test_pack_pdf_vector_bar_drawer_packs_with_zero_image_xobjects() -> None:
    """A PDF batch using a bar-drawer template (``running-track`` =
    horizontal_bars) packs successfully via the run-merge code path,
    has a non-trivial body length, and contains zero image XObjects
    (the no-logo vector contract). The visible bar geometry is hard
    to assert without rasterising the PDF, but the absence of image
    XObjects plus a non-trivial body length proves the run-merge
    path was exercised end-to-end. The v1 review flagged the
    bar-drawer code path as untested in the PDF output."""
    from qr_generator import _pack_pdf_vector, generate_sequence_render_plan

    plans = list(
        generate_sequence_render_plan(
            start=1,
            count=3,
            padding=2,
            prefix="",
            template_id="running-track",
        )
    )
    events = list(_pack_pdf_vector(iter(plans)))
    result = [e for e in events if e[0] == "result"][0]
    body = result[1]
    assert body.startswith(b"%PDF-")
    # Non-trivial body length: at minimum, three QRs of run-merged
    # rectangles plus reportlab's PDF skeleton easily exceed a few KB.
    assert len(body) > 2000, (
        f"expected the vector PDF body to be non-trivial; got "
        f"{len(body)} bytes"
    )
    image_xobjects = body.count(b"/Subtype /Image") + body.count(b"/Subtype/Image")
    assert image_xobjects == 0, (
        f"bar-drawer no-logo PDF batch should have no image XObjects; "
        f"got {image_xobjects}"
    )


def test_generate_sequence_svg_logo_uses_shared_padded_logo_data_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1 review issue 4: ``generate_sequence_svg`` must pre-pad the
    logo once and reuse the same base64 data URL across every emitted
    SVG, not re-pad and re-encode per item.

    The functional contract is "the LANCZOS resize plus PNG encode
    plus base64 inflation runs at most once across the whole
    sequence." Asserting only that every emitted SVG embeds the same
    base64 payload is too weak: Pillow's PNG encoder is deterministic
    for a given input image and ``LOGO_WORK_SIZE``, so a regression
    that re-pads per item would still produce identical bytes across
    items and pass a payload-equality check (the v2 review flagged
    this).

    To pin the actual contract we monkeypatch ``qr_generator._pad_logo``
    with a ``MagicMock`` that wraps the real implementation, run a
    3-item batch via :func:`generate_sequence_svg`, and assert the
    wrapped helper was called exactly once. The wrap preserves the
    original behaviour so the resulting SVGs are byte-for-byte the
    same as before; only the call count is observed.
    """
    from unittest.mock import MagicMock

    import qr_generator as _qrg

    spy = MagicMock(wraps=_qrg._pad_logo)
    monkeypatch.setattr(_qrg, "_pad_logo", spy)

    logo = Image.new("RGB", (64, 64), (255, 165, 0))
    items = list(
        _qrg.generate_sequence_svg(
            start=1, count=3, padding=2, prefix="", logo=logo,
        )
    )
    assert len(items) == 3
    # The pad-logo helper must run exactly once for the whole batch.
    # A regression that reverted to per-item padding would record
    # ``call_count == 3``.
    assert spy.call_count == 1, (
        "generate_sequence_svg must pad and base64-encode the logo "
        f"exactly once per batch; got {spy.call_count} calls for a "
        "3-item batch"
    )
