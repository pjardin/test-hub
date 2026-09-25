"""Shipping labels: a Code 128 barcode (SVG) and a 4x6 in PDF, both drawn
here with no library -- the label a depot scanner reads, and the file a
printer takes.

Code 128, subset B (printable ASCII): a start symbol, one symbol per
character, a weighted modulo-103 checksum, a stop symbol. Every symbol is
three bars and three spaces, 11 modules wide in total (the stop is 13). The
sample test DEMO-024 carries its OWN decoder -- a scanner does not share code
with the printer -- so a wrong barcode is caught the way a depot would catch
it.
"""
import unicodedata
from html import escape

# bar/space widths of symbols 0..106, in modules (Code 128 standard table)
PATTERNS = (
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312",
    "132212", "221213", "221312", "231212", "112232", "122132", "122231", "113222",
    "123122", "123221", "223211", "221132", "221231", "213212", "223112", "312131",
    "311222", "321122", "321221", "312212", "322112", "322211", "212123", "212321",
    "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
    "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121",
    "313121", "211331", "231131", "213113", "213311", "213131", "311123", "311321",
    "331121", "312113", "312311", "332111", "314111", "221411", "431111", "111224",
    "111422", "121124", "121421", "141122", "141221", "112214", "112412", "122114",
    "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
    "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112",
    "421211", "212141", "214121", "412121", "111143", "111341", "131141", "114113",
    "114311", "411113", "411311", "113141", "114131", "311141", "411131", "211412",
    "211214", "211232", "2331112",
)
START_B, STOP = 104, 106
QUIET = 10                       # modules of white on each side


def symbols(text):
    """Code 128-B symbol values for `text`, start + data + checksum + stop."""
    values = []
    for ch in text:
        code = ord(ch)
        if not 32 <= code <= 126:
            raise ValueError(f"Code 128-B cannot encode {ch!r}")
        values.append(code - 32)
    check = (START_B + sum(i * v for i, v in enumerate(values, 1))) % 103
    return [START_B] + values + [check, STOP]


def widths(text):
    """Alternating bar/space widths in modules, starting and ending on a bar."""
    return [int(w) for v in symbols(text) for w in PATTERNS[v]]


def svg(text, module=2, height=64, element_id="label-barcode"):
    """The barcode as an SVG: one <rect> per bar, white quiet zones."""
    x = QUIET * module
    rects = []
    for i, w in enumerate(widths(text)):
        if i % 2 == 0:
            rects.append(f'<rect x="{x}" y="0" width="{w * module}" height="{height}"/>')
        x += w * module
    total = x + QUIET * module
    return (f'<svg id="{escape(element_id)}" class="barcode" xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {total} {height}" width="{total}" height="{height}" role="img" '
            f'aria-label="Barcode {escape(text)}" data-symbology="code128">'
            f'<rect x="0" y="0" width="{total}" height="{height}" fill="#fff"/>'
            f'<g fill="#000">{"".join(rects)}</g></svg>')


# --------------------------------------------------------------------------
# PDF: one 4 x 6 in page, standard fonts (nothing embedded), no compression
# --------------------------------------------------------------------------

PAGE_W, PAGE_H = 288, 432        # points: 4 x 6 inches


def _pdf_text(s):
    """A PDF literal string in WinAnsi (cp1252): accents that encoding lacks
    fall back to their base letter ("İstanbul" -> "Istanbul")."""
    out = []
    for ch in str(s):
        try:
            ch.encode("cp1252")
        except UnicodeEncodeError:
            ch = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode() or "?"
        out.append(ch)
    s = "".join(out).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return "(" + s + ")"


def pdf(fields):
    """A 4x6 label as PDF bytes. `fields`: id, code (ECO/STD/EXP), service,
    from, to, route, weight, pieces, created, customer."""
    ops = []

    def text(x, y, size, s, bold=False):
        ops.append(f"BT /{'F2' if bold else 'F1'} {size} Tf {x} {y} Td {_pdf_text(s)} Tj ET")

    def box(x, y, w, h, fill=False):
        ops.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re {'f' if fill else 'S'}")

    ops.append("1.2 w")
    box(8, 8, PAGE_W - 16, PAGE_H - 16)
    text(18, 398, 16, "ACME FREIGHT", bold=True)
    text(18, 384, 8, "Air + ground freight  -  demo label, not a real shipment")
    box(222, 372, 50, 44, fill=True)
    ops.append("1 g")                                       # white code on the black box
    text(230, 388, 16, fields["code"], bold=True)
    ops.append("0 g")
    text(18, 350, 8, "SHIP FROM", bold=True)
    text(18, 336, 11, fields["customer"])
    text(18, 322, 11, fields["from"])
    text(18, 292, 8, "SHIP TO", bold=True)
    text(18, 272, 15, fields["to"], bold=True)
    box(18, 248, PAGE_W - 36, 0.8, fill=True)
    text(18, 230, 9, f"Service: {fields['service']}")
    text(150, 230, 9, f"Route: {fields['route']}")
    text(18, 216, 9, f"Weight: {fields['weight']}")
    text(150, 216, 9, f"Pieces: {fields['pieces']}")
    text(18, 202, 9, f"Shipped: {fields['created']}")
    # the barcode, scaled to fit between the side margins
    bars = widths(fields["id"])
    modules = sum(bars) + 2 * QUIET
    unit = (PAGE_W - 36) / modules
    x, y0, h = 18 + QUIET * unit, 96, 84
    for i, w in enumerate(bars):
        if i % 2 == 0:
            box(x, y0, w * unit, h, fill=True)
        x += w * unit
    # centred under the bars: Helvetica-Bold digits and capitals average ~0.58 em
    text(round((PAGE_W - len(fields["id"]) * 14 * 0.58) / 2, 1), 76, 14, fields["id"], bold=True)
    text(18, 30, 7, "Scan at every hub. Questions: the Acme Freight demo site's Track page.")
    content = "\n".join(ops).encode("cp1252")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
         b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>" % (PAGE_W, PAGE_H)),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        (b"<< /Title " + _pdf_text(f"Acme Freight label {fields['id']}").encode("cp1252")
         + b" /Producer (Acme Freight demo) >>"),
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += (b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, len(objects), xref))
    return bytes(out)
