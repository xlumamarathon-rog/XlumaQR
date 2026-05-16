"""Flask HTTP layer for XlumaQR.

This module is intentionally thin: it parses and validates form input,
delegates to the pure-Python helpers in :mod:`qr_generator`, and streams
the resulting PNG / ZIP / PDF bytes back to the client. All real work
lives in ``qr_generator`` so the core remains unit-testable without
spinning up a server.
"""

from __future__ import annotations

import base64
import io
import json
import re
from typing import Any, Iterator

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

from qr_generator import (
    MAX_BORDER,
    MAX_BOX_SIZE,
    MAX_DATA_LENGTH,
    MAX_PADDING,
    compute_range,
    generate_qr,
    generate_sequence,
    images_to_pdf,
    images_to_zip,
    iter_batch_with_progress,
)

app = Flask(__name__)

# ``prefix`` is concatenated unmodified into ZIP entry names. Restrict it
# to a conservative set so a hostile caller cannot sneak path separators,
# NULs, or leading dots into the archive. The class admits letters,
# digits, ``_``, ``-``, ``.``, and space so common real-world prefixes
# (``inv.001-``, ``tickets.``, ``2026 batch ``) are accepted, but any
# leading ``.`` is rejected to keep hidden-file names and traversal
# patterns (``../``) out.
_PREFIX_RE = re.compile(r"^(?![.])[A-Za-z0-9_. -]*$")


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
    try:
        image = generate_qr(data, label=label, box_size=box_size, border=border)
    except ValueError as exc:
        # ``qrcode`` raises ValueError when the encoded payload exceeds
        # version 40 capacity (e.g. multi-byte UTF-8 input under the cap
        # but over the byte ceiling). Surface as a 400, not a 500.
        return jsonify({"error": f"data could not be encoded: {exc}"}), 400
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
    parsed, err = _parse_batch_form()
    if err is not None:
        return jsonify({"error": err}), 400
    assert parsed is not None

    items = generate_sequence(
        start=parsed["start"],
        count=parsed["count"],
        end=parsed["end"],
        data_template=parsed["data_template"],
        label_template=parsed["label_template"],
        padding=parsed["padding"],
        prefix=parsed["prefix"],
        box_size=parsed["box_size"],
        border=parsed["border"],
    )

    fmt = parsed["fmt"]
    first_n = parsed["first_n"]
    last_n = parsed["last_n"]

    try:
        if fmt == "pdf":
            payload = images_to_pdf(items)
            mimetype = "application/pdf"
            filename = f"qr_batch_{first_n}_{last_n}.pdf"
        else:
            payload = images_to_zip(items)
            mimetype = "application/zip"
            filename = f"qr_batch_{first_n}_{last_n}.zip"
    except ValueError as exc:
        # ``qrcode`` raises ValueError when a substituted template
        # overflows QR version 40 capacity. Surface as a 400.
        return jsonify({"error": f"batch could not be encoded: {exc}"}), 400

    return send_file(
        io.BytesIO(payload),
        mimetype=mimetype,
        as_attachment=True,
        download_name=filename,
    )


def _parse_batch_form() -> tuple[dict[str, Any] | None, str | None]:
    """Validate the batch form and return either parsed values or an error.

    Returns ``(parsed, None)`` on success and ``(None, error_message)``
    on failure. The error message is suitable for a 400 JSON body and
    matches the messages produced by the long-standing
    :func:`api_batch` route, so the synchronous and streaming routes
    return identical 400 contracts for the same bad input.
    """
    try:
        start = _parse_int(request.form.get("start"), "start")
        if start is None:
            return None, "start is required"
        count = _parse_int(request.form.get("count"), "count")
        end = _parse_int(request.form.get("end"), "end")
        padding = _parse_int(
            request.form.get("padding"),
            "padding",
            default=0,
            min_value=0,
            max_value=MAX_PADDING,
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
        return None, str(exc)

    assert padding is not None and box_size is not None and border is not None

    if count is None and end is None:
        return None, "exactly one of count or end is required"
    if count is not None and end is not None:
        return None, "provide only one of count or end, not both"

    prefix = request.form.get("prefix", "")
    if not _PREFIX_RE.match(prefix):
        return None, (
            "prefix may only contain letters, digits, "
            "'_', '-', '.', or space, and may not start with '.'"
        )
    data_template = request.form.get("data_template") or "{n}"
    if len(data_template) > MAX_DATA_LENGTH:
        return None, f"data_template must be <= {MAX_DATA_LENGTH} characters"

    raw_label_template = request.form.get("label_template")
    if raw_label_template is None:
        label_template: str | None = "{n}"
    elif raw_label_template == "":
        label_template = None
    else:
        label_template = raw_label_template
    if label_template is not None and len(label_template) > MAX_DATA_LENGTH:
        return None, f"label_template must be <= {MAX_DATA_LENGTH} characters"

    fmt = (request.form.get("format") or "zip").strip().lower()
    if fmt not in {"zip", "pdf"}:
        return None, "format must be 'zip' or 'pdf'"

    # Validate the range up front so we can return a clean 400 before we
    # start rendering hundreds of QR codes.
    try:
        numbers = compute_range(start, count=count, end=end, padding=padding)
    except ValueError as exc:
        return None, str(exc)

    return (
        {
            "start": start,
            "count": count,
            "end": end,
            "padding": padding,
            "box_size": box_size,
            "border": border,
            "prefix": prefix,
            "data_template": data_template,
            "label_template": label_template,
            "fmt": fmt,
            "first_n": numbers[0],
            "last_n": numbers[-1],
            "total": len(numbers),
        },
        None,
    )


@app.route("/api/qr/batch/stream", methods=["POST"])
def api_batch_stream() -> Response:
    """Render a sequential batch and stream NDJSON progress events.

    Validation behaves exactly like :func:`api_batch`: invalid input
    returns a 400 JSON body and never enters streaming mode. Once
    validation passes, the response is ``application/x-ndjson`` and the
    body consists of one JSON object per line:

    * ``{"event": "start", "total": N, "format": "zip|pdf",
       "first": "...", "last": "..."}``
    * ``{"event": "progress", "index": i, "total": N, "name": "<filename>"}``
       once per generated QR (``i`` runs ``0..N-1``).
    * Either ``{"event": "result", "filename": "...",
       "mimetype": "application/zip|application/pdf",
       "data_base64": "..."}`` carrying the packed bytes, OR
       ``{"event": "error", "error": "..."}`` if encoding fails
       mid-stream. The HTTP status remains 200 in either case once
       streaming has begun.
    """
    parsed, err = _parse_batch_form()
    if err is not None:
        return jsonify({"error": err}), 400
    assert parsed is not None

    fmt: str = parsed["fmt"]
    total: int = parsed["total"]
    first_n: str = parsed["first_n"]
    last_n: str = parsed["last_n"]

    if fmt == "pdf":
        mimetype = "application/pdf"
        filename = f"qr_batch_{first_n}_{last_n}.pdf"
    else:
        mimetype = "application/zip"
        filename = f"qr_batch_{first_n}_{last_n}.zip"

    items = generate_sequence(
        start=parsed["start"],
        count=parsed["count"],
        end=parsed["end"],
        data_template=parsed["data_template"],
        label_template=parsed["label_template"],
        padding=parsed["padding"],
        prefix=parsed["prefix"],
        box_size=parsed["box_size"],
        border=parsed["border"],
    )

    def _ndjson(obj: dict[str, Any]) -> str:
        # ``ensure_ascii`` keeps non-ASCII filenames safe on the wire as
        # \uXXXX escapes; downstream JSON parsers handle them transparently.
        return json.dumps(obj, ensure_ascii=True) + "\n"

    def generate() -> Iterator[str]:
        yield _ndjson(
            {
                "event": "start",
                "total": total,
                "format": fmt,
                "first": first_n,
                "last": last_n,
            }
        )

        # Render items one at a time so we can emit a ``progress`` event
        # after each one. The rendered pairs are buffered in a list and
        # packed into the final container at the end. This means progress
        # events arrive live during rendering (the slow part), and the
        # client sees the percentage climb to 100% before the final
        # ``result`` event lands. Packing into a ZIP/PDF after all images
        # are rendered is cheap relative to QR generation.
        rendered: list = []
        try:
            for index, (name, image) in enumerate(items):
                rendered.append((name, image))
                yield _ndjson(
                    {
                        "event": "progress",
                        "index": index,
                        "total": total,
                        "name": name,
                    }
                )

            payload = iter_batch_with_progress(iter(rendered), fmt)
        except ValueError as exc:
            yield _ndjson(
                {"event": "error", "error": f"batch could not be encoded: {exc}"}
            )
            return
        except Exception as exc:  # pragma: no cover - defensive catch-all
            yield _ndjson({"event": "error", "error": str(exc)})
            return

        yield _ndjson(
            {
                "event": "result",
                "filename": filename,
                "mimetype": mimetype,
                "data_base64": base64.b64encode(payload).decode("ascii"),
            }
        )

    response = Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
    )
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
