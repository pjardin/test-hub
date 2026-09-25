"""Manifest import: a customer's CSV of shipments, validated and priced row
by row on the server.

Rows are checked the way a real intake desk checks them -- known cities, a
sane weight, a service we sell, no hazardous goods on Express -- priced with
the same engine as the quote wizard, and every problem names the row, the
column and the fix. It runs as a background job: rows take time
(manifest_row_s per release), which is what makes the progress bar, and the
hub's step timings across releases, worth watching.
"""
import csv
import io
from decimal import Decimal, InvalidOperation

from freight import catalog, pricing

COLUMNS = ["reference", "origin", "destination", "weight_kg", "pieces",
           "service", "hazardous"]
MAX_BYTES = 512 * 1024
MAX_ROWS = 2000
YES = {"y", "yes", "true", "1"}
NO = {"", "n", "no", "false", "0"}


class ManifestError(ValueError):
    """The file as a whole cannot be read -- shown on the upload form."""


def parse(data):
    """bytes -> list of row dicts (with their line numbers). Raises
    ManifestError for problems with the FILE; row problems come later."""
    if len(data) > MAX_BYTES:
        raise ManifestError(f"The file is {len(data) // 1024} KB; the limit is "
                            f"{MAX_BYTES // 1024} KB.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ManifestError("The file is not UTF-8 text. Save it from your "
                            "spreadsheet as 'CSV UTF-8' and try again.")
    reader = csv.DictReader(io.StringIO(text))
    header = [str(h or "").strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in COLUMNS if c not in header]
    if missing:
        raise ManifestError("Missing column(s): " + ", ".join(missing)
                            + ". The first line must name the columns: "
                            + ",".join(COLUMNS))
    rows = []
    for number, raw in enumerate(reader, start=2):      # line 1 is the header
        row = {str(k or "").strip().lower(): str(v or "").strip()
               for k, v in raw.items() if k is not None}
        if not any(row.get(c) for c in COLUMNS):
            continue                                      # blank line
        row["_line"] = number
        rows.append(row)
        if len(rows) > MAX_ROWS:
            raise ManifestError(f"More than {MAX_ROWS} rows; split the file.")
    if not rows:
        raise ManifestError("The file has a header but no rows.")
    return rows


def check_row(row, seen_refs):
    """-> (status, messages, priced) for one row."""
    problems = []
    ref = row.get("reference", "")
    if not ref:
        problems.append("reference: missing")
    elif ref in seen_refs:
        problems.append(f"reference: {ref} appears twice in this file")
    seen_refs.add(ref)

    places = {}
    for col in ("origin", "destination"):
        text = row.get(col, "")
        if not text:
            problems.append(f"{col}: missing")
            continue
        city = catalog.find_city(text)
        if city is None:
            hint = catalog.did_you_mean(text)
            problems.append(f"{col}: unknown city '{text}'"
                            + (f" -- did you mean {hint}?" if hint else ""))
        else:
            places[col] = city

    weight = None
    try:
        weight = Decimal(row.get("weight_kg", ""))
        if weight <= 0 or weight > pricing.MAX_KG:
            problems.append(f"weight_kg: {row.get('weight_kg')} is not between "
                            f"0 and {pricing.MAX_KG:,} kg")
            weight = None
    except InvalidOperation:
        problems.append(f"weight_kg: '{row.get('weight_kg', '')}' is not a number")

    try:
        if int(row.get("pieces") or "1") < 1:
            raise ValueError
    except ValueError:
        problems.append(f"pieces: '{row.get('pieces')}' must be a whole number, 1 or more")

    service = (row.get("service") or "standard").lower()
    if service not in pricing.SERVICE_INFO:
        problems.append(f"service: '{row.get('service')}' -- use economy, "
                        "standard or express")
    hazardous_raw = (row.get("hazardous") or "").lower()
    if hazardous_raw not in YES | NO:
        problems.append(f"hazardous: '{row.get('hazardous')}' -- use yes or no")
    hazardous = hazardous_raw in YES
    if hazardous and service == "express":
        problems.append("service: hazardous goods cannot fly Express -- "
                        "choose Standard or Economy")

    if problems:
        return "error", problems, None
    quote = pricing.quote(places["origin"], places["destination"], weight,
                          service, hazardous=hazardous)
    return "ok", [], quote


def run(job, params):
    rows = params["rows"]
    step_s = float(params.get("step_s", 0.1))
    job.update(0.0, f"Checking {len(rows)} rows")
    job.say(f"{params.get('filename') or 'manifest'}: {len(rows)} rows")
    out, seen, ok, bad = [], set(), 0, 0
    total = Decimal("0")
    for i, row in enumerate(rows, start=1):
        status, messages, quote = check_row(row, seen)
        if status == "ok":
            ok += 1
            total += quote.total
        else:
            bad += 1
            job.say(f"line {row['_line']}: " + "; ".join(messages))
        out.append({
            "line": row["_line"], "reference": row.get("reference", ""),
            "origin": row.get("origin", ""), "destination": row.get("destination", ""),
            "weight_kg": row.get("weight_kg", ""),
            "service": (row.get("service") or "standard").lower(),
            "status": status, "messages": messages,
            "distance_km": quote.distance_km if quote else None,
            "price": str(quote.total) if quote else "",
        })
        job.point(i, ok, bad)
        job.update(i / len(rows), f"Row {i} of {len(rows)} -- {ok} ok, {bad} with problems")
        job.sleep(step_s)
    job.say(f"{ok} rows priced, {bad} need fixing")
    return {"filename": params.get("filename") or "manifest.csv",
            "rows": out, "ok": ok, "errors": bad, "total": str(total)}


def results_csv(result):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["line", "reference", "origin", "destination", "weight_kg",
                     "service", "status", "distance_km", "price_usd", "problems"])
    for r in result["rows"]:
        writer.writerow([r["line"], r["reference"], r["origin"], r["destination"],
                         r["weight_kg"], r["service"], r["status"],
                         r["distance_km"] if r["distance_km"] is not None else "",
                         r["price"], " | ".join(r["messages"])])
    return buf.getvalue()
