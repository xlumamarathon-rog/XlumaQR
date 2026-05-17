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
* :func:`list_templates` - return the built-in design templates registry.
* :func:`get_template` - look up a template by id (raises ``ValueError``).
* :func:`render_template_preview` - render a small thumbnail PNG for a template.
"""

from __future__ import annotations

import base64
import copy
import functools
import io
import os
import xml.sax.saxutils
import zipfile
from typing import Iterable, Iterator

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_H, ERROR_CORRECT_M
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.colormasks import (
    HorizontalGradiantColorMask,
    RadialGradiantColorMask,
    SolidFillColorMask,
    SquareGradiantColorMask,
    VerticalGradiantColorMask,
)
from qrcode.image.styles.moduledrawers.pil import (
    CircleModuleDrawer,
    GappedSquareModuleDrawer,
    HorizontalBarsDrawer,
    RoundedModuleDrawer,
    SquareModuleDrawer,
    VerticalBarsDrawer,
)
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdf_canvas

__all__ = [
    "generate_qr",
    "generate_qr_svg",
    "compute_range",
    "generate_sequence",
    "generate_sequence_svg",
    "generate_sequence_render_plan",
    "images_to_zip",
    "images_to_pdf",
    "iter_batch_with_progress",
    "iter_batch_vector_with_progress",
    "list_templates",
    "get_template",
    "render_template_preview",
    "TEMPLATES",
    "MAX_RANGE_SIZE",
    "MAX_DATA_LENGTH",
    "MAX_BOX_SIZE",
    "MAX_BORDER",
    "MAX_PADDING",
    "MAX_LOGO_BYTES",
    "MAX_LOGO_DIMENSION",
    "LOGO_HARD_MAX_DIMENSION",
    "LOGO_WORK_SIZE",
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
#
# ``MAX_LOGO_BYTES`` is the hard byte cap on the multipart upload so a
# single request cannot exhaust the Lambda's memory. ``MAX_LOGO_DIMENSION``
# is the *auto-resize target*: uploads above this on either axis are
# scaled down to fit (preserving aspect ratio) by the HTTP layer's
# validator rather than rejected outright, so users can drop a phone
# camera screenshot into the form without thinking about pixel sizes.
# ``LOGO_HARD_MAX_DIMENSION`` is the absolute ceiling: dimensions above
# this are rejected with a 400 because the worst-case decoded bitmap
# (``LOGO_HARD_MAX_DIMENSION ** 2 * 4`` bytes for an RGBA decode, ~64 MB
# at 4096) is the largest allocation the validator will tolerate before
# the auto-resize step. ``LOGO_WORK_SIZE`` is the working size we
# resize a validated logo down to before pasting it onto the QR; the
# padded result is then handed to ``StyledPilImage``, which scales it
# to ``embedded_image_ratio = 0.22`` of the QR's pixel width. The
# value is set to 1024 (rather than the 256 it used to be) so every
# render in the box_size range we support is a clean LANCZOS
# *downscale* rather than an upscale. At the HD download size
# (``box_size = 40``, ~1640 px wide for a 33-module QR) the 22%
# centre region is ~360 px, so the previous 256 px source had to be
# upscaled and produced a visibly blurry logo on saved files; 1024
# leaves headroom even at the largest box_size. A 1024x1024 RGBA pad
# is ~4 MB, allocated once per render. This does *not* change peak
# memory (still dominated by the upload's pre-resize bitmap, bounded
# by ``LOGO_HARD_MAX_DIMENSION ** 2 * 4`` = ~64 MB at the 4096
# ceiling) but it does add ~4 MB to the steady-state per-render
# footprint, which is comfortably inside the 1 GB Lambda tier and
# still fine on a 256 MB tier. Resizing once up-front also avoids a
# fresh LANCZOS resize per item when the same logo is reused across
# a batch.
#
# The 4096 ceiling is chosen deliberately to give users headroom for
# phone-camera screenshots (modern phones routinely produce 4032x3024
# JPEGs). Each accepted upload allocates a worst-case ~64 MB RGBA
# bitmap during validation, and three coincident uploads sit at
# ~192 MB on top of the Flask/Pillow runtime, which fits comfortably
# inside the 1 GB tier we target on Vercel but lands close to the
# limit on a 256 MB Lambda. Lower this constant (to 2048, ~16 MB
# worst case, halving the peak twice) only if you are deploying on a
# tighter memory tier *and* you are willing to reject phone-camera
# screenshots above 2048 per side.
MAX_RANGE_SIZE = 5000
MAX_DATA_LENGTH = 2300
MAX_BOX_SIZE = 50
MAX_BORDER = 16
MAX_PADDING = 12
MAX_LOGO_BYTES = 2 * 1024 * 1024
MAX_LOGO_DIMENSION = 1024
LOGO_HARD_MAX_DIMENSION = 4096
LOGO_WORK_SIZE = 1024


# --- Label rendering -----------------------------------------------------
#
# The optional label drawn under a QR is rendered with a bundled
# Plus Jakarta Sans Bold TrueType font for a clean, premium appearance.
# The font is committed under ``static/fonts/`` next to its SIL OFL 1.1
# licence, so no runtime download is required and the deployable
# package stays self-contained.
LABEL_FONT_PATH = os.path.join(
    os.path.dirname(__file__), "static", "fonts", "PlusJakartaSans-Bold.ttf",
)


# Register the bundled TTF with reportlab once at import time so the
# vector PDF path can call ``c.setFont('PlusJakartaSans-Bold', size)``
# without re-registering on every render. ``registerFont`` is
# idempotent within a process but raises on import-side failure (e.g.
# the font file going missing); guard with try/except so a packaging
# accident does not break unrelated callers that never need the
# vector PDF path. The SVG path references the font by its CSS
# family name (``Plus Jakarta Sans``) and accepts system fallback
# rather than embedding the ~130 KB TTF as base64 in every SVG.
_PDF_LABEL_FONT_NAME = "PlusJakartaSans-Bold"
try:
    pdfmetrics.registerFont(TTFont(_PDF_LABEL_FONT_NAME, LABEL_FONT_PATH))
except Exception:  # pragma: no cover - defensive, font ships in-tree
    pass


# --- Templates registry --------------------------------------------------
#
# TEMPLATES is the built-in catalogue of QR design presets. Each entry is
# a plain dict so it can be JSON-serialised straight to the wire by the
# HTTP layer.
#
# Entry shape::
#
#     {
#         "id": <slug>,            # stable identifier (kebab-case)
#         "name": <human name>,    # what the UI displays
#         "category": <slug>,      # one of: default, marathon, running,
#                                  # duathlon, triathlon, cycling, swimming,
#                                  # business, event, wifi, social, personal
#         "spec": {
#             "module_drawer_kind": one of {square, rounded, circle,
#                                           gapped_square, vertical_bars,
#                                           horizontal_bars},
#             "color_mask_kind": one of {solid, radial_gradient,
#                                        square_gradient,
#                                        horizontal_gradient,
#                                        vertical_gradient},
#             # plus the named colour stops the chosen mask consumes;
#             # always RGB tuples:
#             "back_color": (r, g, b),
#             # solid:
#             "front_color": (r, g, b),
#             # radial / square gradient:
#             "center_color": (r, g, b),
#             "edge_color":   (r, g, b),
#             # horizontal gradient:
#             "left_color":   (r, g, b),
#             "right_color":  (r, g, b),
#             # vertical gradient:
#             "top_color":    (r, g, b),
#             "bottom_color": (r, g, b),
#         },
#     }
#
# The reserved ``default`` template (id ``default``, category ``default``)
# is wired so that callers passing ``template_id='default'`` with no logo
# fall back to the legacy plain-black-on-white render path byte-for-byte.
# Sport templates use warm reds/oranges for marathon/running, blue+yellow
# gradients for triathlon, blue/cyan for swimming, and green/yellow for
# cycling. General-use categories favour cleaner solids and gentle
# gradients suitable for day-to-day use.
TEMPLATES: list[dict] = [
    # --- default (legacy plain rendering) --------------------------------
    {
        "id": "default",
        "name": "Default (plain black & white)",
        "category": "default",
        "spec": {
            "module_drawer_kind": "square",
            "color_mask_kind": "solid",
            "back_color": (255, 255, 255),
            "front_color": (0, 0, 0),
        },
    },
    # --- marathon (4) ----------------------------------------------------
    {
        "id": "marathon-fire",
        "name": "Marathon - Fire",
        "category": "marathon",
        "spec": {
            "module_drawer_kind": "rounded",
            "color_mask_kind": "radial_gradient",
            "back_color": (255, 255, 255),
            "center_color": (255, 87, 34),
            "edge_color": (183, 28, 28),
        },
    },
    {
        "id": "marathon-sunset",
        "name": "Marathon - Sunset",
        "category": "marathon",
        "spec": {
            "module_drawer_kind": "rounded",
            "color_mask_kind": "vertical_gradient",
            "back_color": (255, 255, 255),
            "top_color": (255, 152, 0),
            "bottom_color": (191, 54, 12),
        },
    },
    {
        "id": "marathon-medal",
        "name": "Marathon - Medal",
        "category": "marathon",
        "spec": {
            "module_drawer_kind": "circle",
            "color_mask_kind": "horizontal_gradient",
            "back_color": (255, 255, 255),
            "left_color": (255, 193, 7),
            "right_color": (216, 67, 21),
        },
    },
    {
        "id": "marathon-asphalt",
        "name": "Marathon - Asphalt",
        "category": "marathon",
        "spec": {
            "module_drawer_kind": "square",
            "color_mask_kind": "solid",
            "back_color": (255, 255, 255),
            "front_color": (33, 33, 33),
        },
    },
    # --- running (4) -----------------------------------------------------
    {
        "id": "running-energy",
        "name": "Running - Energy",
        "category": "running",
        "spec": {
            "module_drawer_kind": "rounded",
            "color_mask_kind": "horizontal_gradient",
            "back_color": (255, 255, 255),
            "left_color": (244, 67, 54),
            "right_color": (255, 152, 0),
        },
    },
    {
        "id": "running-track",
        "name": "Running - Track",
        "category": "running",
        "spec": {
            "module_drawer_kind": "horizontal_bars",
            "color_mask_kind": "solid",
            "back_color": (255, 255, 255),
            "front_color": (211, 47, 47),
        },
    },
    {
        "id": "running-trail",
        "name": "Running - Trail",
        "category": "running",
        "spec": {
            "module_drawer_kind": "rounded",
            "color_mask_kind": "vertical_gradient",
            "back_color": (255, 255, 255),
            "top_color": (191, 54, 12),
            "bottom_color": (62, 39, 35),
        },
    },
    {
        "id": "running-dawn",
        "name": "Running - Dawn",
        "category": "running",
        "spec": {
            "module_drawer_kind": "circle",
            "color_mask_kind": "radial_gradient",
            "back_color": (255, 255, 255),
            "center_color": (255, 138, 101),
            "edge_color": (198, 40, 40),
        },
    },
    # --- duathlon (3) ----------------------------------------------------
    {
        "id": "duathlon-twin",
        "name": "Duathlon - Twin",
        "category": "duathlon",
        "spec": {
            "module_drawer_kind": "rounded",
            "color_mask_kind": "horizontal_gradient",
            "back_color": (255, 255, 255),
            "left_color": (211, 47, 47),
            "right_color": (46, 125, 50),
        },
    },
    {
        "id": "duathlon-relay",
        "name": "Duathlon - Relay",
        "category": "duathlon",
        "spec": {
            "module_drawer_kind": "gapped_square",
            "color_mask_kind": "vertical_gradient",
            "back_color": (255, 255, 255),
            "top_color": (245, 127, 23),
            "bottom_color": (27, 94, 32),
        },
    },
    {
        "id": "duathlon-stride",
        "name": "Duathlon - Stride",
        "category": "duathlon",
        "spec": {
            "module_drawer_kind": "square",
            "color_mask_kind": "solid",
            "back_color": (255, 255, 255),
            "front_color": (78, 52, 46),
        },
    },
    # --- triathlon (3) ---------------------------------------------------
    {
        "id": "triathlon-tri",
        "name": "Triathlon - Tri",
        "category": "triathlon",
        "spec": {
            "module_drawer_kind": "rounded",
            "color_mask_kind": "horizontal_gradient",
            "back_color": (255, 255, 255),
            "left_color": (13, 71, 161),
            "right_color": (255, 193, 7),
        },
    },
    {
        "id": "triathlon-ironwave",
        "name": "Triathlon - Ironwave",
        "category": "triathlon",
        "spec": {
            "module_drawer_kind": "circle",
            "color_mask_kind": "radial_gradient",
            "back_color": (255, 255, 255),
            "center_color": (255, 235, 59),
            "edge_color": (21, 101, 192),
        },
    },
    {
        "id": "triathlon-sprint",
        "name": "Triathlon - Sprint",
        "category": "triathlon",
        "spec": {
            "module_drawer_kind": "rounded",
            "color_mask_kind": "vertical_gradient",
            "back_color": (255, 255, 255),
            "top_color": (25, 118, 210),
            "bottom_color": (255, 167, 38),
        },
    },
    # --- cycling (4) -----------------------------------------------------
    {
        "id": "cycling-peloton",
        "name": "Cycling - Peloton",
        "category": "cycling",
        "spec": {
            "module_drawer_kind": "circle",
            "color_mask_kind": "horizontal_gradient",
            "back_color": (255, 255, 255),
            "left_color": (104, 159, 56),
            "right_color": (255, 235, 59),
        },
    },
    {
        "id": "cycling-mountain",
        "name": "Cycling - Mountain",
        "category": "cycling",
        "spec": {
            "module_drawer_kind": "rounded",
            "color_mask_kind": "vertical_gradient",
            "back_color": (255, 255, 255),
            "top_color": (46, 125, 50),
            "bottom_color": (27, 94, 32),
        },
    },
    {
        "id": "cycling-roadie",
        "name": "Cycling - Roadie",
        "category": "cycling",
        "spec": {
            "module_drawer_kind": "vertical_bars",
            "color_mask_kind": "solid",
            "back_color": (255, 255, 255),
            "front_color": (56, 142, 60),
        },
    },
    {
        "id": "cycling-criterium",
        "name": "Cycling - Criterium",
        "category": "cycling",
        "spec": {
            "module_drawer_kind": "gapped_square",
            "color_mask_kind": "radial_gradient",
            "back_color": (255, 255, 255),
            "center_color": (205, 220, 57),
            "edge_color": (33, 105, 49),
        },
    },
    # --- swimming (3) ----------------------------------------------------
    {
        "id": "swimming-lagoon",
        "name": "Swimming - Lagoon",
        "category": "swimming",
        "spec": {
            "module_drawer_kind": "circle",
            "color_mask_kind": "radial_gradient",
            "back_color": (255, 255, 255),
            "center_color": (0, 188, 212),
            "edge_color": (1, 87, 155),
        },
    },
    {
        "id": "swimming-tide",
        "name": "Swimming - Tide",
        "category": "swimming",
        "spec": {
            "module_drawer_kind": "rounded",
            "color_mask_kind": "vertical_gradient",
            "back_color": (255, 255, 255),
            "top_color": (3, 169, 244),
            "bottom_color": (13, 71, 161),
        },
    },
    {
        "id": "swimming-pool",
        "name": "Swimming - Pool",
        "category": "swimming",
        "spec": {
            "module_drawer_kind": "horizontal_bars",
            "color_mask_kind": "solid",
            "back_color": (255, 255, 255),
            "front_color": (2, 119, 189),
        },
    },
    # --- business (3) ----------------------------------------------------
    {
        "id": "business-slate",
        "name": "Business - Slate",
        "category": "business",
        "spec": {
            "module_drawer_kind": "square",
            "color_mask_kind": "solid",
            "back_color": (255, 255, 255),
            "front_color": (38, 50, 56),
        },
    },
    {
        "id": "business-navy",
        "name": "Business - Navy",
        "category": "business",
        "spec": {
            "module_drawer_kind": "rounded",
            "color_mask_kind": "vertical_gradient",
            "back_color": (255, 255, 255),
            "top_color": (26, 35, 126),
            "bottom_color": (13, 71, 161),
        },
    },
    {
        "id": "business-graphite",
        "name": "Business - Graphite",
        "category": "business",
        "spec": {
            "module_drawer_kind": "gapped_square",
            "color_mask_kind": "solid",
            "back_color": (255, 255, 255),
            "front_color": (55, 71, 79),
        },
    },
    {
        "id": "business-square-frame",
        "name": "Business - Square Frame",
        "category": "business",
        "spec": {
            "module_drawer_kind": "rounded",
            "color_mask_kind": "square_gradient",
            "back_color": (255, 255, 255),
            "center_color": (55, 71, 79),
            "edge_color": (13, 71, 161),
        },
    },
    # --- event (3) -------------------------------------------------------
    {
        "id": "event-festival",
        "name": "Event - Festival",
        "category": "event",
        "spec": {
            "module_drawer_kind": "rounded",
            "color_mask_kind": "horizontal_gradient",
            "back_color": (255, 255, 255),
            "left_color": (171, 71, 188),
            "right_color": (255, 87, 34),
        },
    },
    {
        "id": "event-concert",
        "name": "Event - Concert",
        "category": "event",
        "spec": {
            "module_drawer_kind": "circle",
            "color_mask_kind": "radial_gradient",
            "back_color": (255, 255, 255),
            "center_color": (236, 64, 122),
            "edge_color": (74, 20, 140),
        },
    },
    {
        "id": "event-conference",
        "name": "Event - Conference",
        "category": "event",
        "spec": {
            "module_drawer_kind": "square",
            "color_mask_kind": "solid",
            "back_color": (255, 255, 255),
            "front_color": (49, 27, 146),
        },
    },
    # --- wifi (3) --------------------------------------------------------
    {
        "id": "wifi-azure",
        "name": "WiFi - Azure",
        "category": "wifi",
        "spec": {
            "module_drawer_kind": "rounded",
            "color_mask_kind": "radial_gradient",
            "back_color": (255, 255, 255),
            "center_color": (3, 169, 244),
            "edge_color": (1, 87, 155),
        },
    },
    {
        "id": "wifi-mint",
        "name": "WiFi - Mint",
        "category": "wifi",
        "spec": {
            "module_drawer_kind": "circle",
            "color_mask_kind": "horizontal_gradient",
            "back_color": (255, 255, 255),
            "left_color": (0, 137, 123),
            "right_color": (38, 166, 154),
        },
    },
    {
        "id": "wifi-signal",
        "name": "WiFi - Signal",
        "category": "wifi",
        "spec": {
            "module_drawer_kind": "horizontal_bars",
            "color_mask_kind": "solid",
            "back_color": (255, 255, 255),
            "front_color": (2, 136, 209),
        },
    },
    # --- social (3) ------------------------------------------------------
    {
        "id": "social-bubblegum",
        "name": "Social - Bubblegum",
        "category": "social",
        "spec": {
            "module_drawer_kind": "rounded",
            "color_mask_kind": "horizontal_gradient",
            "back_color": (255, 255, 255),
            "left_color": (236, 64, 122),
            "right_color": (255, 152, 0),
        },
    },
    {
        "id": "social-aurora",
        "name": "Social - Aurora",
        "category": "social",
        "spec": {
            "module_drawer_kind": "circle",
            "color_mask_kind": "vertical_gradient",
            "back_color": (255, 255, 255),
            "top_color": (123, 31, 162),
            "bottom_color": (3, 169, 244),
        },
    },
    {
        "id": "social-sunrise",
        "name": "Social - Sunrise",
        "category": "social",
        "spec": {
            "module_drawer_kind": "rounded",
            "color_mask_kind": "radial_gradient",
            "back_color": (255, 255, 255),
            "center_color": (255, 213, 79),
            "edge_color": (244, 81, 30),
        },
    },
    # --- personal (3) ----------------------------------------------------
    {
        "id": "personal-forest",
        "name": "Personal - Forest",
        "category": "personal",
        "spec": {
            "module_drawer_kind": "rounded",
            "color_mask_kind": "vertical_gradient",
            "back_color": (255, 255, 255),
            "top_color": (46, 125, 50),
            "bottom_color": (27, 94, 32),
        },
    },
    {
        "id": "personal-plum",
        "name": "Personal - Plum",
        "category": "personal",
        "spec": {
            "module_drawer_kind": "circle",
            "color_mask_kind": "solid",
            "back_color": (255, 255, 255),
            "front_color": (106, 27, 154),
        },
    },
    {
        "id": "personal-denim",
        "name": "Personal - Denim",
        "category": "personal",
        "spec": {
            "module_drawer_kind": "gapped_square",
            "color_mask_kind": "horizontal_gradient",
            "back_color": (255, 255, 255),
            "left_color": (40, 53, 147),
            "right_color": (92, 107, 192),
        },
    },
]


_DRAWER_FACTORIES = {
    "square": SquareModuleDrawer,
    "rounded": RoundedModuleDrawer,
    "circle": CircleModuleDrawer,
    "gapped_square": GappedSquareModuleDrawer,
    "vertical_bars": VerticalBarsDrawer,
    "horizontal_bars": HorizontalBarsDrawer,
}


def _resolve_drawer(kind: str):
    """Return a fresh module-drawer instance for ``kind``.

    Raises :class:`ValueError` if ``kind`` is not one of the registered
    drawer kinds. The registry is closed on purpose so a typo in the
    template data raises eagerly rather than silently rendering as
    ``SquareModuleDrawer``.
    """
    factory = _DRAWER_FACTORIES.get(kind)
    if factory is None:
        raise ValueError(f"unknown module_drawer_kind: {kind!r}")
    return factory()


def _resolve_color_mask(spec: dict):
    """Build the right ``color_mask`` instance from a template ``spec``.

    Raises :class:`ValueError` on an unknown ``color_mask_kind`` or on a
    missing colour stop required by the chosen mask.
    """
    kind = spec.get("color_mask_kind")
    back = spec.get("back_color", (255, 255, 255))
    if kind == "solid":
        front = spec.get("front_color")
        if front is None:
            raise ValueError("solid mask requires front_color")
        return SolidFillColorMask(back_color=back, front_color=front)
    if kind == "radial_gradient":
        center = spec.get("center_color")
        edge = spec.get("edge_color")
        if center is None or edge is None:
            raise ValueError(
                "radial_gradient mask requires center_color and edge_color",
            )
        return RadialGradiantColorMask(
            back_color=back, center_color=center, edge_color=edge,
        )
    if kind == "square_gradient":
        center = spec.get("center_color")
        edge = spec.get("edge_color")
        if center is None or edge is None:
            raise ValueError(
                "square_gradient mask requires center_color and edge_color",
            )
        return SquareGradiantColorMask(
            back_color=back, center_color=center, edge_color=edge,
        )
    if kind == "horizontal_gradient":
        left = spec.get("left_color")
        right = spec.get("right_color")
        if left is None or right is None:
            raise ValueError(
                "horizontal_gradient mask requires left_color and right_color",
            )
        return HorizontalGradiantColorMask(
            back_color=back, left_color=left, right_color=right,
        )
    if kind == "vertical_gradient":
        top = spec.get("top_color")
        bottom = spec.get("bottom_color")
        if top is None or bottom is None:
            raise ValueError(
                "vertical_gradient mask requires top_color and bottom_color",
            )
        return VerticalGradiantColorMask(
            back_color=back, top_color=top, bottom_color=bottom,
        )
    raise ValueError(f"unknown color_mask_kind: {kind!r}")


def _label_color_from_spec(spec: dict) -> tuple[int, int, int]:
    """Return the representative foreground RGB for a template ``spec``.

    Both label layouts use this colour so the printed text matches the
    QR's visual identity:

    * the band drawn under the QR (when a label is supplied alongside
      a logo), and
    * the centre badge drawn on the QR (when a label is supplied
      without a logo).

    Each ``color_mask_kind`` maps onto a single representative stop:

    * ``solid`` -> ``front_color``
    * ``radial_gradient`` -> ``center_color``
    * ``square_gradient`` -> ``center_color``
    * ``horizontal_gradient`` -> ``left_color``
    * ``vertical_gradient`` -> ``top_color``

    Raises :class:`ValueError` on an unknown ``color_mask_kind``,
    mirroring the closed-set policy in :func:`_resolve_color_mask`.
    """
    kind = spec.get("color_mask_kind")
    if kind == "solid":
        return spec["front_color"]
    if kind == "radial_gradient":
        return spec["center_color"]
    if kind == "square_gradient":
        return spec["center_color"]
    if kind == "horizontal_gradient":
        return spec["left_color"]
    if kind == "vertical_gradient":
        return spec["top_color"]
    raise ValueError(f"unknown color_mask_kind: {kind!r}")


@functools.lru_cache(maxsize=128)
def _load_label_font(size_px: int):
    """Return the bundled Plus Jakarta Sans Bold TTF at ``size_px``.

    Falls back to :func:`PIL.ImageFont.load_default` only if PIL cannot
    open the bundled file (``OSError``). The fallback is purely
    defensive: the font ships with the source tree at
    :data:`LABEL_FONT_PATH` so the truetype path is the expected one.

    Cached by ``size_px`` because :data:`LABEL_FONT_PATH` is module-
    constant. The centre-badge autofit loop in
    :func:`_render_label_badge` walks a range of font sizes (4 px
    steps from ~30% of ``LOGO_WORK_SIZE`` down to a 24 px floor); at
    ``LOGO_WORK_SIZE = 1024`` the worst case is exactly 72 distinct
    sizes (307, 303, ..., 27, 24) for a long label that needs the
    floor. ``maxsize=128`` covers the entire autofit range plus the
    band-below font sizes with headroom, so a re-render of the same
    label hits the cache on every step instead of evicting the
    largest sizes between renders.
    """
    try:
        return ImageFont.truetype(LABEL_FONT_PATH, size_px)
    except OSError:
        return ImageFont.load_default()


def list_templates() -> list[dict]:
    """Return a defensive deep copy of the built-in :data:`TEMPLATES` list.

    The returned list has the same shape as :data:`TEMPLATES` (each entry
    is a dict with ``id``, ``name``, ``category``, ``spec``). Callers are
    free to mutate the result without affecting the registry.
    """
    return copy.deepcopy(TEMPLATES)


def get_template(template_id: str) -> dict:
    """Look up a template by its slug ``id``.

    Returns a deep copy of the matching entry. Raises :class:`ValueError`
    with a clear message if no template with that id exists; this is the
    error shape the HTTP layer turns into a clean 400/404.
    """
    for entry in TEMPLATES:
        if entry["id"] == template_id:
            return copy.deepcopy(entry)
    raise ValueError(f"unknown template id: {template_id}")


def _pad_logo(logo: Image.Image, target_size_px: int) -> Image.Image:
    """Wrap ``logo`` in a white rounded-square pad.

    ``StyledPilImage`` pastes the supplied embedded image as-is, so
    without a white background ring around the logo the QR's modules
    can clip the logo's outline. This helper paints a slightly rounded
    white square the size of ``target_size_px``, fits the logo into
    about 80% of that area centred inside it, and returns the result
    as an RGBA image. The 80% pad leaves a small white margin on every
    side, which is what keeps the QR scannable.

    The corner radius is capped to be strictly less than the offset
    between the canvas edge and the logo's bounding box so the rounded
    corner stays in the white margin and never cuts past the logo's
    outer edge. A naive fixed ``target_size_px // 8`` radius can eat
    inward past the logo's corners when the logo fills the inner 80%
    of the pad (offsets ~26 px at ``target_size_px=256`` while the
    fixed radius would be 32 px), which would leave the logo's corners
    unprotected by the white ring.
    """
    if target_size_px <= 0:
        raise ValueError("target_size_px must be > 0")

    # Fit the logo into ~80% of the pad while preserving aspect ratio.
    # We need to know the actual placed offsets before drawing the
    # rounded rectangle so the corner radius can be capped inside the
    # margin.
    inner = max(1, int(target_size_px * 0.80))
    work = logo.convert("RGBA").copy()
    work.thumbnail((inner, inner), Image.LANCZOS)
    offset_x = (target_size_px - work.width) // 2
    offset_y = (target_size_px - work.height) // 2

    # Build the white rounded-square pad as the base canvas. The corner
    # radius is capped below the smallest offset so the rounded curve
    # stays strictly inside the margin and the logo's bounding box is
    # entirely surrounded by solid white. ``- 2`` keeps a one-pixel
    # white sliver between the rounded edge and the logo perimeter so
    # PIL's anti-aliased curve cannot brush against the logo border.
    margin_cap = max(2, min(offset_x, offset_y) - 2)
    radius = max(2, min(target_size_px // 8, margin_cap))
    pad = Image.new("RGBA", (target_size_px, target_size_px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(pad)
    draw.rounded_rectangle(
        [(0, 0), (target_size_px - 1, target_size_px - 1)],
        radius=radius,
        fill=(255, 255, 255, 255),
    )

    pad.paste(work, (offset_x, offset_y), mask=work)
    return pad


def _is_default_template(template_id: str | None) -> bool:
    """Return True if ``template_id`` represents the legacy plain render."""
    return template_id is None or template_id == "default"


def _render_label_badge(
    label: str,
    fg_color: tuple[int, int, int],
    target_size_px: int = LOGO_WORK_SIZE,
) -> Image.Image:
    """Render ``label`` as a centred badge on a white rounded-square pad.

    Returns an RGBA image of size ``target_size_px`` square containing
    ``label`` rendered in ``fg_color`` on the same white rounded pad
    that :func:`_pad_logo` builds for an embedded logo. The pad shape
    (rounded corners, ~80% inner area, white fill) is shared with the
    logo embed by routing the rendered text image through
    :func:`_pad_logo` itself, so the centre badge and the logo embed
    are visually consistent and any future tweak to the pad geometry
    flows to both code paths automatically.

    The font size is auto-fitted: it starts at ~30% of
    ``target_size_px`` and shrinks in 4 px steps until the rendered
    text bounding box fits inside ~70% of the canvas on both axes,
    with a hard floor of 24 px so very long labels still render at a
    legible size (the text image will be downscaled by
    :func:`_pad_logo`'s thumbnail step in that case).
    """
    if target_size_px <= 0:
        raise ValueError("target_size_px must be > 0")

    inner_max = max(1, int(target_size_px * 0.70))
    font_size = max(24, int(target_size_px * 0.30))
    floor = 24

    font = _load_label_font(font_size)
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    while True:
        try:
            bbox = measure.textbbox((0, 0), label, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            text_offset_x = -bbox[0]
            text_offset_y = -bbox[1]
        except AttributeError:
            # Pillow < 9.2 fallback.
            text_w, text_h = measure.textsize(label, font=font)  # type: ignore[attr-defined]
            text_offset_x = 0
            text_offset_y = 0
        if (text_w <= inner_max and text_h <= inner_max) or font_size <= floor:
            break
        font_size = max(floor, font_size - 4)
        font = _load_label_font(font_size)

    # Render the glyph onto an RGBA scratch canvas sized to its bbox so
    # _pad_logo can centre it inside the pad's inner ~80% area.
    scratch_w = max(1, text_w)
    scratch_h = max(1, text_h)
    text_img = Image.new("RGBA", (scratch_w, scratch_h), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(text_img)
    tdraw.text(
        (text_offset_x, text_offset_y),
        label,
        fill=(fg_color[0], fg_color[1], fg_color[2], 255),
        font=font,
    )

    return _pad_logo(text_img, target_size_px)


def generate_qr(
    data: str,
    label: str | None = None,
    box_size: int = 10,
    border: int = 4,
    label_height: int | None = None,
    template_id: str | None = None,
    logo: Image.Image | None = None,
) -> Image.Image:
    """Render ``data`` as a QR code with an optional label.

    The optional ``label`` is rendered in one of two layouts depending
    on whether a ``logo`` is also supplied:

    * **No logo, label only**: the label is drawn as a centred badge on
      the QR pattern, on the same white rounded-square pad that an
      embedded logo would sit on. Error correction is bumped to
      :data:`qrcode.constants.ERROR_CORRECT_H` so the QR remains
      scannable with the centre region occupied. A payload that fits
      at M-mode without a label may overflow at H-mode with a centre
      label, in the same way a logo embed does, surfacing as a
      :class:`ValueError` from the underlying ``qrcode`` library.
    * **Logo plus label**: the logo occupies the centre and the label
      is rendered in a clean white band drawn directly under the QR
      pattern. The returned image is taller than the bare QR by the
      band's height. The band's font size scales with the QR's pixel
      height (~12% with a 14 px floor) and uses the bundled
      Plus Jakarta Sans Bold TTF (see :data:`LABEL_FONT_PATH`).

    When neither ``template_id`` nor ``logo`` nor ``label`` is supplied
    (the common case), the QR is built with
    :data:`qrcode.constants.ERROR_CORRECT_M` and rendered via the
    legacy ``qr.make_image(fill_color, back_color)`` path,
    byte-for-byte identical to earlier releases. Note that supplying a
    ``label`` alone (no ``template_id``, no ``logo``) now routes
    through the styled path with a centre badge: those bytes no longer
    match the legacy output. The legacy bytes are preserved only when
    ``label``, ``template_id``, and ``logo`` are all unset.

    When ``template_id`` is supplied (and is not the literal
    ``default``), the QR is rendered through
    ``qrcode.image.styledpil.StyledPilImage`` using the template's
    module drawer and colour mask. The label colour follows the
    template's representative stop (see :func:`_label_color_from_spec`)
    in both the centre-badge and band-below layouts; the legacy plain
    path uses pure black for label text.

    Parameters
    ----------
    data:
        Payload encoded into the QR code.
    label:
        Optional text. ``None`` returns the bare QR. With a logo the
        label is drawn in a band below the QR; without a logo the
        label is drawn as a centred badge on the QR.
    box_size:
        Pixel size of each QR module (passed through to ``qrcode``).
    border:
        Quiet-zone width in modules (passed through to ``qrcode``).
    label_height:
        Deprecated. Retained for backwards compatibility but ignored:
        the band's height (when a label and a logo are both supplied)
        is now derived from the chosen font size, which scales with
        the QR's pixel height.
    template_id:
        Optional template slug. ``None`` and ``"default"`` both take
        the legacy plain-black-on-white render path when no centre
        region is occupied. Any other id is resolved via
        :func:`get_template` (raises :class:`ValueError` on unknown).
    logo:
        Optional :class:`PIL.Image.Image` to embed at the centre of
        the QR. When supplied, error correction is upgraded to
        ``ERROR_CORRECT_H`` and the logo is pre-wrapped in a white
        rounded-square pad. If only ``logo`` is supplied without a
        template, the ``default`` template (plain black on white) is
        used as the colour mask.

    Returns
    -------
    PIL.Image.Image
        RGB image of the rendered QR. When ``label`` is supplied
        alongside a ``logo`` the image is taller than the bare QR by
        the height of the label band; when ``label`` is supplied
        without a ``logo`` the image keeps the bare QR's size and the
        centre region carries the label badge.
    """
    centre_label = label is not None and logo is None
    use_styled = (
        not _is_default_template(template_id)
        or logo is not None
        or centre_label
    )

    spec: dict | None = None
    if not use_styled:
        # --- Legacy fast path: byte-for-byte identical to earlier releases.
        qr = qrcode.QRCode(
            error_correction=ERROR_CORRECT_M,
            box_size=box_size,
            border=border,
        )
        qr.add_data(data)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    else:
        # --- Styled path: StyledPilImage with optional embedded logo
        #     or centre label badge.
        spec_id = template_id if template_id is not None else "default"
        template = get_template(spec_id)
        spec = template["spec"]
        drawer = _resolve_drawer(spec["module_drawer_kind"])
        color_mask = _resolve_color_mask(spec)

        error_correction = (
            ERROR_CORRECT_H
            if (logo is not None or centre_label)
            else ERROR_CORRECT_M
        )
        qr = qrcode.QRCode(
            error_correction=error_correction,
            box_size=box_size,
            border=border,
        )
        qr.add_data(data)
        qr.make(fit=True)

        if logo is not None:
            embedded_image = _pad_logo(logo, LOGO_WORK_SIZE)
        elif centre_label:
            if _is_default_template(template_id):
                badge_fg: tuple[int, int, int] = (0, 0, 0)
            else:
                badge_fg = _label_color_from_spec(spec)
            embedded_image = _render_label_badge(label, badge_fg, LOGO_WORK_SIZE)
        else:
            embedded_image = None

        styled = qr.make_image(
            image_factory=StyledPilImage,
            module_drawer=drawer,
            color_mask=color_mask,
            embedded_image=embedded_image,
            embedded_image_ratio=0.22,
        )
        qr_img = styled.get_image().convert("RGB")

    if label is None or centre_label:
        # No label, or the label is already drawn as a centre badge in
        # the styled path above; either way nothing more to draw.
        return qr_img

    # --- Band-below layout: label is supplied alongside a logo.
    qr_w, qr_h = qr_img.size

    # Choose a font size proportional to the QR's pixel height (~12%)
    # with a floor that keeps the label legible on small renders.
    font_size = max(14, qr_h * 12 // 100)
    font = _load_label_font(font_size)

    # Measure the label text on a scratch ImageDraw so the canvas size
    # below is sized to fit.
    scratch = ImageDraw.Draw(qr_img)
    try:
        bbox = scratch.textbbox((0, 0), label, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        text_offset_x = -bbox[0]
        text_offset_y = -bbox[1]
    except AttributeError:
        # Pillow < 9.2 fallback (kept for safety; modern Pillow has textbbox).
        text_w, text_h = scratch.textsize(label, font=font)  # type: ignore[attr-defined]
        text_offset_x = 0
        text_offset_y = 0

    # Padding around the text in the band scales with the font size so
    # the band breathes proportionally to the QR. ``pad_x`` is computed
    # for parity with ``pad_y`` (the band would shrink to fit a wider
    # label by the same logic on both axes); the band currently spans
    # the full QR width, so the horizontal pad is informational only
    # and the text is centred via ``(qr_w - text_w) // 2`` below.
    pad_y = max(box_size, font_size // 2)
    pad_x = max(box_size, font_size // 2)  # noqa: F841 - reserved for future side-aligned layouts
    band_h = text_h + 2 * pad_y

    # Determine the label foreground colour. The legacy render path
    # uses plain black; templated renders pick the representative stop
    # for the chosen colour mask so the label visually matches the QR.
    if _is_default_template(template_id) or spec is None:
        fg_color: tuple[int, int, int] = (0, 0, 0)
    else:
        fg_color = _label_color_from_spec(spec)

    # Build a fresh white canvas tall enough for the QR plus the band,
    # paste the QR at the top, and draw the label centred in the band.
    canvas = Image.new("RGB", (qr_w, qr_h + band_h), (255, 255, 255))
    canvas.paste(qr_img, (0, 0))
    draw = ImageDraw.Draw(canvas)
    text_x = (qr_w - text_w) // 2 + text_offset_x
    text_y = qr_h + pad_y + text_offset_y
    draw.text((text_x, text_y), label, fill=fg_color, font=font)

    return canvas


def render_template_preview(template_id: str) -> bytes:
    """Render a small thumbnail PNG for ``template_id``.

    The preview encodes a fixed short payload (``"XlumaQR"``) at a small
    box size so the result stays light enough to ship in an HTTP response
    cheaply. This function is pure: no caching, no I/O. The HTTP layer
    is free to memoise its results in a per-process dict.

    Raises :class:`ValueError` if ``template_id`` is not in the registry.
    """
    image = generate_qr(
        "XlumaQR",
        box_size=4,
        border=2,
        template_id=template_id,
    )
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


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
    template_id: str | None = None,
    logo: Image.Image | None = None,
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

    The optional ``template_id`` and ``logo`` parameters are forwarded to
    each :func:`generate_qr` call so every QR in the sequence is rendered
    with the same design and embedded logo. The defaults preserve the
    legacy plain-black-on-white render path so existing call sites are
    unaffected.
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
            template_id=template_id,
            logo=logo,
        )
        yield f"{prefix}{n}.png", image


def _pack_zip(
    items: Iterable[tuple[str, Image.Image]],
) -> Iterator[tuple]:
    """Pack ``items`` into a ZIP archive in memory, yielding progress.

    Internal generator shared by :func:`images_to_zip` (which discards
    progress events and keeps only the terminal payload) and
    :func:`iter_batch_with_progress` (which forwards both kinds of
    events to the HTTP wire). Centralising the loop here keeps the
    archive-building logic in one place so the synchronous and
    streaming routes cannot drift.

    Yields
    ------
    tuple
        ``("progress", index_zero_based, filename)`` after each entry
        is written into the archive, then ``("result", zip_bytes)``
        exactly once when the source iterator is exhausted.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for index, (filename, image) in enumerate(items):
            png_buf = io.BytesIO()
            image.save(png_buf, format="PNG")
            zf.writestr(filename, png_buf.getvalue())
            yield ("progress", index, filename)
    yield ("result", buffer.getvalue())


def _pack_pdf(
    items: Iterable[tuple[str, Image.Image]],
    page_size: tuple[float, float] = LETTER,
    cols: int = 3,
    rows: int = 4,
    margin_pt: float = 36.0,
) -> Iterator[tuple]:
    """Lay out ``items`` on a PDF grid in memory, yielding progress.

    Internal generator shared by :func:`images_to_pdf` and
    :func:`iter_batch_with_progress`. All PDF layout maths (page size,
    grid density, margin, cell-fit scaling, page-break trigger) live
    here so the synchronous and streaming routes cannot diverge if
    defaults are tuned.

    Memory note
    -----------
    Unlike :func:`_pack_zip`, peak memory here grows with the number
    of items: ``reportlab.pdfgen.canvas.Canvas`` retains drawn-image
    data inside the canvas's content stream and resource dictionary
    until ``c.save()`` finalizes the document. The PIL ``Image`` itself
    is released after each ``drawImage`` call returns, but the PNG
    bytes that reportlab extracts from it stay alive until the final
    yield.

    Yields
    ------
    tuple
        ``("progress", index_zero_based, filename)`` after each draw,
        then ``("result", pdf_bytes)`` exactly once when the source
        iterator is exhausted.
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
    for index, (filename, image) in enumerate(items):
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
        yield ("progress", index, filename)

    c.showPage()
    c.save()
    yield ("result", buffer.getvalue())


def images_to_zip(items: Iterable[tuple[str, Image.Image]]) -> bytes:
    """Pack ``(filename, image)`` pairs into a ZIP archive in memory.

    Each image is encoded as PNG and stored under its given filename.
    Returns the raw bytes of the ZIP archive.
    """
    payload = b""
    for token in _pack_zip(items):
        if token[0] == "result":
            payload = token[1]
    return payload


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
    payload = b""
    for token in _pack_pdf(
        items,
        page_size=page_size,
        cols=cols,
        rows=rows,
        margin_pt=margin_pt,
    ):
        if token[0] == "result":
            payload = token[1]
    return payload


def iter_batch_with_progress(
    items: Iterable[tuple[str, Image.Image]],
    fmt: str,
) -> Iterator[tuple]:
    """Pack ``items`` into a ZIP or PDF, yielding progress events lazily.

    This is the streaming counterpart to :func:`images_to_zip` and
    :func:`images_to_pdf`. ``items`` is consumed lazily (typically the
    iterator returned by :func:`generate_sequence`) so the source
    iterator only ever has one ``(filename, PIL.Image.Image)`` pair
    pending at a time. After each pair has been pulled and written into
    the output container, a progress tuple is yielded; once the source
    iterator is exhausted, a single result tuple is yielded carrying
    the packed bytes.

    Internally, both formats delegate to the shared :func:`_pack_zip` /
    :func:`_pack_pdf` generators so the synchronous (:func:`images_to_zip`,
    :func:`images_to_pdf`) and streaming routes share one source of
    truth for archive building and PDF layout.

    Yields
    ------
    tuple
        Either ``("progress", index_zero_based, filename)`` after each
        item is rendered into the container, or
        ``("result", payload_bytes)`` exactly once at the end.

    Parameters
    ----------
    items:
        Iterable of ``(filename, PIL.Image.Image)`` pairs.
    fmt:
        ``"zip"`` or ``"pdf"``. Anything else raises :class:`ValueError`.

    Memory profile
    --------------
    The two formats have asymmetric peak-memory behaviour:

    * ``"zip"``: peak live state is one ``PIL.Image`` plus its PNG
      buffer plus the growing in-memory archive bytes. The source
      iterator is pulled strictly one item at a time.
    * ``"pdf"``: the source iterator is also pulled one item at a time,
      but ``reportlab.pdfgen.canvas.Canvas`` retains drawn-image data
      inside the canvas's content stream and resource dictionary until
      ``c.save()`` finalizes the document, so the PNG bytes for every
      drawn page accumulate in memory until the terminal ``result``
      event. Peak memory therefore scales with the batch size on the
      PDF path. Callers sizing very large PDF batches should account
      for this.

    Notes
    -----
    The generator is the natural shape for an HTTP layer that wants to
    flush a wire-level progress event after each rendered item: the
    caller drives the loop with ``next()`` (or ``for``), forwards each
    progress tuple to the wire, and turns the terminal result tuple
    into the final response chunk. Encoder ``ValueError`` propagates
    out of the iterator so the caller can surface it as a clean
    application-level error.
    """
    if fmt == "zip":
        yield from _pack_zip(items)
    elif fmt == "pdf":
        yield from _pack_pdf(items)
    else:
        raise ValueError("fmt must be 'zip' or 'pdf'")


# --- Vector rendering (SVG single + ZIP-of-SVGs + native vector PDF) ---


def _svg_color_attr(rgb: tuple[int, int, int]) -> str:
    """Return the SVG ``rgb(r, g, b)`` literal for an RGB tuple."""
    r, g, b = rgb
    return f"rgb({r}, {g}, {b})"


def _autofit_centre_badge_font_size(
    label: str,
    badge_side_px: float,
) -> int:
    """Return the autofitted font-size in pixels for a centre-badge label.

    Mirrors the autofit loop in :func:`_render_label_badge` but returns
    just the chosen font size in pixels rather than a rendered image:
    starts at ~30% of ``badge_side_px``, steps down by 4 px until the
    rendered text bounding box fits inside ~70% of the badge area on
    both axes, with a 24 px floor. Measurements use the bundled TTF
    via :func:`PIL.ImageFont.truetype` plus :meth:`PIL.ImageDraw.textbbox`,
    so the SVG output uses the same chosen size as the PIL render
    would for the same label.
    """
    inner_max = max(1, int(badge_side_px * 0.70))
    font_size = max(24, int(badge_side_px * 0.30))
    floor = 24

    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    while True:
        font = _load_label_font(font_size)
        try:
            bbox = measure.textbbox((0, 0), label, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
        except AttributeError:  # pragma: no cover - Pillow < 9.2 fallback
            text_w, text_h = measure.textsize(label, font=font)  # type: ignore[attr-defined]
        if (text_w <= inner_max and text_h <= inner_max) or font_size <= floor:
            return font_size
        font_size = max(floor, font_size - 4)


def _build_qr_for_render_plan(
    data: str,
    template_id: str | None,
    logo: Image.Image | None,
    label: str | None,
    box_size: int,
    border: int,
):
    """Build a fitted ``qrcode.QRCode`` instance with the right error level.

    Centralises the error-correction policy used by both
    :func:`generate_qr_svg` and :func:`generate_sequence_render_plan`:
    bump to ``ERROR_CORRECT_H`` whenever a logo or centre-label is
    present (matches :func:`generate_qr`), otherwise ``ERROR_CORRECT_M``.
    Returns the fitted ``QRCode`` so callers can read ``qr.modules``
    and ``qr.modules_count`` directly.
    """
    centre_label = label is not None and logo is None
    error_correction = (
        ERROR_CORRECT_H if (logo is not None or centre_label) else ERROR_CORRECT_M
    )
    qr = qrcode.QRCode(
        error_correction=error_correction,
        box_size=box_size,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    return qr


def _merge_runs_horizontal(modules: list[list[bool]]) -> Iterator[tuple[int, int, int]]:
    """Yield ``(row, col_start, col_end_exclusive)`` runs of on-modules per row.

    Used by the ``horizontal_bars`` SVG / PDF code to emit a single
    rectangle per consecutive horizontal run instead of one per module.
    """
    for row, cells in enumerate(modules):
        col = 0
        n = len(cells)
        while col < n:
            if cells[col]:
                start = col
                while col < n and cells[col]:
                    col += 1
                yield row, start, col
            else:
                col += 1


def _merge_runs_vertical(modules: list[list[bool]]) -> Iterator[tuple[int, int, int]]:
    """Yield ``(row_start, row_end_exclusive, col)`` runs of on-modules per column.

    Used by the ``vertical_bars`` SVG / PDF code to emit a single
    rectangle per consecutive vertical run instead of one per module.
    """
    if not modules:
        return
    n_rows = len(modules)
    n_cols = len(modules[0]) if n_rows else 0
    for col in range(n_cols):
        row = 0
        while row < n_rows:
            if modules[row][col]:
                start = row
                while row < n_rows and modules[row][col]:
                    row += 1
                yield start, row, col
            else:
                row += 1


def _render_modules_svg(
    modules: list[list[bool]],
    border: int,
    box_size: int,
    drawer_kind: str,
    fill_attr: str,
) -> str:
    """Render the on-modules of ``modules`` as SVG primitives.

    Returns the markup for a single ``<g>`` element whose ``fill``
    attribute is ``fill_attr`` (typically ``rgb(r, g, b)`` or
    ``url(#...)``) wrapping the per-module shapes. The shape choice
    follows the closed set used by the styled PIL renderer:

    * ``square`` -> ``<rect>`` per on-module.
    * ``rounded`` -> ``<rect>`` with ``rx=ry=box_size/2``.
    * ``circle`` -> ``<circle>`` with ``r=box_size/2``.
    * ``gapped_square`` -> ``<rect>`` inset by ~10% of ``box_size``.
    * ``vertical_bars`` -> a single ``<rect>`` per consecutive
      column run (height = run_length * box_size).
    * ``horizontal_bars`` -> a single ``<rect>`` per consecutive
      row run (width = run_length * box_size).
    """
    parts: list[str] = [f'<g fill="{fill_attr}">']
    if drawer_kind == "vertical_bars":
        for row_start, row_end, col in _merge_runs_vertical(modules):
            x = (border + col) * box_size
            y = (border + row_start) * box_size
            w = box_size
            h = (row_end - row_start) * box_size
            parts.append(
                f'<rect x="{x}" y="{y}" width="{w}" height="{h}"/>'
            )
    elif drawer_kind == "horizontal_bars":
        for row, col_start, col_end in _merge_runs_horizontal(modules):
            x = (border + col_start) * box_size
            y = (border + row) * box_size
            w = (col_end - col_start) * box_size
            h = box_size
            parts.append(
                f'<rect x="{x}" y="{y}" width="{w}" height="{h}"/>'
            )
    else:
        # Per-module primitives.
        inset = max(1, int(round(box_size * 0.10))) if drawer_kind == "gapped_square" else 0
        radius = box_size / 2.0
        for row, cells in enumerate(modules):
            for col, on in enumerate(cells):
                if not on:
                    continue
                x = (border + col) * box_size
                y = (border + row) * box_size
                if drawer_kind == "circle":
                    cx = x + radius
                    cy = y + radius
                    parts.append(
                        f'<circle cx="{cx}" cy="{cy}" r="{radius}"/>'
                    )
                elif drawer_kind == "rounded":
                    parts.append(
                        f'<rect x="{x}" y="{y}" width="{box_size}" '
                        f'height="{box_size}" rx="{radius}" ry="{radius}"/>'
                    )
                elif drawer_kind == "gapped_square":
                    gx = x + inset
                    gy = y + inset
                    gw = box_size - 2 * inset
                    gh = box_size - 2 * inset
                    parts.append(
                        f'<rect x="{gx}" y="{gy}" width="{gw}" height="{gh}"/>'
                    )
                else:  # square (default)
                    parts.append(
                        f'<rect x="{x}" y="{y}" width="{box_size}" '
                        f'height="{box_size}"/>'
                    )
    parts.append("</g>")
    return "".join(parts)


def _build_svg_color_def(
    spec: dict | None,
    qr_w: int,
    qr_h: int,
) -> tuple[str, str]:
    """Return ``(defs_markup, fill_attr)`` for the on-module ``<g>``.

    The default template (or ``spec=None``) produces ``rgb(0, 0, 0)``
    and an empty defs string. Solid masks use the template's
    ``front_color`` directly. Gradient masks emit a ``<linearGradient>``
    or ``<radialGradient>`` inside ``<defs>`` and return ``url(#qr-fill)``
    so the on-module ``<g>`` references it.

    ``qr_w`` / ``qr_h`` are the QR pattern's pixel width and height
    (excluding the optional label band): the gradient stops are bounded
    to the QR area so the PIL render's appearance is preserved as
    closely as SVG allows.
    """
    if spec is None:
        return "", _svg_color_attr((0, 0, 0))

    kind = spec.get("color_mask_kind")
    if kind == "solid":
        return "", _svg_color_attr(spec["front_color"])

    grad_id = "qr-fill"
    if kind == "horizontal_gradient":
        left = spec["left_color"]
        right = spec["right_color"]
        defs = (
            f'<defs><linearGradient id="{grad_id}" '
            f'gradientUnits="userSpaceOnUse" '
            f'x1="0" y1="0" x2="{qr_w}" y2="0">'
            f'<stop offset="0%" stop-color="{_svg_color_attr(left)}"/>'
            f'<stop offset="100%" stop-color="{_svg_color_attr(right)}"/>'
            "</linearGradient></defs>"
        )
        return defs, f"url(#{grad_id})"
    if kind == "vertical_gradient":
        top = spec["top_color"]
        bottom = spec["bottom_color"]
        defs = (
            f'<defs><linearGradient id="{grad_id}" '
            f'gradientUnits="userSpaceOnUse" '
            f'x1="0" y1="0" x2="0" y2="{qr_h}">'
            f'<stop offset="0%" stop-color="{_svg_color_attr(top)}"/>'
            f'<stop offset="100%" stop-color="{_svg_color_attr(bottom)}"/>'
            "</linearGradient></defs>"
        )
        return defs, f"url(#{grad_id})"
    if kind in {"radial_gradient", "square_gradient"}:
        center = spec["center_color"]
        edge = spec["edge_color"]
        # Radius is bounded to half the QR pattern's width (NOT the
        # full canvas including any label band) so the gradient stays
        # centred on the QR and does not bleed into the band.
        radius = qr_w / 2.0
        cx = qr_w / 2.0
        cy = qr_h / 2.0
        defs = (
            f'<defs><radialGradient id="{grad_id}" '
            f'gradientUnits="userSpaceOnUse" '
            f'cx="{cx}" cy="{cy}" r="{radius}">'
            f'<stop offset="0%" stop-color="{_svg_color_attr(center)}"/>'
            f'<stop offset="100%" stop-color="{_svg_color_attr(edge)}"/>'
            "</radialGradient></defs>"
        )
        return defs, f"url(#{grad_id})"
    raise ValueError(f"unknown color_mask_kind: {kind!r}")


def generate_qr_svg(
    data: str,
    label: str | None = None,
    box_size: int = 10,
    border: int = 4,
    template_id: str | None = None,
    logo: Image.Image | None = None,
) -> str:
    """Render ``data`` as a QR code and return an SVG document string.

    Mirrors the contract of :func:`generate_qr` but emits SVG markup
    instead of a PIL image. The QR pattern is fully vector (each
    on-module is an SVG primitive) so the result stays sharp at any
    zoom level. The output has a transparent background by design: no
    ``<rect>`` covers the canvas under the modules.

    Two raster trade-offs are deliberate:

    * Embedded logos are encoded as base64 PNG data URIs and pasted
      via ``<image>``. The QR pattern around the logo stays vector,
      which is what users actually complain about pixelating; the
      logo region is small (~22% of the QR width) and acceptable.
      The ``href`` attribute (modern SVG2) is used rather than the
      legacy ``xlink:href`` so the root ``<svg>`` does not need the
      ``xmlns:xlink`` namespace declaration. Modern browsers accept
      ``href`` directly; very old SVG viewers may need ``xlink:href``,
      but we accept that compatibility floor in exchange for a
      cleaner root element.
    * Label text is referenced by family name (``Plus Jakarta Sans``)
      with a sans-serif fallback rather than embedded as a 130 KB TTF
      data URL. A batch ZIP of 100 SVGs would be hundreds of KB
      heavier with embedded fonts; system fallback is acceptable for
      label glyphs, and the QR pattern (the part the user complained
      about) is vector regardless of the font choice.

    Layout matches :func:`generate_qr`:

    * No label, no logo: bare QR.
    * Label only: centred badge on a white rounded-square pad in the
      QR's centre region (same geometry as the PIL render).
    * Logo only: centred logo on the same pad.
    * Label and logo: logo in the centre, label drawn in a white band
      directly under the QR pattern.

    Error correction is bumped to ``ERROR_CORRECT_H`` whenever a logo
    or centre-label is present, mirroring :func:`generate_qr`.
    """
    centre_label = label is not None and logo is None
    qr = _build_qr_for_render_plan(
        data, template_id, logo, label, box_size, border,
    )
    modules = qr.modules
    if modules is None:  # pragma: no cover - defensive (qr.make sets it)
        raise RuntimeError("qrcode.QRCode.make did not populate modules")
    modules_count = qr.modules_count

    spec_id = template_id if template_id is not None else "default"
    template = get_template(spec_id)
    spec = template["spec"]
    is_default = _is_default_template(template_id)

    drawer_kind = spec["module_drawer_kind"]
    qr_side = (modules_count + 2 * border) * box_size
    qr_w = qr_side
    qr_h = qr_side

    # Band-below layout extends the canvas height. Use the same maths
    # as the PIL render so the SVG and PNG layouts stay visually
    # aligned even though the SVG label is rendered by the browser.
    band_h = 0
    pad_y = 0
    font_size = 0
    if label is not None and logo is not None:
        font_size = max(14, qr_h * 12 // 100)
        pad_y = max(box_size, font_size // 2)
        band_h = font_size + 2 * pad_y
    canvas_w = qr_w
    canvas_h = qr_h + band_h

    # Fill colour and gradient defs for the on-modules <g>.
    fg_color: tuple[int, int, int]
    if is_default:
        defs_markup = ""
        fill_attr = _svg_color_attr((0, 0, 0))
        fg_color = (0, 0, 0)
    else:
        defs_markup, fill_attr = _build_svg_color_def(spec, qr_w, qr_h)
        fg_color = _label_color_from_spec(spec)

    parts: list[str] = []
    parts.append('<?xml version="1.0" encoding="UTF-8" standalone="no"?>')
    # Use the modern ``href`` attribute on <image> (no xmlns:xlink
    # needed); see docstring for the compatibility rationale.
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {canvas_w} {canvas_h}" '
        f'width="{canvas_w}" height="{canvas_h}" '
        f'shape-rendering="crispEdges">'
    )
    if defs_markup:
        parts.append(defs_markup)
    parts.append(
        _render_modules_svg(modules, border, box_size, drawer_kind, fill_attr)
    )

    # Centre badge / logo overlay.
    if logo is not None:
        # Overlay the padded logo as a base64 PNG. The pad is the same
        # white rounded-square the PIL render builds, so the SVG and
        # PNG outputs match visually.
        padded = _pad_logo(logo, LOGO_WORK_SIZE)
        png_buf = io.BytesIO()
        padded.save(png_buf, format="PNG")
        encoded = base64.b64encode(png_buf.getvalue()).decode("ascii")
        ratio = 0.22
        side = qr_w * ratio
        lx = (qr_w - side) / 2.0
        ly = (qr_h - side) / 2.0
        parts.append(
            f'<image href="data:image/png;base64,{encoded}" '
            f'x="{lx}" y="{ly}" width="{side}" height="{side}" '
            f'preserveAspectRatio="xMidYMid meet"/>'
        )
    elif centre_label:
        # Centre badge: rounded white pad + centred label glyph.
        ratio = 0.22
        badge_side = qr_w * ratio
        bx = (qr_w - badge_side) / 2.0
        by = (qr_h - badge_side) / 2.0
        radius = max(2.0, badge_side / 8.0)
        parts.append(
            f'<rect x="{bx}" y="{by}" width="{badge_side}" height="{badge_side}" '
            f'rx="{radius}" ry="{radius}" fill="rgb(255, 255, 255)"/>'
        )
        glyph_color = (0, 0, 0) if is_default else fg_color
        glyph_size = _autofit_centre_badge_font_size(label, badge_side)
        # SVG uses the badge's pixel side as the autofit target so the
        # rendered glyph has the same proportions as the PIL render.
        cx = qr_w / 2.0
        cy = qr_h / 2.0
        escaped = xml.sax.saxutils.escape(label)
        parts.append(
            f'<text x="{cx}" y="{cy}" '
            f'font-family="Plus Jakarta Sans, sans-serif" font-weight="700" '
            f'font-size="{glyph_size}" '
            f'fill="{_svg_color_attr(glyph_color)}" '
            f'text-anchor="middle" dominant-baseline="central">'
            f"{escaped}</text>"
        )

    # Band-below layout: white band rect + centred label.
    if label is not None and logo is not None:
        parts.append(
            f'<rect x="0" y="{qr_h}" width="{qr_w}" height="{band_h}" '
            f'fill="rgb(255, 255, 255)"/>'
        )
        text_y = qr_h + band_h / 2.0
        escaped = xml.sax.saxutils.escape(label)
        parts.append(
            f'<text x="{qr_w / 2.0}" y="{text_y}" '
            f'font-family="Plus Jakarta Sans, sans-serif" font-weight="700" '
            f'font-size="{font_size}" '
            f'fill="{_svg_color_attr(fg_color)}" '
            f'text-anchor="middle" dominant-baseline="central">'
            f"{escaped}</text>"
        )

    parts.append("</svg>")
    return "".join(parts)


def generate_sequence_svg(
    start: int,
    count: int | None = None,
    end: int | None = None,
    data_template: str = "{n}",
    label_template: str | None = "{n}",
    padding: int = 0,
    prefix: str = "",
    box_size: int = 10,
    border: int = 4,
    template_id: str | None = None,
    logo: Image.Image | None = None,
) -> Iterator[tuple[str, str]]:
    """Yield ``(filename, svg_string)`` pairs for a sequential range.

    Mirrors :func:`generate_sequence` but emits SVG strings instead of
    PIL images. The filename extension is ``.svg``. ``data_template``
    and ``label_template`` follow the same ``str.replace`` semantics:
    ``{n}`` is substituted with the padded numeric string, all other
    text is literal.
    """
    numbers = compute_range(start, count=count, end=end, padding=padding)
    for n in numbers:
        data = data_template.replace("{n}", n)
        label = label_template.replace("{n}", n) if label_template is not None else None
        svg = generate_qr_svg(
            data,
            label=label,
            box_size=box_size,
            border=border,
            template_id=template_id,
            logo=logo,
        )
        yield f"{prefix}{n}.svg", svg


def generate_sequence_render_plan(
    start: int,
    count: int | None = None,
    end: int | None = None,
    data_template: str = "{n}",
    label_template: str | None = "{n}",
    padding: int = 0,
    prefix: str = "",
    box_size: int = 10,
    border: int = 4,
    template_id: str | None = None,
    logo: Image.Image | None = None,
) -> Iterator[tuple[str, dict]]:
    """Yield ``(filename_no_ext, render_plan)`` pairs for a sequential range.

    A render plan is a dict carrying everything :func:`_pack_pdf_vector`
    needs to draw a single QR using reportlab primitives directly:

    * ``modules``: NxN bool matrix (from ``qr.modules``) of on-module flags.
    * ``modules_count``: side length in modules.
    * ``border``: quiet-zone width in modules.
    * ``box_size``: pixel size per module (used for the SVG layout
      contract; the PDF path scales modules into the page cell so
      this is informational on the PDF side).
    * ``spec``: resolved template spec dict (always populated, with
      the ``default`` template's spec when no template is supplied).
    * ``is_default``: True when no template / the ``default`` template
      was selected, so the PDF path uses pure black for module fill
      and label text.
    * ``logo``: optional pre-padded RGBA :class:`PIL.Image.Image`.
    * ``label``: optional label string.
    * ``centre_label``: True when ``label`` is set without a ``logo``.
    * ``label_color``: representative RGB tuple for the label.

    Iterated lazily so peak memory stays at one render plan at a time
    (one ``qrcode.QRCode`` instance plus, optionally, one padded logo).
    """
    numbers = compute_range(start, count=count, end=end, padding=padding)
    spec_id = template_id if template_id is not None else "default"
    template = get_template(spec_id)
    spec = template["spec"]
    is_default = _is_default_template(template_id)
    if is_default:
        label_color: tuple[int, int, int] = (0, 0, 0)
    else:
        label_color = _label_color_from_spec(spec)

    # The padded logo is the same for every item in the sequence; build
    # it once and reuse so we do not pay the LANCZOS resize per item.
    padded_logo = _pad_logo(logo, LOGO_WORK_SIZE) if logo is not None else None

    for n in numbers:
        data = data_template.replace("{n}", n)
        label = label_template.replace("{n}", n) if label_template is not None else None
        qr = _build_qr_for_render_plan(
            data, template_id, logo, label, box_size, border,
        )
        plan = {
            "modules": qr.modules,
            "modules_count": qr.modules_count,
            "border": border,
            "box_size": box_size,
            "spec": spec,
            "is_default": is_default,
            "logo": padded_logo,
            "label": label,
            "centre_label": label is not None and logo is None,
            "label_color": label_color,
        }
        yield f"{prefix}{n}", plan


def _pack_zip_svg(items: Iterable[tuple[str, str]]) -> Iterator[tuple]:
    """Pack ``(filename, svg_string)`` pairs into a ZIP archive in memory.

    Mirrors :func:`_pack_zip` but writes SVG XML strings (UTF-8 encoded)
    rather than PNG bytes. Yields ``("progress", index, filename)``
    after each entry and ``("result", zip_bytes)`` exactly once at the
    end. Items are pulled lazily so peak memory stays at one SVG
    string plus the growing archive bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for index, (filename, svg) in enumerate(items):
            zf.writestr(filename, svg.encode("utf-8"))
            yield ("progress", index, filename)
    yield ("result", buffer.getvalue())


def _pack_pdf_vector(
    items: Iterable[tuple[str, dict]],
    page_size: tuple[float, float] = LETTER,
    cols: int = 3,
    rows: int = 4,
    margin_pt: float = 36.0,
) -> Iterator[tuple]:
    """Pack a sequence of render plans onto a PDF using vector primitives.

    Each on-module is drawn as a reportlab ``rect`` / ``circle`` /
    ``roundRect`` directly into the canvas's content stream so the
    resulting PDF stays vector and zoom-clean. No raster image XObject
    is added unless the render plan carries a ``logo`` (the small
    centre region is embedded as a raster trade-off, matching the SVG
    path).

    Gradient masks are simplified to a single representative colour
    (``label_color``) on the PDF path. Full vector gradients via
    ``c.linearGradient`` / ``c.radialGradient`` are possible but add
    complexity; the user's primary complaint is pixelation, not
    colour fidelity at zoom, and the simplification keeps the PDF
    body small. The SVG path renders gradients fully via
    ``<linearGradient>`` / ``<radialGradient>`` defs.

    Yields ``("progress", index, filename)`` after each render and
    ``("result", pdf_bytes)`` exactly once at the end.
    """
    if cols <= 0 or rows <= 0:
        raise ValueError("cols and rows must both be > 0")

    buffer = io.BytesIO()
    page_w, page_h = page_size
    usable_w = page_w - 2 * margin_pt
    usable_h = page_h - 2 * margin_pt
    cell_w = usable_w / cols
    cell_h = usable_h / rows

    # Disable page compression so the raw rectangle operator (``re``) and
    # the QR module geometry remain inspectable in the PDF body. The
    # vector contract test (``test_pack_pdf_vector_no_image_xobjects_for_no_logo_batch``)
    # asserts on the substring ``b' re'`` to confirm rectangle drawing
    # actually happened; with default compression the content stream is
    # FlateDecode'd and the test would have to re-implement decoding.
    # The compression saving is a few KB per page on a vector QR; the
    # readability win for tests is worth the trade-off.
    c = pdf_canvas.Canvas(buffer, pagesize=page_size, pageCompression=0)
    per_page = cols * rows
    for index, (filename, plan) in enumerate(items):
        slot = index % per_page
        if index > 0 and slot == 0:
            c.showPage()

        col_idx = slot % cols
        row_idx = slot // cols

        modules = plan["modules"]
        modules_count = plan["modules_count"]
        border = plan["border"]
        spec = plan["spec"]
        is_default = plan["is_default"]
        logo = plan["logo"]
        label = plan["label"]
        centre_label = plan["centre_label"]
        label_color = plan["label_color"]

        # Compute the QR pixel-side and band layout so the cell-fit
        # scaling preserves the SVG/PNG layout proportions.
        qr_modules_side = modules_count + 2 * border
        # Pick a working "module unit" of 1 PDF point per module before
        # scaling: the actual scale is determined by cell-fit below.
        unit = 1.0
        qr_side = qr_modules_side * unit
        # Band height (band-below layout when label + logo) follows the
        # PIL render's proportions: ~12% of the QR pixel height plus
        # 2 * pad_y where pad_y >= unit.
        band_h = 0.0
        font_size_px = 0.0
        if label is not None and logo is not None:
            font_size_px = max(14, int(qr_side * 12 // 100))
            pad_y = max(unit, font_size_px / 2.0)
            band_h = font_size_px + 2 * pad_y
        total_w = qr_side
        total_h = qr_side + band_h

        scale = min(cell_w / total_w, cell_h / total_h)
        draw_w = total_w * scale
        draw_h = total_h * scale

        cell_left = margin_pt + col_idx * cell_w
        # Reportlab origin is bottom-left; we want row 0 at the top.
        cell_bottom = page_h - margin_pt - (row_idx + 1) * cell_h
        x0 = cell_left + (cell_w - draw_w) / 2
        # The QR's top-left pixel sits at the top of the cell after
        # centring; convert to PDF coordinates (bottom-left origin).
        y_top = cell_bottom + (cell_h - draw_h) / 2 + draw_h
        # The QR pattern occupies the top of the cell; the band sits
        # below it. Compute the QR's bottom-left in PDF coords.
        qr_pdf_w = qr_side * scale
        qr_pdf_h = qr_side * scale
        qr_y0 = y_top - qr_pdf_h
        band_pdf_h = band_h * scale

        # Module fill colour: representative stop for non-default templates,
        # pure black for default. Gradient masks render as a single
        # representative colour on the PDF path; see docstring.
        if is_default:
            module_color = (0, 0, 0)
        else:
            module_color = label_color
        c.setFillColorRGB(
            module_color[0] / 255.0,
            module_color[1] / 255.0,
            module_color[2] / 255.0,
        )
        c.setStrokeColorRGB(0, 0, 0)

        drawer_kind = spec["module_drawer_kind"]
        # Per-module pixel size in PDF points after scaling.
        module_pt = unit * scale
        radius_pt = module_pt / 2.0
        gap_inset = module_pt * 0.10 if drawer_kind == "gapped_square" else 0.0

        def _module_xy(row: int, col: int) -> tuple[float, float]:
            x = x0 + (border + col) * module_pt
            # PDF y origin bottom-left: row 0 sits at the TOP of the QR.
            y = qr_y0 + qr_pdf_h - (border + row + 1) * module_pt
            return x, y

        if drawer_kind == "vertical_bars":
            for row_start, row_end, col in _merge_runs_vertical(modules):
                x = x0 + (border + col) * module_pt
                run_h = (row_end - row_start) * module_pt
                # Top-most module of the run is row_start; convert.
                y = qr_y0 + qr_pdf_h - (border + row_end) * module_pt
                c.rect(x, y, module_pt, run_h, fill=1, stroke=0)
        elif drawer_kind == "horizontal_bars":
            for row, col_start, col_end in _merge_runs_horizontal(modules):
                x = x0 + (border + col_start) * module_pt
                y = qr_y0 + qr_pdf_h - (border + row + 1) * module_pt
                run_w = (col_end - col_start) * module_pt
                c.rect(x, y, run_w, module_pt, fill=1, stroke=0)
        else:
            for row, cells in enumerate(modules):
                for col_idx2, on in enumerate(cells):
                    if not on:
                        continue
                    x, y = _module_xy(row, col_idx2)
                    if drawer_kind == "circle":
                        c.circle(
                            x + radius_pt,
                            y + radius_pt,
                            radius_pt,
                            fill=1,
                            stroke=0,
                        )
                    elif drawer_kind == "rounded":
                        c.roundRect(
                            x, y, module_pt, module_pt, radius_pt,
                            fill=1, stroke=0,
                        )
                    elif drawer_kind == "gapped_square":
                        c.rect(
                            x + gap_inset,
                            y + gap_inset,
                            module_pt - 2 * gap_inset,
                            module_pt - 2 * gap_inset,
                            fill=1,
                            stroke=0,
                        )
                    else:  # square (default)
                        c.rect(x, y, module_pt, module_pt, fill=1, stroke=0)

        # Logo (raster centre embed). The pad is built once per
        # sequence in :func:`generate_sequence_render_plan` and reused
        # here.
        if logo is not None:
            ratio = 0.22
            side = qr_pdf_w * ratio
            lx = x0 + (qr_pdf_w - side) / 2.0
            ly = qr_y0 + (qr_pdf_h - side) / 2.0
            c.drawImage(
                ImageReader(logo),
                lx,
                ly,
                width=side,
                height=side,
                preserveAspectRatio=True,
                mask="auto",
            )

        # Centre badge (label without logo): white rounded pad +
        # centred glyph.
        if centre_label and label is not None:
            ratio = 0.22
            badge_side = qr_pdf_w * ratio
            bx = x0 + (qr_pdf_w - badge_side) / 2.0
            by = qr_y0 + (qr_pdf_h - badge_side) / 2.0
            c.setFillColorRGB(1, 1, 1)
            c.roundRect(
                bx, by, badge_side, badge_side, badge_side / 8.0,
                fill=1, stroke=0,
            )
            # Glyph colour follows the template (or pure black for default).
            if is_default:
                glyph_color = (0, 0, 0)
            else:
                glyph_color = label_color
            c.setFillColorRGB(
                glyph_color[0] / 255.0,
                glyph_color[1] / 255.0,
                glyph_color[2] / 255.0,
            )
            # Autofit on the badge's pixel side; reuse the SVG/PIL
            # autofit so the rendered glyph proportions match.
            font_size_pt = _autofit_centre_badge_font_size(label, badge_side)
            try:
                c.setFont(_PDF_LABEL_FONT_NAME, font_size_pt)
            except KeyError:  # pragma: no cover - registration failed at import
                c.setFont("Helvetica-Bold", font_size_pt)
            text_x = bx + badge_side / 2.0
            # ``drawCentredString`` aligns at the text baseline, so
            # offset the y to roughly the badge's vertical centre.
            text_y = by + badge_side / 2.0 - font_size_pt * 0.35
            c.drawCentredString(text_x, text_y, label)

        # Band-below layout: white band + centred label glyph.
        if label is not None and logo is not None:
            band_y = qr_y0 - band_pdf_h
            c.setFillColorRGB(1, 1, 1)
            c.rect(x0, band_y, qr_pdf_w, band_pdf_h, fill=1, stroke=0)
            if is_default:
                glyph_color = (0, 0, 0)
            else:
                glyph_color = label_color
            c.setFillColorRGB(
                glyph_color[0] / 255.0,
                glyph_color[1] / 255.0,
                glyph_color[2] / 255.0,
            )
            font_size_pt = font_size_px * scale
            try:
                c.setFont(_PDF_LABEL_FONT_NAME, font_size_pt)
            except KeyError:  # pragma: no cover - registration failed at import
                c.setFont("Helvetica-Bold", font_size_pt)
            text_x = x0 + qr_pdf_w / 2.0
            text_y = band_y + band_pdf_h / 2.0 - font_size_pt * 0.35
            c.drawCentredString(text_x, text_y, label)

        yield ("progress", index, filename)

    c.showPage()
    c.save()
    yield ("result", buffer.getvalue())


def iter_batch_vector_with_progress(
    items: Iterable,
    fmt: str,
) -> Iterator[tuple]:
    """Vector-batch counterpart to :func:`iter_batch_with_progress`.

    Routes ``fmt='zip_svg'`` to :func:`_pack_zip_svg` (SVG entries) and
    ``fmt='pdf'`` to :func:`_pack_pdf_vector` (vector PDF). The HTTP
    layer is responsible for handing this iterator the right kind of
    items: SVG strings (filename, svg) for zip_svg, render plans
    (filename_no_ext, plan) for pdf. Anything else raises
    :class:`ValueError`.

    The event shape is unchanged from the raster
    :func:`iter_batch_with_progress`: ``("progress", index, name)`` per
    item plus a single terminal ``("result", payload_bytes)``.
    """
    if fmt == "zip_svg":
        yield from _pack_zip_svg(items)
    elif fmt == "pdf":
        yield from _pack_pdf_vector(items)
    else:
        raise ValueError("fmt must be 'zip_svg' or 'pdf'")
