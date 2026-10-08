"""The bike a rider is listed on, read off the provider's own logo.

The official series-points tables draw the BIKE column as an image —
.../manufacturers/primary/ktm.png — and that is the make the championship
records for the rider. Our riders table holds a rider's CURRENT team, so over
the winter 2026's standings showed 2027 bikes (Justin Hill on a Husqvarna he
never raced in 2026), and privateers showed no bike at all.

Stdlib only: the API imports this (see tests/test_api_deps.py).
"""

import re

# Spelled the way the app's logo table is keyed (App.js MAKE_LOGOS).
_SLUGS = {
    "ktm": "KTM", "honda": "Honda", "yamaha": "Yamaha", "kawasaki": "Kawasaki",
    "suzuki": "Suzuki", "husqvarna": "Husqvarna", "gasgas": "GasGas",
    "gas-gas": "GasGas", "triumph": "Triumph", "ducati": "Ducati",
    "beta": "Beta", "stark": "Stark",
}
_LOGO_RE = re.compile(r"/manufacturers/[^/]+/([a-z0-9_-]+)\.(?:png|svg|jpg)", re.I)


def bike_from_logo(src):
    """'KTM' from '.../manufacturers/primary/ktm.png'; None if unrecognised.
    An unknown slug is title-cased rather than dropped: a new make should
    still say something."""
    m = _LOGO_RE.search(src or "")
    if not m:
        return None
    slug = m.group(1).lower()
    return _SLUGS.get(slug) or slug.replace("-", " ").title()


def bike_in_cell(cell):
    """The bike in one BeautifulSoup table cell, or None."""
    img = cell.find("img") if cell is not None else None
    return bike_from_logo(img.get("src")) if img else None
