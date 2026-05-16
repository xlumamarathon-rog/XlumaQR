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
