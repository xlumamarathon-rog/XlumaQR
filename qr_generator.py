"""Pure-Python QR code generation core for XlumaQR.

This module is intentionally free of any Flask imports so it can be
unit-tested on its own and reused from any caller (CLI, web layer,
notebooks). The Flask HTTP layer in ``app.py`` is a thin wrapper that
delegates to the helpers exposed here.

User-facing canonical example
-----------------------------
Generate 100 sequential QR codes numbered 101..200 (inclusive on both
ends, since count=100 starting at 101 covers 101, 102, ..., 200)::

    from qr_generator import compute_range, generate_sequence, images_to_zip

    numbers = compute_range(101, count=100)
    # ['101', '102', ..., '200']

    seq = generate_sequence(start=101, count=100)
    # iterator of (filename, PIL.Image.Image) pairs:
    # ('101.png', <Image>), ('102.png', <Image>), ..., ('200.png', <Image>)

    zip_bytes = images_to_zip(seq)
    # bytes of an in-memory ZIP archive containing 100 PNG files.

Public API
----------
* :func:`generate_qr` - render a single QR (optionally with a printed label).
* :func:`compute_range` - build a list of zero-padded numeric strings.
* :func:`generate_sequence` - iterator of ``(filename, PIL.Image.Image)``.
* :func:`images_to_zip` - pack an iterable of images into a ZIP archive.
* :func:`images_to_pdf` - lay out images on a PDF grid (one PDF per call).
"""

from __future__ import annotations

import io
import zipfile
from typing import Iterable, Iterator

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_M
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas

__all__ = [
    "generate_qr",
    "compute_range",
    "generate_sequence",
    "images_to_zip",
    "images_to_pdf",
]


# Sensible upper bounds for inputs that flow in from untrusted callers
# (e.g. an HTTP form). These are advisory: the core enforces ``MAX_RANGE_SIZE``
# in :func:`compute_range` so an unwary caller cannot accidentally materialise
# millions of strings, but the rest are exposed as constants so the HTTP
# layer can validate before invoking us.
#
# ``MAX_DATA_LENGTH`` is the most conservative of the four and deserves a
# note. The underlying ``qrcode`` library tops out at QR version 40, whose
# binary capacity at error-correction level M is roughly 2300 bytes. A
# payload above that limit raises ``ValueError("Invalid version (was 41,
# expected 1 to 40)")`` deep inside ``qrcode.make()``. We pick ``2300`` as
# the cap so any input that passes the HTTP validator will encode cleanly
# regardless of character set; callers that exceed it should see a 400
# from the HTTP layer rather than a 500 from the encoder.
MAX_RANGE_SIZE = 5000
MAX_DATA_LENGTH = 2300
MAX_BOX_SIZE = 50
MAX_BORDER = 16
MAX_PADDING = 12


def generate_qr(
    data: str,
    label: str | None = None,
    box_size: int = 10,
    border: int = 4,
    label_height: int | None = None,
) -> Image.Image:
    """Render ``data`` as a QR code and optionally overlay a label on it.

    The QR is built with :data:`qrcode.constants.ERROR_CORRECT_M`. When
    ``label`` is provided, a small white badge is drawn at the bottom
    center of the QR image and the label text is rendered on top of it.
    The QR remains scannable because error correction level M tolerates
    up to 15% damage.

    Parameters
    ----------
    data:
        Payload encoded into the QR code.
    label:
        Optional text overlaid on the QR. ``None`` returns the bare QR.
    box_size:
        Pixel size of each QR module (passed through to ``qrcode``).
    border:
        Quiet-zone width in modules (passed through to ``qrcode``).
    label_height:
        Height in pixels of the overlay badge. When ``None`` (the default),
        a value proportional to the QR size is chosen.

    Returns
    -------
    PIL.Image.Image
        RGB image of the rendered QR (with label overlay, if provided).
    """
    qr = qrcode.QRCode(
        error_correction=ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    if label is None:
        return qr_img

    qr_w, qr_h = qr_img.size
    if label_height is None:
        # Roughly 18% of the QR's height, with a sensible floor so the
        # default bitmap font remains readable on small QRs.
        label_height = max(24, qr_h // 6)

    font = ImageFont.load_default()

    # Measure the label text.
    draw = ImageDraw.Draw(qr_img)
    try:
        bbox = draw.textbbox((0, 0), label, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        text_offset_x = -bbox[0]
        text_offset_y = -bbox[1]
    except AttributeError:
        # Pillow < 9.2 fallback (kept for safety; modern Pillow has textbbox).
        text_w, text_h = draw.textsize(label, font=font)  # type: ignore[attr-defined]
        text_offset_x = 0
        text_offset_y = 0

    # Draw a white badge at the bottom center of the QR, overlaid on the
    # QR pattern. Add horizontal and vertical padding around the text.
    pad_x = max(4, box_size)
    pad_y = max(2, box_size // 2)
    badge_w = text_w + 2 * pad_x
    badge_h = text_h + 2 * pad_y

    badge_x = (qr_w - badge_w) // 2
    badge_y = qr_h - badge_h - (border * box_size) // 2

    # Draw badge background (white rectangle)
    draw.rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
        fill="white",
        outline="black",
        width=1,
    )

    # Draw label text centered in the badge
    text_x = badge_x + pad_x + text_offset_x
    text_y = badge_y + pad_y + text_offset_y
    draw.text((text_x, text_y), label, fill="black", font=font)

    return qr_img


def compute_range(
    start: int,
    count: int | None = None,
    end: int | None = None,
    padding: int = 0,
) -> list[str]:
    """Return zero-padded numeric strings for a sequential QR range.

    Exactly one of ``count`` or ``end`` must be provided.

    * ``count`` is interpreted as the number of values starting at ``start``
      (inclusive of ``start``, exclusive of ``start + count``), so
      ``compute_range(101, count=100)`` yields ``['101', ..., '200']``.
    * ``end`` is interpreted inclusively, so ``compute_range(1, end=3)``
      yields ``['1', '2', '3']``.

    Parameters
    ----------
    start:
        First integer in the sequence.
    count:
        Number of items to produce (must be > 0).
    end:
        Final integer in the sequence (inclusive, must be >= start).
    padding:
        Minimum width of each emitted string; values are zero-padded on
        the left to that width. ``0`` disables padding.

    Raises
    ------
    ValueError
        If neither or both of ``count``/``end`` are provided, or if the
        provided value is invalid (``count <= 0``, ``end < start``).
    """
    if count is None and end is None:
        raise ValueError("compute_range requires exactly one of count or end")
    if count is not None and end is not None:
        raise ValueError("compute_range accepts only one of count or end, not both")

    if count is not None:
        if count <= 0:
            raise ValueError("count must be > 0")
        if count > MAX_RANGE_SIZE:
            raise ValueError(f"count must be <= {MAX_RANGE_SIZE}")
        last_exclusive = start + count
    else:
        assert end is not None  # narrow for type checkers
        if end < start:
            raise ValueError("end must be >= start")
        if (end - start + 1) > MAX_RANGE_SIZE:
            raise ValueError(f"range size must be <= {MAX_RANGE_SIZE}")
        last_exclusive = end + 1

    if padding > 0:
        return [str(n).zfill(padding) for n in range(start, last_exclusive)]
    return [str(n) for n in range(start, last_exclusive)]


def generate_sequence(
    start: int,
    count: int | None = None,
    end: int | None = None,
    data_template: str = "{n}",
    label_template: str | None = "{n}",
    padding: int = 0,
    prefix: str = "",
    box_size: int = 10,
    border: int = 4,
) -> Iterator[tuple[str, Image.Image]]:
    """Yield ``(filename, PIL.Image.Image)`` pairs for a sequential range.

    Both ``data_template`` and ``label_template`` are treated as literal
    strings with the substring ``{n}`` replaced by the *padded* numeric
    string for that item. We deliberately use :py:meth:`str.replace`
    rather than :py:meth:`str.format` so user-supplied templates cannot
    raise on stray braces and cannot perform attribute access (e.g.
    ``{n.__class__}``) into a Python object. Templates that do not
    contain ``{n}`` are emitted verbatim.

    Pass ``label_template=None`` to disable the printed label.

    The emitted filename is ``f"{prefix}{padded_n}.png"``.
    """
    numbers = compute_range(start, count=count, end=end, padding=padding)
    for n in numbers:
        data = data_template.replace("{n}", n)
        label = label_template.replace("{n}", n) if label_template is not None else None
        image = generate_qr(
            data,
            label=label,
            box_size=box_size,
            border=border,
        )
        yield f"{prefix}{n}.png", image


def images_to_zip(items: Iterable[tuple[str, Image.Image]]) -> bytes:
    """Pack ``(filename, image)`` pairs into a ZIP archive in memory.

    Each image is encoded as PNG and stored under its given filename.
    Returns the raw bytes of the ZIP archive.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for filename, image in items:
            png_buf = io.BytesIO()
            image.save(png_buf, format="PNG")
            zf.writestr(filename, png_buf.getvalue())
    return buffer.getvalue()


def images_to_pdf(
    items: Iterable[tuple[str, Image.Image]],
    page_size: tuple[float, float] = LETTER,
    cols: int = 3,
    rows: int = 4,
    margin_pt: float = 36.0,
) -> bytes:
    """Lay out images in a grid on a PDF and return the raw PDF bytes.

    The PDF uses ``page_size`` (default US Letter) and packs images in a
    ``cols`` x ``rows`` grid with ``margin_pt`` points of margin on each
    side. Pages are added automatically when the grid fills.
    """
    if cols <= 0 or rows <= 0:
        raise ValueError("cols and rows must both be > 0")

    buffer = io.BytesIO()
    page_w, page_h = page_size
    usable_w = page_w - 2 * margin_pt
    usable_h = page_h - 2 * margin_pt
    cell_w = usable_w / cols
    cell_h = usable_h / rows

    c = pdf_canvas.Canvas(buffer, pagesize=page_size)
    per_page = cols * rows
    index = 0
    for _, image in items:
        slot = index % per_page
        if index > 0 and slot == 0:
            c.showPage()

        col = slot % cols
        row = slot // cols

        # Fit the image into the cell while preserving aspect ratio.
        img_w, img_h = image.size
        scale = min(cell_w / img_w, cell_h / img_h)
        draw_w = img_w * scale
        draw_h = img_h * scale

        cell_left = margin_pt + col * cell_w
        # Reportlab origin is bottom-left; we want row 0 at the top.
        cell_bottom = page_h - margin_pt - (row + 1) * cell_h
        x = cell_left + (cell_w - draw_w) / 2
        y = cell_bottom + (cell_h - draw_h) / 2

        c.drawImage(
            ImageReader(image),
            x,
            y,
            width=draw_w,
            height=draw_h,
            preserveAspectRatio=True,
            mask="auto",
        )
        index += 1

    c.showPage()
    c.save()
    return buffer.getvalue()
