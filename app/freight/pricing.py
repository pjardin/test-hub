"""Quote pricing: the server-side money logic behind the quote wizard, the
manifest import and every generated shipment.

Decimal throughout, rounded half-up to the cent: a tester comparing
"$1,234.56" on screen with a spreadsheet must never be told the difference
is float rounding. The one deliberate bug (release 2.1.0 applies a promo
code twice) goes through `promo_multiplier`, so the correct formula stays
readable here and the fault is visible in exactly one place.
"""
from collections import namedtuple
from decimal import ROUND_HALF_UP, Decimal

from freight import catalog

CENT = Decimal("0.01")

SERVICE_INFO = {
    "economy": {"label": "Economy", "days": "5-7 business days",
                "rate": Decimal("0.00058"), "base": Decimal("18.00")},
    "standard": {"label": "Standard", "days": "3-4 business days",
                 "rate": Decimal("0.00085"), "base": Decimal("25.00")},
    "express": {"label": "Express", "days": "1-2 business days",
                "rate": Decimal("0.00145"), "base": Decimal("42.00")},
}
FUEL_RATE = Decimal("0.085")
HAZMAT_RATE = Decimal("0.25")
OVERSIZE_RATE = Decimal("0.15")
OVERSIZE_CM = 150
VOLUMETRIC_DIVISOR = 5000        # cm3 per kg -- the air-freight convention
MIN_KM = 50
MAX_KG = Decimal("20000")
PROMOS = {"SPRING10": Decimal("0.10"), "LOYAL15": Decimal("0.15")}
BROKERAGE_FEE = Decimal("35.00")
# customs step (release 2.0+): declared value x a duty rate per goods category
DUTY_RATES = {
    "electronics": Decimal("0.035"), "textiles": Decimal("0.12"),
    "machinery": Decimal("0.025"), "food": Decimal("0.08"),
    "documents": Decimal("0"), "other": Decimal("0.05"),
}

Line = namedtuple("Line", "code label amount")
Quote = namedtuple("Quote", "service distance_km chargeable_kg lines subtotal "
                            "discount fuel total promo")


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def is_international(origin, dest):
    return origin.iso2 != dest.iso2


def distance_km(origin, dest):
    """Billable distance: roads wind (x1.25) for domestic ground runs under
    1,500 km; everything else flies, and air lanes nearly don't (x1.08)."""
    km = catalog.haversine_km(origin, dest)
    ground = not is_international(origin, dest) and km < 1500
    return max(MIN_KM, int(round(km * (1.25 if ground else 1.08))))


def chargeable_kg(weight_kg, dims_cm=None):
    """The larger of actual and volumetric weight: a box of pillows pays for
    the space it takes, not for what it weighs."""
    kg = Decimal(str(weight_kg))
    if dims_cm:
        length, width, height = (Decimal(str(v)) for v in dims_cm)
        kg = max(kg, length * width * height / VOLUMETRIC_DIVISOR)
    return kg.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def quote(origin, dest, weight_kg, service="standard", dims_cm=None,
          hazardous=False, customs=None, promo="", promo_multiplier=1):
    info = SERVICE_INFO[service]
    km = distance_km(origin, dest)
    ckg = chargeable_kg(weight_kg, dims_cm)
    transport = money(ckg * km * info["rate"])
    lines = [Line("base", f"{info['label']} base fee", money(info["base"])),
             Line("transport", f"Transport: {ckg} kg x {km:,} km", transport)]
    if hazardous:
        lines.append(Line("hazmat", "Hazardous goods handling (25%)",
                          money(transport * HAZMAT_RATE)))
    if dims_cm and max(dims_cm) > OVERSIZE_CM:
        lines.append(Line("oversize", "Oversize handling (15%)",
                          money(transport * OVERSIZE_RATE)))
    if customs:
        category = customs.get("category") or "other"
        rate = DUTY_RATES.get(category, DUTY_RATES["other"])
        lines.append(Line("duty", f"Import duty estimate ({category})",
                          money(Decimal(str(customs.get("declared_value") or 0)) * rate)))
        lines.append(Line("brokerage", "Customs brokerage", BROKERAGE_FEE))
    subtotal = money(sum(line.amount for line in lines))
    code = (promo or "").strip().upper()
    rate = PROMOS.get(code)
    discount = money(subtotal * rate * promo_multiplier) if rate else money(0)
    fuel = money((subtotal - discount) * FUEL_RATE)
    total = money(subtotal - discount + fuel)
    return Quote(service, km, ckg, lines, subtotal, discount, fuel, total,
                 code if rate else "")


def fmt(amount):
    """$1,234.56 -- the one money format every page uses."""
    return f"${money(amount):,.2f}"
