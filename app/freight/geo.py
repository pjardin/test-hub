"""Maps, drawn on the server as SVG from the bundled Natural Earth data.

No tile server, no JavaScript map library, no network: a basemap is one SVG
document built once per (map, theme) and cached; a page lays a small inline
SVG overlay -- hubs, lanes, routes, markers -- on top of it using the SAME
projection, so the two line up exactly. Server-side on purpose: the demo must
work air-gapped, and the overlay is plain DOM a test can inspect
(`svg .route`, `[data-city="Fresno"]`).

World map: the Equal Earth projection (Savric, Patterson & Jenny, 2018 --
an equal-area projection that still looks like the world people know).
Region maps: equirectangular scaled by cos(latitude) at the region's centre,
which is indistinguishable from fancier choices at state/country scale.
"""
import math
import threading

from freight import catalog
from freight.regions import REGIONS

THEMES = {
    "classic": {"sea": "#d6e6f3", "land": "#f5f2ea", "border": "#b4bfc9",
                "line": "#d2d8dd", "grid": "#c3d7e8"},
    "modern": {"sea": "#0c1a28", "land": "#1a2f45", "border": "#2e4d6b",
               "line": "#253e57", "grid": "#132638"},
}

# Equal Earth polynomial coefficients
_A1, _A2, _A3, _A4 = 1.340264, -0.081106, 0.000893, 0.003796
_M = math.sqrt(3) / 2


def _equal_earth(lon, lat):
    theta = math.asin(_M * math.sin(math.radians(lat)))
    t2 = theta * theta
    t6 = t2 * t2 * t2
    x = (math.radians(lon) * math.cos(theta)
         / (_M * (_A1 + 3 * _A2 * t2 + t6 * (7 * _A3 + 9 * _A4 * t2))))
    y = theta * (_A1 + _A2 * t2 + t6 * (_A3 + _A4 * t2))
    return x, y


class WorldProjection:
    key = "world"
    LAT_TOP, LAT_BOTTOM = 84.0, -57.0      # Antarctica is not drawn

    def __init__(self, width=1000):
        self._xmax = _equal_earth(180, 0)[0]
        self._ytop = _equal_earth(0, self.LAT_TOP)[1]
        ybot = _equal_earth(0, self.LAT_BOTTOM)[1]
        self._scale = width / (2 * self._xmax)
        self.width = width
        self.height = int(round((self._ytop - ybot) * self._scale))

    def xy(self, lat, lon):
        x, y = _equal_earth(lon, lat)
        return (round((x + self._xmax) * self._scale, 1),
                round((self._ytop - y) * self._scale, 1))


class RegionProjection:
    def __init__(self, key, width=900):
        self.key = key
        self.bbox = x0, y0, x1, y1 = REGIONS[key]["bbox"]
        self._cos = math.cos(math.radians((y0 + y1) / 2))
        self._k = width / ((x1 - x0) * self._cos)
        self.width = width
        self.height = int(round((y1 - y0) * self._k))

    def xy(self, lat, lon):
        x0, _, _, y1 = self.bbox
        return (round((lon - x0) * self._cos * self._k, 1),
                round((y1 - lat) * self._k, 1))


_projections = {}
_basemaps = {}
_lock = threading.Lock()


def projection(key):
    if key not in _projections:
        _projections[key] = (WorldProjection() if key == "world"
                             else RegionProjection(key))
    return _projections[key]


def _fmt(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _ring_d(proj, flat, close=True):
    pts = [proj.xy(flat[i + 1], flat[i]) for i in range(0, len(flat) - 1, 2)]
    if len(pts) < 2:
        return ""
    body = " ".join(f"{_fmt(x)} {_fmt(y)}" for x, y in pts)
    return f"M{body}{'Z' if close else ''}"


def basemap_svg(key, theme="classic"):
    """The whole basemap as an SVG document (cached per map and theme)."""
    if key != "world" and key not in REGIONS:
        raise KeyError(key)
    theme = theme if theme in THEMES else "classic"
    cache_key = (key, theme)
    with _lock:
        if cache_key in _basemaps:
            return _basemaps[cache_key]
    colors = THEMES[theme]
    proj = projection(key)
    data = catalog.geo()
    w, h = proj.width, proj.height
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
             f'width="{w}" height="{h}">',
             f'<rect width="{w}" height="{h}" fill="{colors["sea"]}"/>']
    if key == "world":
        grid = []
        for lon in range(-180, 181, 30):
            flat = []
            for lat in range(-57, 85, 3):
                flat += [lon, lat]
            grid.append(_ring_d(proj, flat, close=False))
        for lat in range(-30, 81, 30):
            flat = []
            for lon in range(-180, 181, 5):
                flat += [lon, lat]
            grid.append(_ring_d(proj, flat, close=False))
        parts.append(f'<path d="{" ".join(grid)}" fill="none" '
                     f'stroke="{colors["grid"]}" stroke-width="0.6"/>')
        countries = [(iso, rings) for _name, iso, rings in data["world"]]
        lines, lakes = [], []
    else:
        region = data["regions"][key]
        countries = region["land"]
        lines, lakes = region["lines"], region["lakes"]
    for iso, rings in countries:
        d = "".join(_ring_d(proj, ring) for ring in rings)
        if d:
            parts.append(f'<path d="{d}" fill="{colors["land"]}" '
                         f'stroke="{colors["border"]}" stroke-width="0.7" '
                         f'fill-rule="evenodd"><title>{iso}</title></path>')
    for ring in lakes:
        parts.append(f'<path d="{_ring_d(proj, ring)}" fill="{colors["sea"]}" '
                     f'stroke="{colors["border"]}" stroke-width="0.4"/>')
    if lines:
        d = "".join(_ring_d(proj, line, close=False) for line in lines)
        parts.append(f'<path d="{d}" fill="none" stroke="{colors["line"]}" '
                     f'stroke-width="0.8"/>')
    parts.append("</svg>")
    svg = "".join(parts)
    with _lock:
        _basemaps[cache_key] = svg
    return svg


def _vec(lat, lon):
    la, lo = math.radians(lat), math.radians(lon)
    return (math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la))


def great_circle(a, b, steps=48):
    """[(lat, lon), ...] along the great circle from a to b."""
    va, vb = _vec(*a), _vec(*b)
    dot = max(-1.0, min(1.0, sum(p * q for p, q in zip(va, vb))))
    omega = math.acos(dot)
    if omega < 1e-9:
        return [a, b]
    out = []
    for i in range(steps + 1):
        t = i / steps
        s1 = math.sin((1 - t) * omega) / math.sin(omega)
        s2 = math.sin(t * omega) / math.sin(omega)
        x, y, z = (s1 * p + s2 * q for p, q in zip(va, vb))
        out.append((math.degrees(math.atan2(z, math.hypot(x, y))),
                    math.degrees(math.atan2(y, x))))
    return out


def arc_d(proj, a, b, steps=48):
    """An SVG path along the great circle, split where it crosses the date
    line (so Los Angeles -> Tokyo does not draw a line across the world)."""
    pts = great_circle(a, b, steps)
    d, prev_lon = [], None
    for lat, lon in pts:
        x, y = proj.xy(lat, lon)
        cmd = "M" if prev_lon is None or abs(lon - prev_lon) > 180 else "L"
        d.append(f"{cmd}{_fmt(x)} {_fmt(y)}")
        prev_lon = lon
    return "".join(d)


def polyline(proj, latlons):
    return " ".join(f"{_fmt(x)},{_fmt(y)}"
                    for x, y in (proj.xy(lat, lon) for lat, lon in latlons))
