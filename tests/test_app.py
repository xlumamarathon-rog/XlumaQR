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
