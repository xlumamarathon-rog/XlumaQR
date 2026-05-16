"""Tests for the Flask HTTP layer in ``app``.

These tests use Flask's built-in ``test_client`` to exercise the real
routes and assert on real response bodies (PNG magic, ZIP namelists,
PDF magic). No mocking.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app import app as flask_app

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture()
def client():
    flask_app.config.update(TESTING=True)
    with flask_app.test_client() as client:
        yield client


def test_index_returns_200_with_html(client) -> None:
    rv = client.get("/")
    assert rv.status_code == 200
    body = rv.data
    assert b"<form" in body
    # Both tab labels should be present in the markup.
    assert b"Single QR" in body
    assert b"Sequential Batch" in body


def test_single_returns_png(client) -> None:
    rv = client.post("/api/qr/single", data={"data": "hello"})
    assert rv.status_code == 200
    assert rv.mimetype == "image/png"
    assert rv.data.startswith(PNG_MAGIC)


def test_single_with_label_returns_png(client) -> None:
    rv = client.post(
        "/api/qr/single",
        data={"data": "hello", "label": "42"},
    )
    assert rv.status_code == 200
    assert rv.mimetype == "image/png"
    assert rv.data.startswith(PNG_MAGIC)


def test_batch_zip_contains_correct_filenames(client) -> None:
    rv = client.post(
        "/api/qr/batch",
        data={
            "start": "101",
            "count": "3",
            "padding": "3",
            "prefix": "x_",
            "format": "zip",
        },
    )
    assert rv.status_code == 200
    assert rv.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(rv.data)) as zf:
        names = zf.namelist()
        assert names == ["x_101.png", "x_102.png", "x_103.png"]
        for name in names:
            assert zf.read(name).startswith(PNG_MAGIC)


def test_batch_pdf_returns_pdf(client) -> None:
    rv = client.post(
        "/api/qr/batch",
        data={"start": "1", "count": "3", "format": "pdf"},
    )
    assert rv.status_code == 200
    assert rv.mimetype == "application/pdf"
    assert rv.data.startswith(b"%PDF-")


def test_batch_user_example_101_count_100(client) -> None:
    rv = client.post(
        "/api/qr/batch",
        data={"start": "101", "count": "100", "format": "zip"},
    )
    assert rv.status_code == 200
    assert rv.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(rv.data)) as zf:
        names = zf.namelist()
        assert len(names) == 100
        assert names[0] == "101.png"
        assert names[-1] == "200.png"


def test_batch_invalid_returns_400(client) -> None:
    rv = client.post(
        "/api/qr/batch",
        data={"start": "1", "count": "0"},
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None
    assert "error" in body


# --- Hardening regression tests (semantic-review v1) ----------------


def test_single_missing_data_returns_400(client) -> None:
    """Issue 10: missing-``data`` 400 path on /single."""
    rv = client.post("/api/qr/single", data={})
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body


def test_single_data_too_long_returns_400(client) -> None:
    """Issue 5: very long QR payload is rejected with a clean 400."""
    rv = client.post("/api/qr/single", data={"data": "A" * 5000})
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body


def test_single_box_size_too_large_returns_400(client) -> None:
    """Issue 4: very large box_size is rejected before rendering."""
    rv = client.post("/api/qr/single", data={"data": "hello", "box_size": "100000"})
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body


def test_single_border_zero_is_honoured(client) -> None:
    """Issue 6: explicit ``border=0`` must not be silently rewritten to 4."""
    rv0 = client.post("/api/qr/single", data={"data": "hello", "border": "0"})
    rv4 = client.post("/api/qr/single", data={"data": "hello", "border": "4"})
    assert rv0.status_code == 200
    assert rv4.status_code == 200
    # A border-zero QR must be strictly smaller than a border-4 QR
    # (same payload, same default box_size). If border=0 were silently
    # rewritten to the default, the two byte streams would be identical.
    from PIL import Image as _Image  # local import keeps test scope tidy

    img0 = _Image.open(io.BytesIO(rv0.data))
    img4 = _Image.open(io.BytesIO(rv4.data))
    assert img0.size[0] < img4.size[0]


def test_single_negative_box_size_returns_400(client) -> None:
    """Issue 7: negative sizes get a 400, not a Flask 500."""
    rv = client.post("/api/qr/single", data={"data": "hello", "box_size": "-1"})
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body


def test_batch_count_above_max_returns_400(client) -> None:
    """Issue 3: unbounded count is rejected before rendering."""
    rv = client.post(
        "/api/qr/batch",
        data={"start": "1", "count": "10000000", "format": "zip"},
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body


def test_batch_both_count_and_end_returns_400(client) -> None:
    """Issue 10: both-``count``-and-``end`` 400 path."""
    rv = client.post(
        "/api/qr/batch",
        data={"start": "1", "count": "5", "end": "10"},
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body


def test_batch_unknown_format_returns_400(client) -> None:
    """Issue 10: unknown-``format`` 400 path."""
    rv = client.post(
        "/api/qr/batch",
        data={"start": "1", "count": "3", "format": "xml"},
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body


def test_batch_end_mode_zip(client) -> None:
    """Issue 10: ``end=`` mode is exercised end-to-end."""
    rv = client.post(
        "/api/qr/batch",
        data={"start": "1", "end": "3", "format": "zip"},
    )
    assert rv.status_code == 200
    assert rv.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(rv.data)) as zf:
        assert zf.namelist() == ["1.png", "2.png", "3.png"]


def test_batch_pdf_filename_uses_padded_first_and_last(client) -> None:
    """Issue 8: the Content-Disposition filename pads ``start`` the same way
    as the entries inside the archive (no half-padded asymmetry)."""
    rv = client.post(
        "/api/qr/batch",
        data={
            "start": "1",
            "count": "5",
            "padding": "3",
            "format": "pdf",
        },
    )
    assert rv.status_code == 200
    assert rv.mimetype == "application/pdf"
    disposition = rv.headers.get("Content-Disposition", "")
    # Both sides padded; filename is symmetric.
    assert "qr_batch_001_005.pdf" in disposition
    assert "qr_batch_1_005.pdf" not in disposition


def test_batch_data_template_substitution_end_to_end(client) -> None:
    """Issue v2#4: ``data_template`` must actually substitute ``{n}``.

    Use a template that contains ``{n}`` and run it for two different
    ``start`` values whose decimal representations differ. The encoded
    payloads will then differ ("Z1Z" vs "Z99999Z") and the rendered PNG
    bytes must differ as a result. If the HTTP layer dropped substitution
    or if ``generate_sequence`` regressed, both runs would encode the
    literal string "Z{n}Z" and the bytes would be identical.
    """
    rv_a = client.post(
        "/api/qr/batch",
        data={
            "start": "1",
            "count": "1",
            "format": "zip",
            "data_template": "Z{n}Z",
            "label_template": "",
        },
    )
    rv_b = client.post(
        "/api/qr/batch",
        data={
            "start": "99999",
            "count": "1",
            "format": "zip",
            "data_template": "Z{n}Z",
            "label_template": "",
        },
    )
    assert rv_a.status_code == 200
    assert rv_b.status_code == 200
    with zipfile.ZipFile(io.BytesIO(rv_a.data)) as zf:
        png_a = zf.read("1.png")
    with zipfile.ZipFile(io.BytesIO(rv_b.data)) as zf:
        png_b = zf.read("99999.png")

    # Both PNGs are valid.
    assert png_a.startswith(PNG_MAGIC)
    assert png_b.startswith(PNG_MAGIC)
    # The two encoded payloads differ ("Z1Z" vs "Z99999Z"), so the
    # rendered PNG bytes must differ. If substitution were dropped both
    # runs would encode the literal string "Z{n}Z" and produce identical
    # byte streams.
    assert png_a != png_b


def test_batch_invalid_template_does_not_500(client) -> None:
    """Issues 1 & 2: malformed templates like ``{m}`` no longer raise."""
    rv = client.post(
        "/api/qr/batch",
        data={
            "start": "1",
            "count": "2",
            "format": "zip",
            "data_template": "{m}",
            "label_template": "{0}",
        },
    )
    assert rv.status_code == 200
    assert rv.mimetype == "application/zip"


def test_batch_prefix_with_path_separator_returns_400(client) -> None:
    """Issue 9: ``prefix`` may not contain path separators or leading dots."""
    rv = client.post(
        "/api/qr/batch",
        data={
            "start": "1",
            "count": "2",
            "format": "zip",
            "prefix": "../",
        },
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body


# --- Hardening regression tests (semantic-review v2) ----------------


def test_single_multibyte_payload_over_capacity_returns_400(client) -> None:
    """Issue v2#1: a payload that passes the byte-length cap but still
    exceeds QR version 40 capacity must return 400, not a Flask 500.

    Latin-1 characters with code points >= 128 are encoded as two-byte
    UTF-8 sequences inside ``qrcode``. A 1200-character string of such
    characters fits comfortably under ``MAX_DATA_LENGTH`` but overflows
    the QR encoder's binary-mode capacity, exercising the wrapped
    ``ValueError`` path.
    """
    rv = client.post(
        "/api/qr/single",
        data={"data": "\u00e9" * 1200},
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body
    # The error must surface as a clean message, not the raw stack.
    assert "could not be encoded" in body["error"]


def test_batch_data_template_too_long_returns_400(client) -> None:
    """Issue v2#2: oversized ``data_template`` must be rejected at the
    HTTP layer, not surface as a 500 from inside ``qrcode.make``."""
    from qr_generator import MAX_DATA_LENGTH as _MAX

    rv = client.post(
        "/api/qr/batch",
        data={
            "start": "1",
            "count": "1",
            "format": "zip",
            "data_template": "A" * (_MAX + 1),
        },
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body
    assert "data_template" in body["error"]


def test_batch_label_template_too_long_returns_400(client) -> None:
    """Issue v2#2: oversized ``label_template`` must be rejected too."""
    from qr_generator import MAX_DATA_LENGTH as _MAX

    rv = client.post(
        "/api/qr/batch",
        data={
            "start": "1",
            "count": "1",
            "format": "zip",
            "label_template": "L" * (_MAX + 1),
        },
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body
    assert "label_template" in body["error"]


def test_batch_padding_above_max_returns_400(client) -> None:
    """Issue v2#3: ``padding`` is bounded so a 10 000-character padded
    number cannot reach the encoder."""
    from qr_generator import MAX_PADDING as _MAX

    rv = client.post(
        "/api/qr/batch",
        data={
            "start": "1",
            "count": "1",
            "format": "zip",
            "padding": str(_MAX + 1),
        },
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body
    assert "padding" in body["error"]


def test_batch_data_template_substituted_overflow_returns_400(client) -> None:
    """Issue v2#1+#2: the wrapped-encoder path catches batches where the
    substituted payload itself overflows QR capacity and surfaces a 400."""
    from qr_generator import MAX_DATA_LENGTH as _MAX

    # data_template fits under the cap but encodes to multi-byte UTF-8
    # at a length that exceeds QR version 40 binary capacity (each
    # latin-1 supplement char uses two bytes in UTF-8 so this packs
    # roughly 2 * _MAX bytes into the encoder, well past the ~2300-byte
    # ceiling).
    rv = client.post(
        "/api/qr/batch",
        data={
            "start": "1",
            "count": "1",
            "format": "zip",
            "data_template": "\u00e9" * _MAX,
            "label_template": "",
        },
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body
    assert "could not be encoded" in body["error"]


def test_batch_prefix_with_dots_and_spaces_is_accepted(client) -> None:
    """Issue v2#6: realistic prefixes (`inv.001-`, `2026 batch `,
    `tickets.`) must be accepted by the widened class."""
    for prefix in ("inv.001-", "2026 batch ", "tickets."):
        rv = client.post(
            "/api/qr/batch",
            data={
                "start": "1",
                "count": "2",
                "format": "zip",
                "prefix": prefix,
            },
        )
        assert rv.status_code == 200, f"{prefix!r} should be accepted"
        with zipfile.ZipFile(io.BytesIO(rv.data)) as zf:
            names = zf.namelist()
            assert names == [f"{prefix}1.png", f"{prefix}2.png"]


def test_batch_prefix_with_leading_dot_returns_400(client) -> None:
    """Issue v2#6: a leading ``.`` prefix is still rejected even though
    dots are now allowed in the interior of the prefix. This keeps
    hidden-file names and ``../`` traversal patterns out."""
    for prefix in (".hidden", "..", "./foo"):
        rv = client.post(
            "/api/qr/batch",
            data={
                "start": "1",
                "count": "2",
                "format": "zip",
                "prefix": prefix,
            },
        )
        assert rv.status_code == 400, f"{prefix!r} should be rejected"
        body = rv.get_json()
        assert body is not None and "error" in body


# --- Streaming batch endpoint ---------------------------------------
#
# The new ``POST /api/qr/batch/stream`` route returns ``application/x-ndjson``.
# The event ordering it guarantees, and that the tests below assert on, is:
#
#   1. exactly one ``start`` event with ``total = N``,
#   2. exactly N ``progress`` events with ``index`` running 0..N-1
#      monotonically and ``total = N`` on every one,
#   3. exactly one terminal ``result`` event whose ``data_base64`` decodes
#      to a valid ZIP (starting with ``PK``) or PDF (starting with ``%PDF-``).
#
# On a validation failure the route never enters streaming mode and
# instead returns a normal JSON 400, identical in shape to ``/api/qr/batch``.


def _parse_ndjson(body: bytes) -> list[dict]:
    """Split a streamed NDJSON body into a list of parsed events."""
    import json as _json

    events: list[dict] = []
    for line in body.split(b"\n"):
        if not line:
            continue
        events.append(_json.loads(line.decode("utf-8")))
    return events


def test_batch_stream_zip_happy_path(client) -> None:
    """Streaming ZIP: status 200, ndjson content-type, expected event sequence."""
    import base64 as _b64

    rv = client.post(
        "/api/qr/batch/stream",
        data={"start": "1", "count": "3", "format": "zip"},
    )
    assert rv.status_code == 200
    assert rv.content_type.startswith("application/x-ndjson")
    assert rv.headers.get("Cache-Control") == "no-cache"
    assert rv.headers.get("X-Accel-Buffering") == "no"

    events = _parse_ndjson(rv.get_data())
    # one start + three progress + one result = five events
    assert len(events) == 5

    assert events[0]["event"] == "start"
    assert events[0]["total"] == 3
    assert events[0]["format"] == "zip"

    progress = events[1:4]
    for i, evt in enumerate(progress):
        assert evt["event"] == "progress"
        assert evt["index"] == i
        assert evt["total"] == 3
        # filename for the un-prefixed default is "<n>.png"
        assert evt["name"] == f"{i + 1}.png"

    # Indices are monotonically increasing.
    indices = [evt["index"] for evt in progress]
    assert indices == sorted(indices)
    assert indices == [0, 1, 2]

    result = events[4]
    assert result["event"] == "result"
    assert result["mimetype"] == "application/zip"
    assert result["filename"] == "qr_batch_1_3.zip"

    payload = _b64.b64decode(result["data_base64"])
    assert payload.startswith(b"PK")
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert zf.namelist() == ["1.png", "2.png", "3.png"]
        for name in zf.namelist():
            assert zf.read(name).startswith(PNG_MAGIC)


def test_batch_stream_pdf_happy_path(client) -> None:
    """Streaming PDF: terminal result decodes to bytes starting with %PDF-."""
    import base64 as _b64

    rv = client.post(
        "/api/qr/batch/stream",
        data={"start": "1", "count": "2", "format": "pdf"},
    )
    assert rv.status_code == 200
    assert rv.content_type.startswith("application/x-ndjson")

    events = _parse_ndjson(rv.get_data())
    assert events[0]["event"] == "start"
    assert events[0]["total"] == 2
    assert events[0]["format"] == "pdf"

    progress = [e for e in events if e.get("event") == "progress"]
    assert len(progress) == 2
    assert [e["index"] for e in progress] == [0, 1]

    result = events[-1]
    assert result["event"] == "result"
    assert result["mimetype"] == "application/pdf"
    assert result["filename"] == "qr_batch_1_2.pdf"
    payload = _b64.b64decode(result["data_base64"])
    assert payload.startswith(b"%PDF-")


def test_batch_stream_validation_failure_returns_400_json(client) -> None:
    """Streaming endpoint validation failures must return a normal JSON 400,
    never partial NDJSON."""
    rv = client.post(
        "/api/qr/batch/stream",
        data={"start": "1", "count": "0"},
    )
    assert rv.status_code == 400
    # JSON content-type, NOT ndjson; we never entered streaming mode.
    assert rv.mimetype == "application/json"
    body = rv.get_json()
    assert body is not None and "error" in body
    # Body must not contain any NDJSON-style event lines.
    raw = rv.get_data()
    assert b'"event"' not in raw


def test_batch_stream_unknown_format_returns_400_json(client) -> None:
    """Streaming endpoint mirrors the synchronous endpoint's format check."""
    rv = client.post(
        "/api/qr/batch/stream",
        data={"start": "1", "count": "2", "format": "xml"},
    )
    assert rv.status_code == 400
    assert rv.mimetype == "application/json"
    body = rv.get_json()
    assert body is not None and "error" in body


def test_batch_stream_padded_prefix_filenames(client) -> None:
    """Progress event ``name`` reflects the padded, prefixed filename used
    in the terminal ZIP - i.e. progress events agree with archive entries."""
    import base64 as _b64

    rv = client.post(
        "/api/qr/batch/stream",
        data={
            "start": "1",
            "count": "2",
            "padding": "3",
            "prefix": "tkt_",
            "format": "zip",
        },
    )
    assert rv.status_code == 200
    events = _parse_ndjson(rv.get_data())
    progress_names = [e["name"] for e in events if e.get("event") == "progress"]
    assert progress_names == ["tkt_001.png", "tkt_002.png"]

    result = events[-1]
    payload = _b64.b64decode(result["data_base64"])
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert zf.namelist() == ["tkt_001.png", "tkt_002.png"]


# --- Streaming batch endpoint: review-v1 follow-ups -----------------


def test_batch_stream_chunks_arrive_incrementally(client) -> None:
    """Review v1 issue 3: the response must actually stream.

    A regression that wrapped the generator in ``list(...)`` (e.g. by
    dropping ``stream_with_context`` or by calling ``rv.get_data()``
    inside the route) would buffer the entire NDJSON body before any
    chunks were emitted. We assert here that the response is marked as
    streamed, that the underlying chunk iterator emits progress chunks
    *before* the result chunk, and that progress events for distinct
    items arrive in distinct chunks (not all bundled into one) - which
    is only possible if the helper yields each progress event as its
    item is rendered, rather than buffering all N images first.
    """
    rv = client.post(
        "/api/qr/batch/stream",
        data={"start": "1", "count": "3", "format": "zip"},
        buffered=False,
    )
    assert rv.status_code == 200
    assert rv.is_streamed

    progress_chunks = 0
    saw_result = False
    for chunk in rv.response:
        if not chunk:
            continue
        has_progress = b'"event": "progress"' in chunk
        has_result = b'"event": "result"' in chunk
        if has_progress:
            progress_chunks += 1
            # If we see progress, we must not yet have seen result, and
            # the result must not be in the same chunk (which would mean
            # the body buffered until rendering finished).
            assert not saw_result, (
                "result event arrived before progress events, "
                "which means the body was buffered"
            )
            assert not has_result, (
                "progress and result events arrived in the same chunk, "
                "which means the body was buffered"
            )
        elif has_result:
            assert progress_chunks > 0, (
                "result event arrived before any progress events, "
                "which means the body was buffered"
            )
            saw_result = True
    # All three progress events must arrive in their own chunks; if
    # they were bundled (e.g. flushed only at the end of rendering),
    # we'd see fewer chunks here.
    assert progress_chunks == 3
    assert saw_result


def test_batch_stream_mid_stream_error_event(client) -> None:
    """Review v1 issue 6: a mid-stream encoder ``ValueError`` must be
    surfaced as an ``error`` NDJSON event with HTTP 200, not a 500.

    The data template encodes 1200 latin-1 supplement characters per
    item. Each character takes two bytes in UTF-8, so the substituted
    payload comes in well over QR version 40's binary capacity (~2300
    bytes), which makes ``qrcode.make`` raise ``ValueError`` partway
    through the batch. The data_template length itself stays under
    ``MAX_DATA_LENGTH`` so the request passes form validation and the
    route does enter streaming mode.
    """
    rv = client.post(
        "/api/qr/batch/stream",
        data={
            "start": "1",
            "count": "1",
            "format": "zip",
            "data_template": "\u00e9" * 1200,
            "label_template": "",
        },
    )
    # Status remains 200 because validation passed and streaming began
    # before the ValueError was raised.
    assert rv.status_code == 200
    assert rv.content_type.startswith("application/x-ndjson")

    events = _parse_ndjson(rv.get_data())
    # We saw the ``start`` event before failing.
    assert events[0]["event"] == "start"
    # The terminal event is ``error``, never ``result``.
    terminal = events[-1]
    assert terminal["event"] == "error"
    assert "could not be encoded" in terminal["error"]
    # No ``result`` event was emitted.
    assert all(evt.get("event") != "result" for evt in events)


# --- Custom QR designs (FEAT-002) -----------------------------------
#
# These tests cover the new template_id form field, the optional logo
# upload, the GET /api/qr/templates listing, and the per-template PNG
# preview endpoint. They use real PIL pixel inspection rather than
# mocking the rendering pipeline so a regression that silently dropped
# the logo or the template wiring would surface as a colour mismatch.

REQUIRED_SPORT_CATEGORIES = {
    "marathon",
    "running",
    "duathlon",
    "triathlon",
    "cycling",
    "swimming",
}


def _orange_logo_bytes(size: tuple[int, int] = (64, 64)) -> bytes:
    """Build a small solid-orange PNG suitable for embedding in a QR."""
    from PIL import Image as _Image

    img = _Image.new("RGB", size, (255, 165, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _is_orangeish(pixel) -> bool:
    """Return True if a pixel is "in the orange ballpark".

    The pad helper paints the logo onto a white rounded square and the
    QR encoder may anti-alias the perimeter, so we widen the tolerance
    rather than asserting exact (255, 165, 0).
    """
    if isinstance(pixel, int):
        return False  # mode "L" / "1" image - never orange enough
    r, g, b = pixel[:3]
    return r >= 200 and 100 <= g <= 200 and b <= 80


def _centre_pixel(png_bytes: bytes):
    from PIL import Image as _Image

    img = _Image.open(io.BytesIO(png_bytes)).convert("RGB")
    cx = img.width // 2
    cy = img.height // 2
    return img.getpixel((cx, cy))


def test_get_templates_listing_has_required_categories(client) -> None:
    rv = client.get("/api/qr/templates")
    assert rv.status_code == 200
    assert rv.mimetype == "application/json"
    body = rv.get_json()
    assert body is not None
    assert isinstance(body.get("templates"), list)
    templates = body["templates"]
    assert len(templates) >= 30
    # Every entry has the required keys.
    for entry in templates:
        for key in ("id", "name", "category", "spec"):
            assert key in entry, f"missing {key} in {entry}"
    categories = {entry["category"] for entry in templates}
    # All six required sport categories are present.
    assert REQUIRED_SPORT_CATEGORIES.issubset(categories), categories
    # Each required sport category has at least 3 entries.
    for cat in REQUIRED_SPORT_CATEGORIES:
        in_cat = [e for e in templates if e["category"] == cat]
        assert len(in_cat) >= 3, f"{cat} has {len(in_cat)} entries, want >= 3"


def test_get_template_preview_returns_png(client) -> None:
    rv = client.get("/api/qr/templates/default/preview")
    assert rv.status_code == 200
    assert rv.mimetype == "image/png"
    assert rv.data.startswith(PNG_MAGIC)


def test_get_template_preview_unknown_returns_404(client) -> None:
    rv = client.get("/api/qr/templates/does-not-exist/preview")
    assert rv.status_code == 404
    assert rv.mimetype == "application/json"
    body = rv.get_json()
    assert body is not None and "error" in body


def test_single_with_template_id_differs_from_default(client) -> None:
    """A coloured template must produce different bytes than the default."""
    rv_default = client.post("/api/qr/single", data={"data": "hello"})
    rv_styled = client.post(
        "/api/qr/single",
        data={"data": "hello", "template_id": "running-track"},
    )
    assert rv_default.status_code == 200
    assert rv_styled.status_code == 200
    assert rv_default.data.startswith(PNG_MAGIC)
    assert rv_styled.data.startswith(PNG_MAGIC)
    assert rv_default.data != rv_styled.data


def test_single_with_template_id_default_is_byte_identical(client) -> None:
    """``template_id=default`` must hit the legacy fast path byte-for-byte."""
    rv_a = client.post("/api/qr/single", data={"data": "hello"})
    rv_b = client.post(
        "/api/qr/single",
        data={"data": "hello", "template_id": "default"},
    )
    assert rv_a.status_code == 200
    assert rv_b.status_code == 200
    assert rv_a.data == rv_b.data


def test_single_unknown_template_id_returns_400(client) -> None:
    rv = client.post(
        "/api/qr/single",
        data={"data": "hello", "template_id": "no-such-template"},
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body
    assert "unknown template_id" in body["error"]


def test_single_with_logo_returns_png(client) -> None:
    """A small orange logo must end up embedded at the centre of the QR."""
    logo_bytes = _orange_logo_bytes()
    rv = client.post(
        "/api/qr/single",
        data={
            "data": "hello",
            "logo": (io.BytesIO(logo_bytes), "logo.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200, rv.data
    assert rv.mimetype == "image/png"
    assert rv.data.startswith(PNG_MAGIC)
    pixel = _centre_pixel(rv.data)
    assert _is_orangeish(pixel), f"centre pixel {pixel!r} is not orange-ish"


def test_single_logo_too_large_returns_400(client) -> None:
    """A logo above MAX_LOGO_BYTES must be rejected before decoding."""
    from qr_generator import MAX_LOGO_BYTES as _MAX

    # Build a >MAX_LOGO_BYTES JPEG by encoding random RGB noise so it
    # cannot be compressed back below the cap. JPEG is a fine choice
    # here because PNG would compress a noise-free buffer aggressively.
    import os as _os

    noise = _os.urandom(_MAX + 1024)
    rv = client.post(
        "/api/qr/single",
        data={
            "data": "hello",
            "logo": (io.BytesIO(noise), "logo.jpg", "image/jpeg"),
        },
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body
    assert "too large" in body["error"]


def test_single_logo_wrong_mime_returns_400(client) -> None:
    """A non-image upload must be rejected with a clean 400."""
    rv = client.post(
        "/api/qr/single",
        data={
            "data": "hello",
            "logo": (io.BytesIO(b"hello, world\n"), "note.txt", "text/plain"),
        },
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body
    msg = body["error"].lower()
    assert ("png" in msg and "jpeg" in msg) or "could not be decoded" in msg


def test_single_logo_overlarge_dimensions_returns_400(client) -> None:
    """A PNG bigger than MAX_LOGO_DIMENSION on either axis must be rejected."""
    from PIL import Image as _Image
    from qr_generator import MAX_LOGO_DIMENSION as _MAX_DIM

    big = _Image.new("RGB", (_MAX_DIM + 1, _MAX_DIM + 1), (200, 200, 200))
    buf = io.BytesIO()
    big.save(buf, format="PNG")
    buf.seek(0)
    rv = client.post(
        "/api/qr/single",
        data={
            "data": "hello",
            "logo": (buf, "big.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body
    assert "dimension" in body["error"].lower()


def test_single_logo_plus_oversized_payload_returns_400(client) -> None:
    """An H-mode capacity overflow with a logo must surface as a 400."""
    rv = client.post(
        "/api/qr/single",
        data={
            "data": "A" * 2000,
            "logo": (io.BytesIO(_orange_logo_bytes()), "logo.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body is not None and "error" in body
    assert "could not be encoded" in body["error"]


def test_batch_with_logo_each_entry_contains_logo_region(client) -> None:
    """Every QR in a batch must embed the logo, not just the first."""
    logo_bytes = _orange_logo_bytes()
    rv = client.post(
        "/api/qr/batch",
        data={
            "start": "1",
            "count": "2",
            "format": "zip",
            "logo": (io.BytesIO(logo_bytes), "logo.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200, rv.data
    assert rv.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(rv.data)) as zf:
        names = zf.namelist()
        assert names == ["1.png", "2.png"]
        for name in names:
            png = zf.read(name)
            assert png.startswith(PNG_MAGIC)
            pixel = _centre_pixel(png)
            assert _is_orangeish(pixel), (
                f"entry {name} centre pixel {pixel!r} is not orange-ish"
            )


def test_batch_stream_with_template_emits_styled_payload(client) -> None:
    """The streaming path must apply the template per-item, not just at the
    edges. We compare the terminal ZIP entries against a no-template stream
    and assert the bytes differ for every entry."""
    import base64 as _b64

    def _terminal_zip_entries(form_data: dict) -> dict[str, bytes]:
        # Use a fresh client per request: the streaming endpoint relies
        # on ``stream_with_context``, which keeps the request context
        # alive until the response body is fully consumed. Holding two
        # such responses concurrently in one client trips Flask's
        # request-context bookkeeping in the test layer.
        with flask_app.test_client() as c:
            rv = c.post("/api/qr/batch/stream", data=form_data)
            assert rv.status_code == 200
            body = rv.get_data()
        events = _parse_ndjson(body)
        result = events[-1]
        assert result["event"] == "result"
        payload = _b64.b64decode(result["data_base64"])
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            return {name: zf.read(name) for name in zf.namelist()}

    plain = _terminal_zip_entries(
        {"start": "1", "count": "2", "format": "zip"}
    )
    styled = _terminal_zip_entries(
        {
            "start": "1",
            "count": "2",
            "format": "zip",
            "template_id": "running-track",
        }
    )
    assert set(plain.keys()) == set(styled.keys()) == {"1.png", "2.png"}
    for name in plain:
        assert plain[name] != styled[name], (
            f"entry {name} did not get styled in the streaming path"
        )


# --- Custom QR designs: review v1 follow-ups ------------------------


def test_single_logo_decompression_bomb_returns_400_without_oom(client) -> None:
    """Review v1 issue 1: a small-on-the-wire / huge-when-decoded PNG
    must be rejected on its declared header dimensions *before* PIL
    allocates the bitmap.

    A 12000x12000 single-channel PNG fits comfortably under
    ``MAX_LOGO_BYTES`` (a few hundred KB) but would decode to 144 MP.
    PIL's built-in :class:`PIL.Image.DecompressionBombWarning` only
    fires above ~89 MP and is a *warning*, not an exception, so the
    validator cannot rely on it. The fix is to read ``Image.size``
    from the lazy ``Image.open()`` (which does not allocate the
    bitmap) and reject oversized dimensions before calling
    ``Image.load``.

    The test asserts two things:

    1. The validator returns a clean 400 with the *dimension* error
       message (not a generic decode error). If the validator
       regressed to calling ``load()`` first the request would still
       eventually error out with the dimension message, but only
       after allocating the bitmap.
    2. The handler completes quickly. Lazy ``Image.open()`` reads the
       PNG header in microseconds; calling ``load()`` on a 144 MP
       single-channel PNG decodes the full bitmap which takes hundreds
       of milliseconds even on a fast box. We give a generous timing
       budget so this remains reliable on slow CI, but a regression
       to the load-before-check order would blow well past it.
    """
    import time
    import warnings

    from PIL import Image as _Image

    # PIL warns above ~89 MP - the warning is informational and we want
    # to build the bytes without flooding pytest with a warning line.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", _Image.DecompressionBombWarning)
        bomb = _Image.new("L", (12000, 12000), 255)
        buf = io.BytesIO()
        bomb.save(buf, format="PNG", optimize=True)
    raw = buf.getvalue()
    # Sanity check: the bytes really do fit under the byte cap.
    from qr_generator import MAX_LOGO_BYTES as _MAX

    assert len(raw) < _MAX, (
        f"bomb is {len(raw)} bytes, expected to fit under {_MAX}"
    )

    t0 = time.perf_counter()
    rv = client.post(
        "/api/qr/single",
        data={
            "data": "hello",
            "logo": (io.BytesIO(raw), "bomb.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    elapsed = time.perf_counter() - t0
    assert rv.status_code == 400, rv.data
    body = rv.get_json()
    assert body is not None and "error" in body
    # Must be the dimension-cap error, not a generic decode error: that
    # is what proves the lazy-size check ran before ``load()``.
    assert "dimension" in body["error"].lower(), body["error"]
    # Loose timing budget: a lazy header read is microseconds; a load()
    # of a 144 MP single-channel PNG takes hundreds of milliseconds
    # even on fast hardware. 1 second is generous enough for slow CI
    # while still catching a regression to load-before-check.
    assert elapsed < 1.0, (
        f"validator took {elapsed:.3f}s on a 12000x12000 PNG; "
        "this suggests Image.load() ran before the dimension check"
    )


def test_batch_stream_logo_overflow_emits_error_event(client) -> None:
    """Review v1 issue 6: when a logo bumps error correction to H and
    the payload exceeds H-mode capacity, the streaming endpoint must
    surface the encoder ``ValueError`` as a terminal ``error`` NDJSON
    event with HTTP 200, mirroring the synchronous endpoint's clean
    400. The synchronous path is already covered by
    ``test_single_logo_plus_oversized_payload_returns_400``; this test
    asserts the streaming path applies the same translation."""
    import base64 as _b64
    import json as _json

    logo_bytes = _orange_logo_bytes()
    # 'A' * 2000 fits at M (no logo) and overflows at H (with a logo),
    # so the encoder raises ValueError on the first item of the batch.
    rv = client.post(
        "/api/qr/batch/stream",
        data={
            "start": "1",
            "count": "2",
            "format": "zip",
            "data_template": "A" * 2000,
            "label_template": "",
            "logo": (io.BytesIO(logo_bytes), "logo.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    # Status remains 200 because validation passed and streaming began
    # before the ValueError was raised.
    assert rv.status_code == 200, rv.data
    assert rv.content_type.startswith("application/x-ndjson")

    events = []
    for line in rv.get_data().split(b"\n"):
        if line:
            events.append(_json.loads(line.decode("utf-8")))
    # ``start`` event was emitted before the encoder failed.
    assert events[0]["event"] == "start"
    # The terminal event must be ``error``, never ``result``.
    terminal = events[-1]
    assert terminal["event"] == "error"
    assert "could not be encoded" in terminal["error"]
    # No ``result`` event was emitted.
    assert all(evt.get("event") != "result" for evt in events)


def test_get_template_preview_square_gradient_renders(client) -> None:
    """Review v1 issue 3: the registry now ships a template that
    actually exercises the ``square_gradient`` colour-mask branch, so
    ``_resolve_color_mask`` no longer has a dead branch. The preview
    endpoint exercises the full render path end-to-end."""
    rv = client.get("/api/qr/templates/business-square-frame/preview")
    assert rv.status_code == 200
    assert rv.mimetype == "image/png"
    assert rv.data.startswith(PNG_MAGIC)


# --- Custom QR designs: review v2 follow-ups ------------------------


def test_single_logo_decompression_bomb_error_band_returns_400(client) -> None:
    """Review v2 issue 1: PIL escalates the bomb warning to a hard
    :class:`PIL.Image.DecompressionBombError` *exception* once the
    declared image area exceeds ``2 * Image.MAX_IMAGE_PIXELS``
    (~178 956 970 pixels, ~13378 px per side). The error is raised
    from inside :func:`PIL.Image.open` itself, **before** the
    validator's own ``image.size`` check runs.

    ``DecompressionBombError`` extends :class:`Exception` directly, not
    :class:`OSError`/:class:`SyntaxError`/:class:`ValueError`, so the
    narrowed except chain that closed review v1 issue 4 reopened a
    one-band-higher version of the same gap closed by review v1
    issue 1: a 14000x14000 single-colour PNG (~218 KB on the wire,
    well under the 2 MiB byte cap, ~196 MP > the 2x bomb threshold)
    used to surface as an HTTP 500 with a ``DecompressionBombError``
    traceback in the body, instead of a clean 400.

    The fix maps :class:`PIL.Image.DecompressionBombError` to the same
    ``logo dimensions too large`` 400 the explicit dimension check
    produces: any upload that trips the bomb threshold is necessarily
    larger than ``MAX_LOGO_DIMENSION`` per side, so the dimension-cap
    message is the semantically correct response.

    The existing 12000x12000 bomb test exercises the *warning* band
    (between ``MAX_IMAGE_PIXELS`` and ``2 * MAX_IMAGE_PIXELS``); this
    test exercises the *error* band above ``2 * MAX_IMAGE_PIXELS`` and
    locks down both the status code and the dimension-cap message.
    """
    import warnings

    from PIL import Image as _Image

    # 14000^2 = 196 000 000 pixels, comfortably above
    # ``2 * Image.MAX_IMAGE_PIXELS`` (178 956 970), so PIL raises
    # ``DecompressionBombError`` from inside ``Image.open``. We build
    # a single-channel PNG so the on-the-wire bytes stay small (around
    # 218 KB) and fit comfortably under ``MAX_LOGO_BYTES``.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", _Image.DecompressionBombWarning)
        bomb = _Image.new("L", (14000, 14000), 255)
        buf = io.BytesIO()
        bomb.save(buf, format="PNG", optimize=True)
    raw = buf.getvalue()

    from qr_generator import MAX_LOGO_BYTES as _MAX

    assert len(raw) < _MAX, (
        f"bomb is {len(raw)} bytes, expected to fit under {_MAX}"
    )
    # And confirm the area really does sit in the error band, so the
    # test does not silently slide into the warning band if PIL's
    # default ``MAX_IMAGE_PIXELS`` ever changes.
    assert 14000 * 14000 > 2 * _Image.MAX_IMAGE_PIXELS

    rv = client.post(
        "/api/qr/single",
        data={
            "data": "hello",
            "logo": (io.BytesIO(raw), "bomb.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    # Must be a clean 400, not a 500 with a DecompressionBombError
    # traceback in the body.
    assert rv.status_code == 400, rv.data
    body = rv.get_json()
    assert body is not None and "error" in body
    # The dimension-cap message proves the error was mapped correctly,
    # not just absorbed by some catch-all that masks it as "could not
    # be decoded". The substring also locks the user-facing wording so
    # the synchronous and streaming endpoints stay consistent.
    assert "dimension" in body["error"].lower(), body["error"]


def test_load_logo_maps_decompression_bomb_error_to_dimension_message() -> None:
    """Unit-level guard for review v2 issue 1: the validator helper
    must translate :class:`PIL.Image.DecompressionBombError` (which
    extends :class:`Exception` directly, not the narrowed
    ``(OSError, SyntaxError, ValueError)`` tuple) to the dimension-cap
    error message. This test bypasses the route layer and asserts the
    mapping directly so a future refactor of the helper that drops the
    explicit ``except Image.DecompressionBombError`` clause cannot pass
    silently even if the route-level test above is somehow skipped or
    rewritten.
    """
    import warnings

    import pytest
    from PIL import Image as _Image

    import app as app_module

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", _Image.DecompressionBombWarning)
        bomb = _Image.new("L", (14000, 14000), 255)
        buf = io.BytesIO()
        bomb.save(buf, format="PNG", optimize=True)
    raw = buf.getvalue()

    # Drive the helper through Flask's test request context so
    # ``request.files`` resolves to our crafted upload.
    with app_module.app.test_request_context(
        "/api/qr/single",
        method="POST",
        data={
            "data": "hello",
            "logo": (io.BytesIO(raw), "bomb.png", "image/png"),
        },
        content_type="multipart/form-data",
    ):
        with pytest.raises(ValueError) as excinfo:
            app_module._load_logo_from_request()

    msg = str(excinfo.value).lower()
    assert "dimension" in msg, str(excinfo.value)
    # And the cause chain preserves the original PIL exception so an
    # operator inspecting logs can still see what really happened.
    assert isinstance(
        excinfo.value.__cause__, _Image.DecompressionBombError
    ), type(excinfo.value.__cause__).__name__

