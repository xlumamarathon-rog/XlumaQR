"""Flask HTTP layer for XlumaQR.

This module is intentionally thin: it parses and validates form input,
delegates to the pure-Python helpers in :mod:`qr_generator`, and streams
the resulting PNG / ZIP / PDF bytes back to the client. All real work
lives in ``qr_generator`` so the core remains unit-testable without
spinning up a server.
"""

from __future__ import annotations

import io
import re
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_file

from qr_generator import (
    MAX_BORDER,
    MAX_BOX_SIZE,
    MAX_DATA_LENGTH,
    compute_range,
    generate_qr,
    generate_sequence,
    images_to_pdf,
    images_to_zip,
)

app = Flask(__name__)

# ``prefix`` is concatenated unmodified into ZIP entry names. Restrict it
# to a conservative set so a hostile caller cannot sneak path separators,
# NULs, or leading dots into the archive.
_PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]*$")


def _parse_int(
    value: Any,
    field: str,
    *,
    default: int | None = None,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int | None:
    """Coerce a form value to int, returning ``default`` when blank/missing.

    Raises ``ValueError`` with a human-friendly message when the value is
    present but not a valid integer, or falls outside ``[min_value,
    max_value]`` when those bounds are supplied.
    """
    if value is None:
        return default
    if isinstance(value, str):
        if value.strip() == "":
            return default
        try:
            parsed = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{field} must be an integer") from exc
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an integer") from exc

    if min_value is not None and parsed < min_value:
        raise ValueError(f"{field} must be >= {min_value}")
    if max_value is not None and parsed > max_value:
        raise ValueError(f"{field} must be <= {max_value}")
    return parsed


@app.route("/", methods=["GET"])
def index() -> str:
    """Render the single-page UI with Single QR + Sequential Batch tabs."""
    return render_template("index.html")


@app.route("/api/qr/single", methods=["POST"])
def api_single() -> Response:
    """Render a single QR code and return it as a PNG response."""
    data = request.form.get("data", "").strip()
    if not data:
        return jsonify({"error": "data is required"}), 400
    if len(data) > MAX_DATA_LENGTH:
        return jsonify({"error": f"data must be <= {MAX_DATA_LENGTH} characters"}), 400

    label = request.form.get("label")
    if label is not None and label == "":
        label = None

    try:
        box_size = _parse_int(
            request.form.get("box_size"),
            "box_size",
            default=10,
            min_value=1,
            max_value=MAX_BOX_SIZE,
        )
        border = _parse_int(
            request.form.get("border"),
            "border",
            default=4,
            min_value=0,
            max_value=MAX_BORDER,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    assert box_size is not None and border is not None  # defaults guarantee non-None
    image = generate_qr(data, label=label, box_size=box_size, border=border)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)
    return send_file(
        buf,
        mimetype="image/png",
        as_attachment=False,
        download_name="qr.png",
    )


@app.route("/api/qr/batch", methods=["POST"])
def api_batch() -> Response:
    """Render a sequential batch and return a ZIP or PDF response."""
    try:
        start = _parse_int(request.form.get("start"), "start")
        if start is None:
            raise ValueError("start is required")
        count = _parse_int(request.form.get("count"), "count")
        end = _parse_int(request.form.get("end"), "end")
        padding = _parse_int(
            request.form.get("padding"),
            "padding",
            default=0,
            min_value=0,
        )
        box_size = _parse_int(
            request.form.get("box_size"),
            "box_size",
            default=10,
            min_value=1,
            max_value=MAX_BOX_SIZE,
        )
        border = _parse_int(
            request.form.get("border"),
            "border",
            default=4,
            min_value=0,
            max_value=MAX_BORDER,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    assert padding is not None and box_size is not None and border is not None

    if count is None and end is None:
        return jsonify({"error": "exactly one of count or end is required"}), 400
    if count is not None and end is not None:
        return jsonify({"error": "provide only one of count or end, not both"}), 400

    prefix = request.form.get("prefix", "")
    if not _PREFIX_RE.match(prefix):
        return (
            jsonify(
                {"error": "prefix may only contain letters, digits, '_' and '-'"}
            ),
            400,
        )
    data_template = request.form.get("data_template") or "{n}"

    raw_label_template = request.form.get("label_template")
    if raw_label_template is None:
        label_template: str | None = "{n}"
    elif raw_label_template == "":
        label_template = None
    else:
        label_template = raw_label_template

    fmt = (request.form.get("format") or "zip").strip().lower()
    if fmt not in {"zip", "pdf"}:
        return jsonify({"error": "format must be 'zip' or 'pdf'"}), 400

    # Validate the range up front so we can return a clean 400 before we
    # start rendering hundreds of QR codes.
    try:
        numbers = compute_range(start, count=count, end=end, padding=padding)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    first_n = numbers[0]
    last_n = numbers[-1]

    items = generate_sequence(
        start=start,
        count=count,
        end=end,
        data_template=data_template,
        label_template=label_template,
        padding=padding,
        prefix=prefix,
        box_size=box_size,
        border=border,
    )

    if fmt == "pdf":
        payload = images_to_pdf(items)
        mimetype = "application/pdf"
        filename = f"qr_batch_{first_n}_{last_n}.pdf"
    else:
        payload = images_to_zip(items)
        mimetype = "application/zip"
        filename = f"qr_batch_{first_n}_{last_n}.zip"

    return send_file(
        io.BytesIO(payload),
        mimetype=mimetype,
        as_attachment=True,
        download_name=filename,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
