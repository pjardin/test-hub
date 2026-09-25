"""A shipping label: a new tab, a barcode a scanner can read, a PDF.

"Print label" opens the label in a NEW TAB -- page.expect_popup() catches it
and hands the test a second page to drive. The test then reads the barcode
the way a depot scanner would: from the bars alone, with its own decoder
(_lib/barcode.py -- it shares no code with the site that printed the label,
so a label that encodes the wrong thing cannot fool it). Finally it
downloads the PDF, checks it is one, and reads the barcode out of the PDF
too. Both files are kept with the run.

The activity reel and video follow the first tab, so the label's picture is
saved explicitly.
"""
from _lib.barcode import decode, pdf_bars
from _lib.freight import Freight

SHIPMENT = "AF-100001"
BARS = "rs => rs.map(r => [+r.getAttribute('x'), +r.getAttribute('width')])"


def run(page, ctx):
    site = Freight(page, ctx).sign_in()
    site.open(f"ops/shipments/{SHIPMENT}/")

    with ctx.timed("open the label (a new tab)"):
        with page.expect_popup() as popup:
            page.click("#print-label")
        label = popup.value
        label.wait_for_selector("#label-barcode")
    ctx.log(f"the label opened in a new tab: {label.url}")
    label.screenshot(path=str(ctx.artifacts_dir / "screenshots" / "label-tab.png"))

    with ctx.timed("scan the barcode"):
        scanned = decode(label.eval_on_selector_all("#label-barcode g rect", BARS))
    ctx.log(f"the barcode reads {scanned!r}")
    assert scanned == SHIPMENT, (
        f"the label's barcode reads {scanned!r}: a depot scanner would route this parcel "
        f"as {scanned}, not {SHIPMENT}")
    assert label.inner_text("#label-number").strip() == SHIPMENT

    with ctx.timed("download the PDF"):
        with label.expect_download() as download:
            label.click("#label-pdf")
        pdf = ctx.artifacts_dir / f"label-{SHIPMENT}.pdf"
        download.value.save_as(str(pdf))
    data = pdf.read_bytes()
    assert data.startswith(b"%PDF-"), "the download is not a PDF"
    in_pdf = decode(pdf_bars(data))
    assert in_pdf == SHIPMENT, f"the PDF's barcode reads {in_pdf!r}, not {SHIPMENT}"
    ctx.log(f"the PDF ({len(data)} bytes) carries the same barcode")
    label.close()
