"""The monthly operations report: a deliberately slow, multi-stage job.

Six stages, each doing real aggregation over the month's shipments --
volumes, on-time rate, revenue by service and lane, hub scores -- paced by
the release (report_unit_s) so a "full" report takes a couple of minutes.
That is the hub's long-wait case: a test starts it and waits, and the
activity reel's idle sampling keeps the artifacts small while it does.
"""
import csv
import datetime as dt
import io
from collections import Counter, defaultdict
from decimal import Decimal

from freight import catalog

STAGES = [
    ("Collecting the month's shipments", 6),
    ("Computing on-time performance", 6),
    ("Aggregating revenue by service and lane", 6),
    ("Scoring hubs and lanes", 4),
    ("Rendering charts", 4),
    ("Finalizing the report", 2),
]
DETAIL = {"summary": 1, "full": 4}


def available_months():
    """The months the generated history covers, newest first: [(key, label)]."""
    months = []
    for s in catalog.shipments():
        key = s.created.strftime("%Y-%m")
        if key not in [m[0] for m in months]:
            months.append((key, s.created.strftime("%B %Y")))
    return sorted(months, reverse=True)


def estimate_seconds(detail, unit_s):
    return sum(units for _, units in STAGES) * DETAIL.get(detail, 1) * unit_s


def run(job, params):
    month = params["month"]
    detail = params.get("detail", "summary")
    unit_s = float(params.get("unit_s", 1.0))
    reps = DETAIL.get(detail, 1)
    total_units = sum(units for _, units in STAGES) * reps
    done = 0

    def work(stage_name, units):
        nonlocal done
        for _ in range(units * reps):
            done += 1
            job.update(done / (total_units + 1), stage_name)
            job.point(done, round(100 * done / total_units))
            job.sleep(unit_s)

    stage_name, units = STAGES[0]
    job.say(stage_name)
    ships = [s for s in catalog.shipments() if s.created.strftime("%Y-%m") == month]
    work(stage_name, units)
    job.say(f"{len(ships)} shipments booked in {month}")

    stage_name, units = STAGES[1]
    job.say(stage_name)
    delivered = [s for s in ships if s.status == "delivered"]
    on_time = sum(1 for s in delivered if s.on_time)
    transit = [(s.delivered - s.created).total_seconds() / 86400 for s in delivered]
    work(stage_name, units)

    stage_name, units = STAGES[2]
    job.say(stage_name)
    by_service = defaultdict(lambda: {"shipments": 0, "revenue": Decimal("0"), "kg": 0.0})
    lanes = defaultdict(lambda: {"shipments": 0, "revenue": Decimal("0")})
    by_customer = defaultdict(lambda: {"shipments": 0, "revenue": Decimal("0")})
    daily = defaultdict(lambda: {"shipments": 0, "revenue": Decimal("0")})
    for s in ships:
        for bucket in (by_service[s.service], lanes[(s.origin_hub.code, s.dest_hub.code)],
                       by_customer[s.customer], daily[s.created.date().isoformat()]):
            bucket["shipments"] += 1
            bucket["revenue"] += s.price
        by_service[s.service]["kg"] += s.weight_kg
    work(stage_name, units)

    stage_name, units = STAGES[3]
    job.say(stage_name)
    exceptions = Counter(s.origin_hub.code for s in ships if s.status == "exception")
    work(stage_name, units)

    for stage_name, units in STAGES[4:]:
        job.say(stage_name)
        work(stage_name, units)

    revenue = sum((s.price for s in ships), Decimal("0"))
    top = lambda d, n: sorted(d.items(), key=lambda kv: -kv[1]["revenue"])[:n]  # noqa: E731
    label = dt.date(int(month[:4]), int(month[5:]), 1).strftime("%B %Y")
    return {
        "month": month, "label": label, "detail": detail,
        "kpis": {
            "shipments": len(ships),
            "delivered": len(delivered),
            "on_time_pct": round(100.0 * on_time / len(delivered), 1) if delivered else None,
            "revenue": str(revenue),
            "avg_transit_days": round(sum(transit) / len(transit), 1) if transit else None,
            "exceptions": sum(exceptions.values()),
        },
        "by_service": [{"service": k, "shipments": v["shipments"],
                        "revenue": str(v["revenue"]), "kg": round(v["kg"], 1)}
                       for k, v in sorted(by_service.items())],
        "top_lanes": [{"lane": f"{a} -> {b}", "shipments": v["shipments"],
                       "revenue": str(v["revenue"])} for (a, b), v in top(lanes, 8)],
        "top_customers": [{"customer": k, "shipments": v["shipments"],
                           "revenue": str(v["revenue"])} for k, v in top(by_customer, 8)],
        "daily": [{"date": k, "shipments": v["shipments"], "revenue": str(v["revenue"])}
                  for k, v in sorted(daily.items())],
        "exception_hubs": [{"hub": k, "exceptions": n} for k, n in exceptions.most_common(5)],
    }


def report_csv(result):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["section", "key", "shipments", "revenue_usd", "extra"])
    k = result["kpis"]
    w.writerow(["kpi", "shipments", k["shipments"], "", ""])
    w.writerow(["kpi", "revenue", "", k["revenue"], ""])
    w.writerow(["kpi", "on_time_pct", "", "", k["on_time_pct"]])
    for row in result["by_service"]:
        w.writerow(["service", row["service"], row["shipments"], row["revenue"], row["kg"]])
    for row in result["top_lanes"]:
        w.writerow(["lane", row["lane"], row["shipments"], row["revenue"], ""])
    for row in result["top_customers"]:
        w.writerow(["customer", row["customer"], row["shipments"], row["revenue"], ""])
    for row in result["daily"]:
        w.writerow(["day", row["date"], row["shipments"], row["revenue"], ""])
    return buf.getvalue()
