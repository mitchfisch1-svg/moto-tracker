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


# Suffixes a plain .title() mangles. "III" becomes "Iii", which is how the app
# ended up listing a rider called "Will Canaguier Iii".
_NAME_SUFFIXES = {"II", "III", "IV", "V", "VI", "JR", "SR"}


def _is_shouted(s: str) -> bool:
    """Did the source write this name in capitals?

    Judged on the ASCII letters alone, because those are the ones a results
    sheet cases reliably. The site publishes Cornelius Tondel with a LOWERCASE
    o-slash inside an otherwise shouted name, so `s == s.upper()` said no and
    the rider was stored, and displayed, as "CORNELIUS ToNDEL" all season.
    """
    ascii_letters = [c for c in s if c.isascii() and c.isalpha()]
    return bool(ascii_letters) and all(c.isupper() for c in ascii_letters)


def titlecase_name(raw):
    """Title-case a SHOUTED name without wrecking suffixes and Mc- prefixes.

    Results arrive upper-cased, so they have to be re-cased for display. A bare
    .title() gets the ordinary cases right and the interesting ones wrong:
    "III" -> "Iii", "MCGRATH" -> "Mcgrath". A name that already carries mixed
    case is left alone — the source knew what it meant.
    """
    if not raw:
        return raw
    s = str(raw)
    if not _is_shouted(s):
        return s
    out = []
    for word in s.split(" "):
        bare = word.strip(".").upper()
        if not word:
            out.append(word)
        elif bare in _NAME_SUFFIXES:
            out.append(word.upper())               # III, JR. stay shouted
        elif bare.startswith("MC") and len(bare) > 3:
            out.append("Mc" + word[2:].title())    # McGrath, not Mcgrath
        else:
            out.append(word.title())               # O'BRIEN -> O'Brien
    return " ".join(out)


def display_surname(full):
    """The name to show when there is only room for one word.

    "Tre Fierro III" must not render as "III". A lock screen listing a rider
    called III is exactly the kind of detail that makes a whole board look
    untrustworthy, so a suffix brings the real surname along with it.
    """
    parts = [p for p in str(full or "").split() if p]
    if not parts:
        return ""
    if len(parts) > 1 and parts[-1].strip(".").upper() in _NAME_SUFFIXES:
        return " ".join(parts[-2:])                # "Fierro III"
    return parts[-1]
