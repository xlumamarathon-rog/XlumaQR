# XlumaQR

A small Flask web app and HTTP API for generating QR codes, either one
at a time or as a sequential batch packed into a ZIP or laid out on a
PDF. The pure-Python core lives in `qr_generator.py` (no Flask imports)
and the HTTP layer in `app.py` is a thin wrapper around it.

## Quickstart

```bash
python -m pip install -r requirements.txt
python app.py
```

Then open [http://127.0.0.1:5000](http://127.0.0.1:5000) in a browser.

> **Deployment note:** XlumaQR is a localhost developer tool. The bundled
> `app.py` binds to `127.0.0.1:5000` on purpose and ships with no
> authentication or rate limiting. Do not expose it on the public
> internet via `--host=0.0.0.0`, a reverse proxy, or a tunnel without
> putting auth in front of it; the batch endpoint will happily render
> thousands of QR codes for any caller that can reach the port.

The page has two tabs: **Single QR** and **Sequential Batch**.

## Single QR

Enter the data to encode (a URL, an ID, anything UTF-8) and an optional
label. The generated PNG is shown inline in the page.

Form fields:

- `data` (required): the payload to encode.
- `label` (optional): text printed under the QR. Empty means no label.
- `box_size` (default 10): pixel size of each QR module.
- `border` (default 4): quiet-zone width in modules.

## Sequential Batch

The Sequential Batch tab generates a numeric range of QR codes and lets
you download them as a ZIP (one PNG per code) or as a PDF (grid layout).

### The user's canonical example: start=101, count=100

> Enter `start=101` and `count=100` to generate 100 QR codes numbered
> 101 through 200.

`count` is the number of codes to generate, **not** the last number, so
with `start=101` and `count=100` the last code is **200**, not 201. If
you want the inclusive range 101..201 (which is 101 codes), either set
`count=101` or switch to the **End** mode and set `end=201`.

Form fields:

- `start` (required, integer): first number in the range.
- `count` (integer): number of codes to generate. Mutually exclusive
  with `end`. Must be > 0.
- `end` (integer): final number in the range, inclusive. Mutually
  exclusive with `count`.
- `padding` (default 0): minimum width of the numeric string. With
  `padding=3` the numbers become `001`, `002`, ...
- `prefix` (default empty): filename prefix. With `prefix=qr_` the
  archive contains `qr_101.png`, `qr_102.png`, ... To keep ZIP entries
  safe, the prefix is restricted to letters, digits, `_`, `-`, `.`, and
  spaces, and may not start with `.` (so `inv.001-`, `tickets.`, and
  `2026 batch ` are accepted; `../`, `.hidden`, and `a/b` are rejected
  with a 400).
- `data_template` (default `{n}`): template for the encoded data.
  `{n}` is replaced by the padded numeric string.
- `label_template` (default `{n}`): template for the printed label.
  Empty means no label.
- `box_size` (default 10), `border` (default 4): same as Single QR.
- `format` (`zip` or `pdf`, default `zip`): download format.

The page also shows a live range hint as you type, e.g.
`Will generate 100 QR codes: 101 -> 200`.

## HTTP API

### `POST /api/qr/single`

Returns `image/png`.

```bash
curl -fsS -X POST \
  -F data=hello \
  -F label=42 \
  http://127.0.0.1:5000/api/qr/single \
  -o qr.png
```

### `POST /api/qr/batch`

Returns `application/zip` or `application/pdf` depending on the
`format` field. The `Content-Disposition` header carries a filename of
the form `qr_batch_<start>_<lastN>.<ext>`.

The user's example as a ZIP (101..200, 100 entries):

```bash
curl -fsS -X POST \
  -F start=101 \
  -F count=100 \
  http://127.0.0.1:5000/api/qr/batch \
  -o qr.zip
unzip -l qr.zip   # 100 entries: 101.png ... 200.png
```

The same range as a PDF:

```bash
curl -fsS -X POST \
  -F start=101 \
  -F count=100 \
  -F format=pdf \
  http://127.0.0.1:5000/api/qr/batch \
  -o qr.pdf
```

A custom prefix and padding:

```bash
curl -fsS -X POST \
  -F start=1 \
  -F count=5 \
  -F padding=3 \
  -F prefix=ticket_ \
  http://127.0.0.1:5000/api/qr/batch \
  -o tickets.zip
# tickets.zip -> ticket_001.png ... ticket_005.png
```

### `POST /api/qr/batch/stream`

A streaming variant of the batch endpoint that powers the live progress
bar in the browser UI. Form fields, validation rules, and 400 responses
are identical to `/api/qr/batch`. Once validation passes the response is
`application/x-ndjson` (HTTP 200) and the body is one JSON object per
line:

- `{"event": "start", "total": N, "format": "zip"|"pdf",
   "first": "...", "last": "..."}` once at the top.
- `{"event": "progress", "index": i, "total": N, "name": "<filename>"}`
  once per generated QR. `index` runs 0..N-1 monotonically and matches
  the entry name that will appear in the final archive.
- `{"event": "result", "filename": "...",
   "mimetype": "application/zip"|"application/pdf",
   "data_base64": "..."}` once at the end carrying the packed bytes.
  The body is base64-encoded so the stream stays JSON-only and trivial
  to parse line-by-line.
- `{"event": "error", "error": "..."}` instead of `result` if encoding
  fails mid-stream (e.g. a substituted template overflows QR capacity).
  HTTP status remains 200 once streaming has begun; the failure detail
  is in the event payload.

The response also carries `Cache-Control: no-cache` and
`X-Accel-Buffering: no` to discourage proxy buffering.

> **Deployment caveat:** `X-Accel-Buffering: no` is an nginx-specific
> hint. Some serverless platforms (notably Vercel's default
> Lambda-backed Python runtime) buffer the entire response body before
> sending any bytes to the client; on those platforms the bar will
> jump from 0% to 100% in a single tick when the whole NDJSON arrives
> at once at the end. Correctness does not depend on incremental
> delivery: the final `result` event always lands and the download
> still triggers, but the live-progress experience is a localhost /
> properly-streaming-deployment feature, not a guarantee on every
> hosting target. On Vercel specifically, opting the Python function
> into Fluid Compute (or any deployment that exposes Python response
> streaming, e.g. via `vercel.json` `streaming: true`) is what flushes
> chunks incrementally; the JS-only Edge runtime does not run this
> Flask app and is not a workaround.

> **Response size limit:** the terminal `result` event embeds the
> packed ZIP/PDF as base64 inside the JSON line, which inflates the
> payload by roughly 33%. Some serverless platforms enforce a
> per-response body cap (Vercel Hobby is currently 4.5 MB) which a
> large batch can hit well before the `MAX_RANGE_SIZE` limit on the
> input. If you need very large batches over a hosted streaming
> endpoint, prefer `POST /api/qr/batch` (binary response, no base64
> overhead) or run on a deployment without the cap.

The synchronous `POST /api/qr/batch` remains the simpler choice for
scripted/curl usage where you just want one ZIP/PDF blob in one
response. Use `/api/qr/batch/stream` when you want a real percentage
progress indicator while a large batch is rendering.

```bash
curl -N -s -X POST \
  -F start=1 -F count=3 \
  http://127.0.0.1:5000/api/qr/batch/stream | head
# {"event": "start", "total": 3, "format": "zip", "first": "1", "last": "3"}
# {"event": "progress", "index": 0, "total": 3, "name": "1.png"}
# {"event": "progress", "index": 1, "total": 3, "name": "2.png"}
# {"event": "progress", "index": 2, "total": 3, "name": "3.png"}
# {"event": "result", "filename": "qr_batch_1_3.zip", ...}
```

### Custom QR designs

`POST /api/qr/single`, `POST /api/qr/batch`, and `POST /api/qr/batch/stream`
all accept two optional fields that style the rendered QR codes:

- `template_id` (default `default`): the slug of a built-in design
  template. The literal value `default` (or an empty / missing field)
  takes the legacy plain-black-on-white render path byte-for-byte. Any
  other id is validated against the template registry; an unknown id
  returns HTTP 400 with `{"error": "unknown template_id: ..."}`. Browse
  the available ids via `GET /api/qr/templates` (see below).
- `logo` (file field, optional): a PNG or JPEG image embedded at the
  centre of every QR. The HTTP layer enforces three validation limits
  before the bytes ever reach the encoder; failures all return a clean
  HTTP 400 with a JSON `error` body, never a 500:
  - byte cap: `MAX_LOGO_BYTES = 2 * 1024 * 1024` (2 MB). Hard reject.
  - dimension policy: uploads above `MAX_LOGO_DIMENSION = 1024` on
    either axis are auto-resized down to fit, preserving aspect ratio,
    so users can drop a phone-camera screenshot into the form without
    thinking about pixel sizes. Only uploads above
    `LOGO_HARD_MAX_DIMENSION = 4096` per side are rejected outright,
    as a hard reject for OOM safety.
  - format cap: only PNG and JPEG are accepted (mime sniffing via
    PIL `Image.verify` plus `Image.format`, so a renamed `.txt`
    pretending to be `image/png` is caught). The format check runs
    before the auto-resize step because PIL's `Image.thumbnail`
    clears `image.format`.

  The hard dimension ceiling is what bounds the decoded bitmap. A
  pathologically compressible PNG (a single-colour 12000x12000 image
  is well under the 2 MB byte cap on the wire but would decode to
  ~432 MB of RGB pixels) is rejected on its declared header
  dimensions *before* PIL allocates the bitmap, so a
  decompression-bomb upload cannot exhaust the Lambda's memory on the
  way to the dimension check. The auto-resize step runs *after*
  `Image.load()` so the worst-case decoded memory for any upload that
  passes validation is roughly `LOGO_HARD_MAX_DIMENSION**2 * 4` bytes
  (~64 MB at the 4096 ceiling with an RGBA decode); the byte cap and
  PIL's `DecompressionBombError` (raised above ~178 MP from inside
  `Image.open` itself) remain hard rejects regardless.

  The 4096 ceiling is chosen deliberately to give users headroom for
  phone-camera screenshots (modern phones routinely produce
  ~4032x3024 JPEGs). Three coincident uploads sit at ~192 MB of
  decoded RGBA on top of the Flask/Pillow runtime, which fits inside
  the 1 GB Lambda tier we target on Vercel but lands close to the
  limit on a 256 MB tier. If you are deploying onto a tighter
  memory tier, lower `LOGO_HARD_MAX_DIMENSION` (e.g. to 2048, which
  halves the worst-case peak twice to ~16 MB) at the cost of
  rejecting phone-camera screenshots above 2048 per side.

When a logo is supplied, the encoder is bumped to error-correction
level H (15-30% recovery, vs M's 15%) so the QR stays scannable with
the centre region partially obscured. The trade-off is that QR version
40's binary capacity at H is only roughly 1273 bytes versus M's roughly
2300 bytes, so a payload that fits without a logo may overflow with
one. Capacity overflow surfaces the same way as any other encoder
failure: a clean 400 with `{"error": "data could not be encoded: ..."}`
on the synchronous endpoints, and a terminal `{"event": "error", ...}`
NDJSON line on `POST /api/qr/batch/stream`.

#### `GET /api/qr/templates`

Returns the JSON listing of built-in design templates as
`{"templates": [<entry>, ...]}` where each entry has `id`, `name`,
`category`, and `spec` keys. The response carries
`Cache-Control: public, max-age=300` so warm browsers skip the round
trip on subsequent page loads inside the cache window.

```bash
curl -fsS http://127.0.0.1:5000/api/qr/templates | python -m json.tool | head
# {
#     "templates": [
#         {"id": "default", "name": "Default (plain black & white)", ...},
#         ...
#     ]
# }
```

The registry currently spans the categories `default`, `marathon`,
`running`, `duathlon`, `triathlon`, `cycling`, `swimming`, `business`,
`event`, `wifi`, `social`, and `personal`, with at least three
templates per non-default category.

#### `GET /api/qr/templates/<template_id>/preview`

Returns a small `image/png` thumbnail of `template_id` rendered with a
fixed short payload. The response carries
`Cache-Control: public, max-age=3600` and the rendered bytes are
cached in a per-process module-level dict, so a warm Lambda renders
each preview at most once. An unknown id returns
`{"error": "unknown template id"}` with HTTP 404.

```bash
curl -fsS http://127.0.0.1:5000/api/qr/templates/marathon-fire/preview \
  -o preview.png
file preview.png   # PNG image data ...
```

> **Deployment notes:** the preview cache is per-warm-instance, so a
> cold start on Vercel re-renders the gallery on first request and
> shares it across subsequent requests in the same instance. The cache
> is intentionally unbounded but every entry is a small thumbnail PNG
> (a few KB each) and the registry size is bounded by the `TEMPLATES`
> list, so the steady-state memory footprint is small. The logo bytes
> traverse `POST /api/qr/batch/stream` per request and the existing
> base64-inflation note above still applies to the terminal `result`
> event when a logo plus many entries push the packed ZIP/PDF closer
> to the per-response body cap.

### Error handling

Bad input (missing `start`, `count <= 0`, `end < start`, both `count`
and `end` provided, non-integer values, unknown `format`, etc.) returns
HTTP 400 with a JSON body:

```json
{ "error": "count must be > 0" }
```

## Run tests

```bash
python -m pytest -q
```

This runs both the core tests in `tests/test_qr_generator.py` and the
HTTP-layer tests in `tests/test_app.py`.
