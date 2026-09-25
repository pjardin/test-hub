"""Upload a file, let the server chew on it, download what comes back.

Builds a 30-row shipping manifest (CSV) with three deliberate mistakes,
uploads it through the page's file picker, waits for the server to check and
price every row, then asserts the three problems were caught -- each named
on its own row -- and downloads the processed file to compare.

File upload (set_input_files) and download (expect_download) are the two
things record-and-replay tools most often get wrong; here they are a few
readable lines.
"""
import csv
import io

from _lib.freight import Freight

GOOD = [
    ("Oakland, US", "Fresno, US"), ("Boston, US", "Philadelphia, US"),
    ("Dallas, US", "Houston, US"), ("San Diego, US", "Sacramento, US"),
    ("Austin, US", "El Paso, US"), ("Providence, US", "Albany, US"),
    ("San Jose, US", "Bakersfield, US"), ("Baltimore, US", "Hartford, US"),
    ("Fort Worth, US", "San Antonio, US"),
]
MISTAKES = {
    7: ("destination", "Sacremento"),       # typo -> "did you mean"
    15: ("weight_kg", "heavy"),             # not a number
    23: ("hazardous", "yes"),               # ...on Express: not allowed
}


def build_manifest():
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["reference", "origin", "destination", "weight_kg",
                     "pieces", "service", "hazardous"])
    for n in range(1, 31):
        origin, dest = GOOD[n % len(GOOD)]
        row = {"reference": f"QA-{7000 + n}", "origin": origin, "destination": dest,
               "weight_kg": str(20 + n * 3.5), "pieces": str(1 + n % 4),
               "service": ("standard", "economy", "express")[n % 3], "hazardous": "no"}
        if n in MISTAKES:
            column, value = MISTAKES[n]
            row[column] = value
            if column == "hazardous":
                row["service"] = "express"
        writer.writerow([row[c] for c in ("reference", "origin", "destination",
                                          "weight_kg", "pieces", "service", "hazardous")])
    return buf.getvalue()


def run(page, ctx):
    site = Freight(page, ctx).sign_in()
    manifest = ctx.artifacts_dir / "manifest.csv"
    manifest.write_text(build_manifest())

    page.goto(site.url("ops/import/"))
    page.set_input_files("#manifest-file", str(manifest))
    with ctx.timed("process the manifest"):
        page.click("#upload-btn")
        state = site.wait_for_job(timeout_s=90)
    assert state == "done", f"the import ended '{state}'"

    ok, bad = int(page.inner_text("#import-ok")), int(page.inner_text("#import-errors"))
    ctx.log(f"{ok} rows priced, {bad} with problems")
    ctx.screenshot("import-result")
    assert (ok, bad) == (27, 3), f"expected 27 priced and 3 problems, got {ok} and {bad}"
    problems = " | ".join(page.locator("tr.row-error").all_inner_texts())
    for hint in ("did you mean", "is not a number", "cannot fly Express"):
        assert hint in problems, f"the import should have reported '{hint}': {problems}"

    results = site.download("#download-result", ctx.artifacts_dir / "manifest-results.csv")
    with open(results, newline="") as fh:
        statuses = [row["status"] for row in csv.DictReader(fh)]
    assert statuses.count("error") == 3 and statuses.count("ok") == 27, statuses
