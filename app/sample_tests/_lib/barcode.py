"""Read a Code 128 barcode from its bars: the scanner's side of a shipping
label.

Written independently of the site that prints the label, on purpose: a depot
scanner shares no code with the printer, so a test that decoded with the
site's own encoder could never notice the site encoding the wrong thing.

    bars = label.eval_on_selector_all("#label-barcode g rect",
             "rs => rs.map(r => [+r.getAttribute('x'), +r.getAttribute('width')])")
    decode(bars)                 # -> "AF-100001"
    decode(pdf_bars(pdf_bytes))  # the same barcode, read out of the PDF

Handles code sets A, B and C with their switch codes, checks the start
symbol, the modulo-103 checksum and the stop symbol -- a misprinted label
raises ValueError with the reason, like a scanner beeping twice.
"""
import re
from collections import defaultdict

# Code 128 symbol values by bar/space widths (the published table)
_TABLE = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 "
    "221312 231212 112232 122132 122231 113222 123122 123221 223211 221132 "
    "221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 "
    "212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 "
    "231113 231311 112133 112331 132131 113123 113321 133121 313121 211331 "
    "231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 "
    "314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 "
    "112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
    "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 "
    "214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 "
    "114131 311141 411131 211412 211214 211232"
).split()
VALUE = {pattern: value for value, pattern in enumerate(_TABLE)}
STOP = "2331112"
START = {103: "A", 104: "B", 105: "C"}


def runs(bars):
    """[(x, width)] of the dark bars -> alternating bar/space widths."""
    bars = sorted((float(x), float(w)) for x, w in bars)
    out = []
    for i, (x, w) in enumerate(bars):
        if i:
            gap = x - (bars[i - 1][0] + bars[i - 1][1])
            if gap <= 1e-6:
                raise ValueError("bars overlap or touch: not a printable barcode")
            out.append(gap)
        out.append(w)
    return out


def modules(widths):
    """Widths -> whole modules (1..4). Every Code 128 symbol has a 1-module
    element, so the narrowest width is the module."""
    if not widths:
        raise ValueError("no bars found")
    unit = min(widths)
    out = []
    for w in widths:
        m = w / unit
        if abs(m - round(m)) > 0.2 or not 1 <= round(m) <= 4:
            raise ValueError(f"a bar/space {m:.2f} modules wide is not Code 128")
        out.append(int(round(m)))
    return out


def decode(bars):
    """The text a scanner reads from these bars."""
    mods = modules(runs(bars))
    if len(mods) < 6 * 3 + 7 or (len(mods) - 7) % 6:
        raise ValueError(f"{len(mods)} bars and spaces do not make whole Code 128 symbols")
    if "".join(map(str, mods[-7:])) != STOP:
        raise ValueError("no stop symbol at the end")
    values = []
    for i in range(0, len(mods) - 7, 6):
        pattern = "".join(map(str, mods[i:i + 6]))
        if pattern not in VALUE:
            raise ValueError(f"symbol {i // 6 + 1} ({pattern}) is not a Code 128 symbol")
        values.append(VALUE[pattern])
    start, data, check = values[0], values[1:-1], values[-1]
    if start not in START:
        raise ValueError(f"starts with symbol {start}, not a start code")
    expected = (start + sum(i * v for i, v in enumerate(data, 1))) % 103
    if check != expected:
        raise ValueError(f"checksum is {check}, the data says {expected}: a misprint")
    return _text(START[start], data)


def _text(code_set, data):
    out, shift = [], False
    for v in data:
        active = ("B" if code_set == "A" else "A") if shift else code_set
        shift = False
        if active == "C":
            if v < 100:
                out.append(f"{v:02d}")
            elif v == 100:
                code_set = "B"
            elif v == 101:
                code_set = "A"
            continue                               # 102 = FNC1: nothing to print
        if v < 64:
            out.append(chr(v + 32))
        elif v < 96:
            out.append(chr(v - 64) if active == "A" else chr(v + 32))
        elif v == 98:
            shift = True                           # the next symbol is in the other set
        elif v == 99:
            code_set = "C"
        elif v == 100 and code_set == "A":
            code_set = "B"
        elif v == 101 and code_set == "B":
            code_set = "A"
        # 96, 97, 102 (FNC3, FNC2, FNC1) and FNC4 carry no text
    return "".join(out)


_RECT_FILL = re.compile(rb"(-?[\d.]+) (-?[\d.]+) ([\d.]+) ([\d.]+) re f\b")


def pdf_bars(data):
    """The barcode's bars out of a PDF page: filled rectangles that share one
    baseline and one height -- the biggest such group on the page."""
    groups = defaultdict(list)
    for x, y, w, h in _RECT_FILL.findall(data):
        groups[(round(float(y), 1), round(float(h), 1))].append((float(x), float(w)))
    if not groups:
        raise ValueError("no filled rectangles in the PDF: no barcode")
    return max(groups.values(), key=len)
