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
from PIL import Image, UnidentifiedImageError

from qr_generator import (
    MAX_BORDER,
    MAX_BOX_SIZE,
    MAX_DATA_LENGTH,
    MAX_LOGO_BYTES,
    MAX_LOGO_DIMENSION,
    MAX_PADDING,
    compute_range,
    generate_qr,
    generate_sequence,
    get_template,
    images_to_pdf,
    images_to_zip,
    iter_batch_with_progress,
    list_templates,
    render_template_preview,
)

app = Flask(__name__)

# Per-process cache of rendered template preview PNGs. Keyed by template
# id, populated lazily on first ``GET /api/qr/templates/<id>/preview``.
# Under Vercel each warm Lambda instance reuses this dict across
# requests, so subsequent gallery loads in the same instance render the
# bytes once and ship the cached payload thereafter. A cold start
# starts an empty dict and re-renders on first request, which is fine.
_PREVIEW_CACHE: dict[str, bytes] = {}

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


def _resolve_template_id_from_request() -> str | None:
    """Return the template id to use for this request, or ``None``.

    Reads the optional ``template_id`` form field. Whitespace-only values
    and the literal ``"default"`` are normalised to ``None`` so the
    legacy plain-rendering fast path in :func:`generate_qr` is hit
    byte-for-byte. Any other value is validated against the template
    registry; an unknown id raises :class:`ValueError` with a message
    starting ``"unknown template_id"`` so the route's existing
    ``ValueError -> 400`` handlers can surface it cleanly.
    """
    raw = request.form.get("template_id")
    if raw is None:
        return None
    value = raw.strip()
    if value == "" or value == "default":
        return None
    try:
        get_template(value)
    except ValueError as exc:
        raise ValueError(f"unknown template_id: {value}") from exc
    return value


def _load_logo_from_request() -> Image.Image | None:
    """Load and validate an optional ``logo`` upload from the request.

    Returns ``None`` if no ``logo`` file field is supplied or the field
    is empty (no filename / zero bytes). Otherwise returns the decoded
    :class:`PIL.Image.Image` ready to pass straight into
    :func:`generate_qr`.

    Validation steps, in order, all surfaced as :class:`ValueError` so
    the route's existing ``ValueError -> 400`` handler returns a clean
    JSON 400 (never a 500):

    1. The upload is read with a hard byte cap of
       :data:`qr_generator.MAX_LOGO_BYTES`. Anything larger raises
       ``"logo too large"``.
    2. The bytes are sniffed via :func:`PIL.Image.open` followed by
       :meth:`PIL.Image.Image.verify`, which validates the magic bytes
       match a real image. ``verify()`` consumes the image object, so
       the bytes are reopened afterwards for further inspection.
    3. Only PNG and JPEG images are accepted. Anything else (SVG,
       BMP, GIF, WEBP, etc.) raises ``"logo must be PNG or JPEG"``.
    4. Either dimension exceeding :data:`qr_generator.MAX_LOGO_DIMENSION`
       raises ``"logo dimensions too large"``.

    PIL's :class:`PIL.UnidentifiedImageError` (raised when a non-image
    file is uploaded with a fake mime type, e.g. a text file) is caught
    and turned into ``ValueError("logo could not be decoded")``.
    """
    upload = request.files.get("logo")
    if upload is None or not getattr(upload, "filename", ""):
        return None

    raw = upload.read()
    if not raw:
        return None
    if len(raw) > MAX_LOGO_BYTES:
        raise ValueError(f"logo too large (max {MAX_LOGO_BYTES} bytes)")

    # First pass: ``verify()`` confirms the magic bytes match a real
    # image without decoding the full bitmap. It also consumes the
    # image object, so we re-open the bytes for further inspection.
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
    except UnidentifiedImageError as exc:
        raise ValueError("logo could not be decoded") from exc
    except Exception as exc:
        # PIL raises a grab-bag of exceptions on malformed input
        # (SyntaxError, OSError, ValueError, ...). Normalise them all
        # to a clean ValueError so the HTTP layer returns 400.
        raise ValueError("logo could not be decoded") from exc

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except UnidentifiedImageError as exc:
        raise ValueError("logo could not be decoded") from exc
    except Exception as exc:
        raise ValueError("logo could not be decoded") from exc

    fmt = (image.format or "").upper()
    if fmt not in {"PNG", "JPEG"}:
        raise ValueError("logo must be PNG or JPEG")

    width, height = image.size
    if width > MAX_LOGO_DIMENSION or height > MAX_LOGO_DIMENSION:
        raise ValueError(
            f"logo dimensions too large (max {MAX_LOGO_DIMENSION}x{MAX_LOGO_DIMENSION})"
        )

    return image


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
        template_id = _resolve_template_id_from_request()
        logo = _load_logo_from_request()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    assert box_size is not None and border is not None  # defaults guarantee non-None
    try:
        image = generate_qr(
            data,
            label=label,
            box_size=box_size,
            border=border,
            template_id=template_id,
            logo=logo,
        )
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
        template_id=parsed["template_id"],
        logo=parsed["logo"],
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
        template_id = _resolve_template_id_from_request()
        logo = _load_logo_from_request()
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
            "template_id": template_id,
            "logo": logo,
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
        template_id=parsed["template_id"],
        logo=parsed["logo"],
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

        # Drive the streaming packer one item at a time. ``items`` is
        # the lazy iterator returned by :func:`generate_sequence`, and
        # ``iter_batch_with_progress`` is itself a generator that pulls
        # one ``(filename, image)`` pair per turn, writes it into the
        # output container, then yields a ``("progress", ...)`` tuple
        # back. This keeps peak memory at roughly one PIL Image rather
        # than buffering all N images while we wait to start packing.
        try:
            payload: bytes | None = None
            for token in iter_batch_with_progress(items, fmt):
                kind = token[0]
                if kind == "progress":
                    _, index, name = token
                    yield _ndjson(
                        {
                            "event": "progress",
                            "index": index,
                            "total": total,
                            "name": name,
                        }
                    )
                elif kind == "result":
                    payload = token[1]
        except ValueError as exc:
            yield _ndjson(
                {"event": "error", "error": f"batch could not be encoded: {exc}"}
            )
            return
        except Exception as exc:  # pragma: no cover - defensive catch-all
            yield _ndjson({"event": "error", "error": str(exc)})
            return

        # ``payload`` is set by the terminal ``("result", bytes)`` token
        # the helper yields exactly once. If it is missing here, the
        # helper contract was violated; surface that as an error event
        # rather than a 500.
        if payload is None:  # pragma: no cover - contract violation
            yield _ndjson(
                {"event": "error", "error": "internal error: empty batch payload"}
            )
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


@app.route("/api/qr/templates", methods=["GET"])
def api_templates_list() -> Response:
    """Return the JSON listing of built-in design templates.

    The response shape is ``{"templates": [<entry>, ...]}`` where every
    entry has ``id``, ``name``, ``category``, and ``spec`` keys (the
    ``spec`` is included verbatim so the UI can render a small swatch
    if it wants to, but it is not required to use it).

    A short ``Cache-Control`` is set so warm browsers skip the round
    trip on subsequent page loads inside the cache window.
    """
    response = jsonify({"templates": list_templates()})
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


@app.route("/api/qr/templates/<template_id>/preview", methods=["GET"])
def api_template_preview(template_id: str) -> Response:
    """Return a small thumbnail PNG for ``template_id``.

    Resolves the id via :func:`qr_generator.get_template`; an unknown id
    returns a JSON 404. On success, the PNG bytes are produced by
    :func:`qr_generator.render_template_preview` and cached in the
    per-process :data:`_PREVIEW_CACHE` so a warm Lambda renders each
    template at most once.
    """
    try:
        get_template(template_id)
    except ValueError:
        return jsonify({"error": "unknown template id"}), 404

    payload = _PREVIEW_CACHE.get(template_id)
    if payload is None:
        payload = render_template_preview(template_id)
        _PREVIEW_CACHE[template_id] = payload

    response = send_file(
        io.BytesIO(payload),
        mimetype="image/png",
        as_attachment=False,
        download_name=f"{template_id}.png",
    )
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
