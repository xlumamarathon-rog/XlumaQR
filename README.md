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
