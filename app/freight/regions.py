"""Delivery regions of the Acme Freight demo -- plain data, no Django.

Shared by the app (route optimizer, regional maps) and by
scripts/build-freight-geodata.py, which clips the bundled map data to exactly
these boxes -- so the two can never disagree about where a region is.

bbox = (lon_min, lat_min, lon_max, lat_max): the part of the map a region
page shows. The depot is looked up by (city name, ISO country code) in the
bundled cities; every other city inside the box -- in one of `countries`,
when that is set -- is a candidate delivery stop.
"""

REGIONS = {
    "california": {
        "label": "California",
        "bbox": (-124.6, 32.3, -114.0, 42.2),
        "depot": ("Oakland", "US"),
        "countries": ("US",),
    },
    "northeast": {
        "label": "US Northeast",
        "bbox": (-80.6, 38.7, -66.8, 47.6),
        "depot": ("Newark", "US"),
        "countries": ("US",),
    },
    "texas": {
        "label": "Texas",
        "bbox": (-106.8, 25.7, -93.4, 36.6),
        "depot": ("Dallas", "US"),
        "countries": ("US",),
    },
    "central_europe": {
        "label": "Central Europe",
        "bbox": (2.0, 45.0, 19.5, 55.2),
        "depot": ("Frankfurt", "DE"),
        "countries": None,
    },
}

# Degrees of geometry kept around each box when the data file is built, so a
# coastline never visibly ends at the edge of the drawing.
MARGIN = 2.5

# Cities at least this big inside a region become delivery-stop candidates.
REGION_MIN_POPULATION = 25000
