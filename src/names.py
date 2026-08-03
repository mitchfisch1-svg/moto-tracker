"""Rider-name slugs for the photo sources, which key images by name.

Both Feld (feld-smx-rider-headshots/firstname-lastname.png) and Racer X
(racerxonline.com/rider/first-last) address riders by a slug built from their
name, so a rider whose name we store slightly differently is simply invisible
to us. That is not hypothetical: results list R.J. Hampshire as "R J Hampshire",
which slugs to "r-j-hampshire" while BOTH sources use "rj-hampshire" — so a
rider 5th in the 450 championship had no photo from either source.

Hence variants rather than one slug: generate the plausible spellings and let
the caller keep whichever one actually resolves.
"""

import re
import unicodedata

# Letters that aren't accented forms and so survive NFKD unchanged — Nordic
# names reach us through the results scraper (Cornelius Tøndel, Mikkel Haarup).
_LETTER_MAP = {
    "ø": "o", "æ": "ae", "å": "a", "ß": "ss", "đ": "d", "ł": "l", "ð": "d",
    "þ": "th", "ı": "i", "œ": "oe",
}


def fold(text: str) -> str:
    """Lowercase ASCII form of a name: 'Jérémy Tøndel' -> 'jeremy tondel'."""
    out = text.lower()
    for src, dst in _LETTER_MAP.items():
        out = out.replace(src, dst)
    return "".join(c for c in unicodedata.normalize("NFKD", out)
                   if not unicodedata.combining(c))


def _slug(text: str) -> str:
    return re.sub(r"\s+", "-", re.sub(r"[^a-z0-9 \-]", "", text).strip())


def slug_variants(full_name: str) -> list[str]:
    """Plausible source slugs for a rider, most likely first."""
    folded = fold(full_name).strip()
    base = _slug(folded)
    variants = [base]

    # "r j hampshire" -> "rj hampshire". Initials reach us space-separated but
    # both sources write them closed up.
    joined = re.sub(r"\b([a-z])\s+(?=[a-z]\b)", r"\1", folded)
    for candidate in (_slug(joined),
                      # O'Brien -> o-brien as well as obrien
                      _slug(re.sub(r"[^a-z0-9 \-]", "-", folded)),
                      base.replace("-", "")):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return [v for v in variants if v]
