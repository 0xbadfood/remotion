"""Contextual English year wording; ordinary quantities stay cardinal."""
from __future__ import annotations

import re

from num2words import num2words


_YEAR = re.compile(r"(?<![\w.,+])\d{1,4}(?!\w|[.,]\d)")
_DATE_BEFORE = re.compile(
    r"\b(?:in|since|by|before|after|around|circa|during|from|through|until|between|"
    r"year|years|dated|born|died)\s+(?:about\s+)?$", re.I)
_ERA = re.compile(r"\s*(?:B\.?C\.?E?\.?|A\.?D\.?|C\.?E\.?)\b", re.I)
_QUANTITY_AFTER = re.compile(
    r"\s*(?:%|percent\b|per\b|(?:million|billion|trillion|thousand)\b|"
    r"(?:people|persons|soldiers|troops|men|women|children|workers|residents|cases|"
    r"houses|households|families|students|patients|deaths|births|lives|votes|seats|"
    r"books|pages|items|units|vehicles|ships|acres|hectares|rupees|dollars|pounds|"
    r"euros|years|months|weeks|days|hours|minutes|seconds|meters|metres|kilometers|"
    r"kilometres|miles|grams|kilograms|litres|liters|watts|volts|degrees|calories|"
    r"kcal|kg|km|cm|mm|ml|mg|hz|mhz|gb|mb|fps)\b)", re.I)


def _is_year(text, start, end):
    literal = text[start:end]
    number = int(literal)
    before, after = text[:start], text[end:]
    if before.endswith("-") and (len(before) < 2 or not before[-2].isdigit()):
        return False
    # Explicit era notation also supports ancient dates below 1000.
    if _ERA.match(after) or re.search(r"\b(?:AD|CE|BC|BCE)\s*$", before, re.I):
        return True
    if not 1000 <= number <= 2999:
        return False
    if re.search(r"[$£€₹]\s*$|\b(?:USD|INR|EUR|GBP)\s*$", before, re.I):
        return False
    if _QUANTITY_AFTER.match(after):
        return False
    right = re.match(r"\s*(?:[-–—]|to|through|and)\s*(\d{4})(?!\d)", after, re.I)
    if right and _QUANTITY_AFTER.match(after[right.end():]):
        return False
    if _DATE_BEFORE.search(before):
        return True
    if re.search(r"\b(?:January|February|March|April|May|June|July|August|September|"
                 r"October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s*$", before, re.I):
        return True
    # A range inherits date context from its first endpoint.
    left = re.search(r"(\d{4})\s*(?:[-–—]|to|through|and)\s*$", before, re.I)
    if left and _is_year(text, left.start(1), left.end(1)):
        return True
    if right and (_ERA.match(after[right.end():]) or not before.strip()):
        return True
    # Bare years in headings or date-led sentences, without guessing an
    # unmarked measurement such as 'the capacity reached 1970'.
    return not before.strip() and (not after.strip(" .,;:!?()") or bool(
        re.match(r"\s*(?:was|is|marked|saw|brought|became|AD|CE|BC|BCE)\b", after, re.I)))


def year_text(text, *, context=None, offset=0):
    """Use the full source context even when expanding one subtitle token."""
    full = text if context is None else context

    def expand(match):
        start, end = offset + match.start(), offset + match.end()
        if not _is_year(full, start, end):
            return match.group()
        year = int(match.group())
        if year < 1000:
            return num2words(year, lang="en")
        if 2000 <= year <= 2009:
            return "two thousand" + (" " + num2words(year % 100) if year % 100 else "")
        return num2words(year, lang="en", to="year").replace("-", " ")

    # Preserve offsets into the original context while wording date ranges.
    pieces, cursor, previous = [], 0, None
    for match in _YEAR.finditer(text):
        gap = text[cursor:match.start()]
        if previous is not None and re.fullmatch(r"\s*[-–—]\s*", gap):
            gap = " to "
        pieces.extend((gap, expand(match)))
        cursor, previous = match.end(), match
    pieces.append(text[cursor:])
    return "".join(pieces)


_SMALL = dict(zip("zero one two three four five six seven eight nine".split(), range(10)))
_TEENS = dict(zip("ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(), range(10, 20)))
_TENS = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10)))
_CENTURY = {**_TEENS, "twenty": 20, "twenty one": 21, "twenty two": 22,
            "twenty three": 23, "twenty four": 24, "twenty five": 25,
            "twenty six": 26, "twenty seven": 27, "twenty eight": 28, "twenty nine": 29}
_LAST = (r"(?:" + "|".join(_TENS) + r")(?:\s+(?:" + "|".join(_SMALL) +
         r"))?|(?:" + "|".join(_TEENS) + r")|(?:" + "|".join(_SMALL) + r")")
_PAIR = re.compile(
    r"\b(" + "|".join(sorted(_CENTURY, key=len, reverse=True)) + r")\s+"
    r"(hundred(?:\s+(?:and\s+)?(?:" + _LAST + r"))?|oh\s+(?:" +
    "|".join(_SMALL) + r")|(?:" + "|".join(_TENS) + r")(?:\s+(?:" +
    "|".join(_SMALL) + r"))?|(?:" + "|".join(_TEENS) + r"))\b", re.I)


def canonical_year_pairs(text):
    """Scoring only: 'nineteen seventy' and 1970 retain the same value."""
    value = re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", " ", text)

    def expand(match):
        century = _CENTURY[match.group(1).lower()]
        tail = match.group(2).lower().split()
        if tail[0] == "hundred":
            tail = [word for word in tail[1:] if word != "and"]
        if not tail:
            last = 0
        elif tail[0] == "oh":
            last = _SMALL[tail[1]]
        elif tail[0] in _TEENS:
            last = _TEENS[tail[0]]
        elif tail[0] in _SMALL:
            last = _SMALL[tail[0]]
        else:
            last = _TENS[tail[0]] + (_SMALL[tail[1]] if len(tail) > 1 else 0)
        return num2words(century * 100 + last, lang="en")

    return _PAIR.sub(expand, value)
