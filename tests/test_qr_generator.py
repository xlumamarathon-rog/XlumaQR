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
