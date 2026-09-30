"""Code 128 barcodes with the same batch outputs as the QR generator.

QR templates and centre logos do not apply to a 1D barcode. The range,
padding, prefix, data template, label, and download formats do.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Iterator

from PIL import Image, ImageDraw, ImageFont
from reportlab.graphics import renderPS, renderSVG
from reportlab.graphics.barcode.code128 import Code128
from reportlab.graphics.barcode.widgets import BarcodeCode128
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas as pdf_canvas

from qr_generator import compute_range

_FONT_PATH = Path(__file__).resolve().parent / "static" / "fonts" / "PlusJakartaSans-Bold.ttf"


def _reject(data: str) -> str:
    if not data or not str(data).strip():
        raise ValueError("barcode data is required")
    if any(ord(c) > 127 for c in data):
        raise ValueError("barcode data must be plain ASCII (Code 128)")
    return data


def _runs(data: str, extra_quiet: int = 0) -> list[tuple[bool, int]]:
    """Black/white module runs, including the Code 128 quiet zone."""
    data = _reject(data)
    bar = Code128(data, barWidth=1.0, barHeight=10, humanReadable=False, quiet=False)
    bar._calculate()
    if not bar.valid:
        raise ValueError("data could not be encoded as a Code 128 barcode")

    quiet = 10 + max(0, int(extra_quiet))
    runs: list[tuple[bool, int]] = [(False, quiet)]
    lower, upper = ord("a") - 1, ord("A") - 1
    for char in bar.decomposed:
        code = ord(char)
        if "a" <= char <= "z":
            runs.append((False, code - lower))
        elif "A" <= char <= "Z":
            runs.append((True, code - upper))
    runs.append((False, quiet))
    return runs


def _font(size: int) -> ImageFont.ImageFont:
    if _FONT_PATH.is_file():
        return ImageFont.truetype(str(_FONT_PATH), size=size)
    return ImageFont.load_default()


def render_barcode_image(
    data: str,
    label: str | None = None,
    module_px: int = 4,
    extra_quiet: int = 0,
    bar_modules: int = 22,
    background: tuple[int, int, int, int] | None = None,
) -> Image.Image:
    """Barcode image. PNG stays transparent; JPG uses a white ground."""
    module_px = max(2, int(module_px))
    runs = _runs(data, extra_quiet=extra_quiet)
    modules = sum(width for _, width in runs)
    bar_h = max(56, module_px * bar_modules)
    pad_y = module_px * 3
    font_size = max(14, module_px * 3)
    label_h = 0
    font = None
    if label:
        font = _font(font_size)
        label_h = font_size + module_px * 3

    width = modules * module_px
    height = pad_y + bar_h + pad_y + label_h
    image = Image.new("RGBA", (width, height), background or (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    x = 0
    top = pad_y
    for on, run in runs:
        px = run * module_px
        if on and px > 0:
            draw.rectangle((x, top, x + px - 1, top + bar_h - 1), fill=(0, 0, 0, 255))
        x += px

    if label and font is not None:
        bbox = draw.textbbox((0, 0), label, font=font)
        text_w = bbox[2] - bbox[0]
        text_x = max(0, (width - text_w) // 2)
        text_y = pad_y + bar_h + pad_y // 2
        draw.text((text_x, text_y), label, font=font, fill=(0, 0, 0, 255))
    return image


def generate_barcode_png(
    data: str,
    label: str | None = None,
    module_px: int = 4,
    extra_quiet: int = 0,
) -> bytes:
    buf = io.BytesIO()
    render_barcode_image(
        data, label=label, module_px=module_px, extra_quiet=extra_quiet
    ).save(buf, format="PNG")
    return buf.getvalue()


def generate_barcode_jpg(
    data: str,
    label: str | None = None,
    extra_quiet: int = 0,
) -> bytes:
    """Print-resolution JPEG. 16px modules, tall bars, no chroma subsampling."""
    image = render_barcode_image(
        data,
        label=label,
        module_px=16,
        extra_quiet=extra_quiet,
        bar_modules=40,
        background=(255, 255, 255, 255),
    )
    buf = io.BytesIO()
    image.convert("RGB").save(
        buf,
        format="JPEG",
        quality=95,
        subsampling=0,
        optimize=True,
    )
    return buf.getvalue()


def _drawing(data: str, label: str | None) -> Drawing:
    _reject(data)
    widget = BarcodeCode128(
        value=data,
        barWidth=0.9,
        barHeight=42,
        humanReadable=0,
        quiet=1,
    )
    pad = 8
    label_h = 16 if label else 0
    width = widget.width + pad * 2
    height = widget.height + label_h + pad * 2
    drawing = Drawing(width, height)
    widget.x = pad
    widget.y = pad + label_h
    drawing.add(widget)
    if label:
        drawing.add(
            String(
                width / 2,
                4,
                label,
                textAnchor="middle",
                fontName="Helvetica-Bold",
                fontSize=11,
            )
        )
    return drawing


def generate_barcode_svg(data: str, label: str | None = None) -> str:
    return renderSVG.drawToString(_drawing(data, label))


def generate_barcode_eps(data: str, label: str | None = None) -> bytes:
    raw = renderPS.drawToString(_drawing(data, label))
    return raw if isinstance(raw, bytes) else raw.encode("latin-1")


def _safe_stem(value: str) -> str:
    cleaned = "".join("_" if c in '<>:"/\\|?*' else c for c in value.strip())
    cleaned = cleaned.strip(". ")
    return cleaned or "barcode"


def _rows(parsed: dict, ext: str) -> list[tuple[str, str, str | None]]:
    numbers = compute_range(
        parsed["start"],
        count=parsed["count"],
        end=parsed["end"],
        padding=parsed["padding"],
    )
    data_template = parsed["data_template"]
    label_template = parsed["label_template"]
    prefix = parsed["prefix"]
    name_by = parsed.get("name_by") or "barcode"
    rows: list[tuple[str, str, str | None]] = []
    used: set[str] = set()
    for index, n in enumerate(numbers, start=1):
        payload = data_template.replace("{n}", n)
        label = label_template.replace("{n}", n) if label_template is not None else None
        stem = str(index) if name_by == "sequence" else payload
        filename = f"{prefix}{_safe_stem(stem)}.{ext}"
        if filename in used:
            filename = f"{prefix}{_safe_stem(stem)}_{index}.{ext}"
        used.add(filename)
        rows.append((filename, payload, label))
    return rows


def mapping_xlsx(rows: list[tuple[str, str, str | None]]) -> bytes:
    """Two columns: sequence 1, 2, 3… and the barcode ID encoded in each image."""
    import openpyxl
    from openpyxl.styles import Font

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Barcodes"
    sheet.append(["Sequence", "Barcode ID"])
    sheet["A1"].font = Font(bold=True)
    sheet["B1"].font = Font(bold=True)
    for index, (_filename, barcode_id, _label) in enumerate(rows, start=1):
        sheet.append([index, barcode_id])
    sheet.column_dimensions["A"].width = 14
    sheet.column_dimensions["B"].width = 28
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def _flowable(data: str, bar_width: float, bar_height: float) -> Code128:
    _reject(data)
    bar = Code128(
        data,
        barWidth=bar_width,
        barHeight=bar_height,
        humanReadable=False,
        quiet=True,
    )
    bar._calculate()
    if not bar.valid:
        raise ValueError("data could not be encoded as a Code 128 barcode")
    return bar


def _pdf_single(rows: list[tuple[str, str, str | None]]) -> Iterator[tuple]:
    buf = io.BytesIO()
    canvas = None
    for index, (name, data, label) in enumerate(rows):
        bar = _flowable(data, bar_width=1.15, bar_height=48)
        pad = 14
        label_h = 18 if label else 0
        page_w = bar.width + pad * 2
        page_h = bar.height + label_h + pad * 2
        if canvas is None:
            canvas = pdf_canvas.Canvas(buf, pagesize=(page_w, page_h))
        else:
            canvas.showPage()
            canvas.setPageSize((page_w, page_h))
        bar.drawOn(canvas, pad, pad + label_h)
        if label:
            canvas.setFillColorRGB(0, 0, 0)
            canvas.setFont("Helvetica-Bold", 12)
            canvas.drawCentredString(page_w / 2, pad * 0.45, label)
        yield ("progress", index, name)
    if canvas is None:
        raise ValueError("count must be > 0")
    canvas.save()
    yield ("result", buf.getvalue())


def _pdf_grid(rows: list[tuple[str, str, str | None]]) -> Iterator[tuple]:
    page_w, page_h = LETTER
    margin = 36.0
    cols, row_count = 2, 4
    gap = 10.0
    per_page = cols * row_count
    cell_w = (page_w - 2 * margin - gap) / cols
    cell_h = (page_h - 2 * margin - gap * (row_count - 1)) / row_count

    buf = io.BytesIO()
    canvas = pdf_canvas.Canvas(buf, pagesize=LETTER)
    for index, (name, data, label) in enumerate(rows):
        slot = index % per_page
        if index > 0 and slot == 0:
            canvas.showPage()
        col = slot % cols
        row_from_top = slot // cols
        x = margin + col * (cell_w + gap)
        y = page_h - margin - (row_from_top + 1) * cell_h - row_from_top * gap

        label_h = 14 if label else 0
        bar = _flowable(data, bar_width=1.0, bar_height=32)
        avail_w = cell_w - 6
        avail_h = cell_h - label_h - 6
        scale = min(avail_w / bar.width, avail_h / bar.height)
        draw_w = bar.width * scale
        canvas.saveState()
        canvas.translate(x + (cell_w - draw_w) / 2, y + label_h + 2)
        canvas.scale(scale, scale)
        bar.drawOn(canvas, 0, 0)
        canvas.restoreState()
        if label:
            canvas.setFillColorRGB(0, 0, 0)
            canvas.setFont("Helvetica-Bold", 9)
            canvas.drawCentredString(x + cell_w / 2, y + 3, label)
        yield ("progress", index, name)
    canvas.save()
    yield ("result", buf.getvalue())


def _zip(rows: list[tuple[str, str, str | None]], kind: str, extra_quiet: int, module_px: int) -> Iterator[tuple]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, (name, data, label) in enumerate(rows):
            if kind == "jpg":
                payload: bytes | str = generate_barcode_jpg(
                    data, label=label, extra_quiet=extra_quiet
                )
            elif kind == "png":
                payload = generate_barcode_png(
                    data, label=label, module_px=module_px, extra_quiet=extra_quiet
                )
            elif kind == "svg":
                payload = generate_barcode_svg(data, label=label)
            else:
                payload = generate_barcode_eps(data, label=label)
            archive.writestr(name, payload)
            yield ("progress", index, name)
        archive.writestr("barcode_mapping.xlsx", mapping_xlsx(rows))
    yield ("result", buf.getvalue())


def barcode_download(parsed: dict) -> tuple[str, str]:
    """Return ``(mimetype, filename)`` for a barcode batch."""
    fmt = parsed["fmt"]
    stem = f"barcode_batch_{parsed['first_n']}_{parsed['last_n']}"
    if fmt in {"pdf", "pdf_single"}:
        return "application/pdf", f"{stem}.pdf"
    if fmt == "zip_eps":
        return "application/zip", f"{stem}_eps.zip"
    if fmt == "zip_jpg":
        return "application/zip", f"{stem}_jpg.zip"
    return "application/zip", f"{stem}.zip"


def iter_barcode_batch(parsed: dict) -> Iterator[tuple]:
    """Yield the same progress/result tokens the QR batch stream expects."""
    fmt = parsed["fmt"]
    extra_quiet = int(parsed.get("border") or 0)
    module_px = max(4, min(int(parsed.get("box_size") or 6), 8))
    if fmt == "pdf_single":
        yield from _pdf_single(_rows(parsed, "pdf"))
    elif fmt == "pdf":
        yield from _pdf_grid(_rows(parsed, "pdf"))
    elif fmt == "zip_svg":
        yield from _zip(_rows(parsed, "svg"), "svg", extra_quiet, module_px)
    elif fmt == "zip_eps":
        yield from _zip(_rows(parsed, "eps"), "eps", extra_quiet, module_px)
    elif fmt == "zip":
        yield from _zip(_rows(parsed, "png"), "png", extra_quiet, module_px)
    elif fmt == "zip_jpg":
        yield from _zip(_rows(parsed, "jpg"), "jpg", extra_quiet, module_px)
    else:
        raise ValueError(
            "format must be 'zip', 'zip_jpg', 'zip_svg', 'zip_eps', 'pdf', or 'pdf_single'"
        )
