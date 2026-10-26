"""Deterministic validation of high-stakes fields.

This module addresses failure class B, the misheard high-stakes identifier:

a registration plate is transcribed with one character wrong, the flow has no
control positioned to catch it, and the booking is committed and confirmed as
if the value were right.

The point of the module is that **no language model is involved**. Plates, VINs,
phone numbers and mileages are validated by code, because these are the fields
where a probabilistic component must never be the final authority.

Three ideas are encoded here, in the order a call reaches them:

1. `normalise`  — coerce a raw ASR string into a plausible canonical form.
2. `validate`   — check it against the country's *format mask*. A mask narrows
                  the field of plausible characters; it never identifies a
                  specific plate. So a mask alone is not enough.
3. `readback`   — grouped, phonetic, unambiguous read-back the caller can
                  correct. This is the highest-value control, because it moves
                  the check to the party who actually knows the answer.

Everything returns a structured result rather than raising, so the flow can
branch on it (and so the caller never hears "error").

A mask cannot detect a *valid but wrong* plate (two real plates can both match).
That is why `validate` returns `needs_lookup` — the flow must also reconcile
against the vehicle record before committing. The layers are complementary; the
whole point is that skipping one makes failure quieter, not rarer.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

IdentifierKind = Literal["license_plate", "vin", "phone", "mileage", "email", "postcode"]

# --------------------------------------------------------------------------- #
# Format masks
# --------------------------------------------------------------------------- #
# A mask is a positional template: L = letter, D = digit. Compared to a regex,
# a template keeps *positional* information, which is what lets us repair ASR
# confusions ("O" where a digit belongs -> "0") instead of guessing the whole
# string, and lets us group the read-back the way a human would say it.
#
# In production these come from the country spec, and can be overridden per
# dealer in `dealer_profiles.py`. Kept intentionally small here.

PLATE_MASKS: dict[str, list[str]] = {
    # GB (2001+ standard, plus prefix/suffix/NI formats still on the road)
    "GB": ["LLDDLLL", "LDDDLLL", "LLLDDDL", "LLLDDDD"],
    # NL (a few of the sidecodes; the real set is larger)
    "NL": ["LLDDDL", "DDLLDD", "DDLLLDD", "DDDDLL", "LLDDLL", "LDLLDD", "LLLDDL", "LDDDLL"],
    # DE (city code of variable length, then letters+digits, optional E/H suffix)
    "DE": ["LDDD", "LLDD", "LDLL", "LLLDD", "LLLDDL", "LLDDDD", "LLDDDDH", "LLLDDDDE"],
    # FR (SIV since 2009)
    "FR": ["LLDDDLL"],
    # ES (since 2000)
    "ES": ["DDDDLLL"],
    # IT
    "IT": ["LLDDDLL"],
    # BE
    "BE": ["DDDLLL", "LLLDDD", "DDDDDD"],
}

# ASR confusion pairs. Applied only in the direction the mask demands, and every
# coercion is reported so the flow knows to slow down and read back carefully.
_LETTER_TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "T": "1",
                    "Z": "2", "A": "4", "S": "5", "G": "6", "B": "8"}
_DIGIT_TO_LETTER = {"0": "O", "1": "I", "2": "Z", "3": "E", "4": "A", "5": "S",
                    "6": "G", "7": "T", "8": "B", "9": "P"}

_NATO = {
    "A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta", "E": "Echo",
    "F": "Foxtrot", "G": "Golf", "H": "Hotel", "I": "India", "J": "Juliett",
    "K": "Kilo", "L": "Lima", "M": "Mike", "N": "November", "O": "Oscar",
    "P": "Papa", "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango",
    "U": "Uniform", "V": "Victor", "W": "Whiskey", "X": "X-ray", "Y": "Yankee",
    "Z": "Zulu",
}
_DIGIT_WORDS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
                "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine"}


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #

@dataclass
class ValidationResult:
    """Structured outcome of a deterministic check.

    Never raises: the flow branches on `valid` / `needs_lookup` / `reason`.
    """

    kind: IdentifierKind
    raw: str
    normalised: str = ""
    valid: bool = False
    matched_mask: str | None = None
    country: str | None = None
    coercions: list[str] = field(default_factory=list)
    uncertain_positions: list[int] = field(default_factory=list)
    confidence: str = "low"  # low | medium | high
    reason: str = ""
    # A format check can never prove the identifier is *correct*, only plausible.
    # The flow must reconcile against the system of record before committing.
    needs_lookup: bool = True
    readback: str = ""
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        # Retell dynamic variables must be strings, so flatten to strings.
        d = asdict(self)
        d["coercions"] = ",".join(self.coercions)
        d["uncertain_positions"] = ",".join(str(p) for p in self.uncertain_positions)
        d["suggestions"] = ",".join(self.suggestions)
        d["valid"] = "true" if self.valid else "false"
        d["needs_lookup"] = "true" if self.needs_lookup else "false"
        return d


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

_NATO_UPPER = {word.upper(): letter for letter, word in _NATO.items()}

# Number words. Spoken plates mix digits and words ("A B twelve C D E"), and
# compound numbers are the common case a naive splitter gets wrong.
_UNITS = {
    "ZERO": 0, "OH": 0, "NIL": 0, "NOUGHT": 0,
    "ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5,
    "SIX": 6, "SEVEN": 7, "EIGHT": 8, "NINE": 9,
    "TEN": 10, "ELEVEN": 11, "TWELVE": 12, "THIRTEEN": 13, "FOURTEEN": 14,
    "FIFTEEN": 15, "SIXTEEN": 16, "SEVENTEEN": 17, "EIGHTEEN": 18,
    "NINETEEN": 19,
}
# Note the deliberate absence of a bare "O". Spoken, it is genuinely ambiguous
# between the letter O and zero, so it is read as the LETTER O and left to the
# mask repair step, which coerces it to 0 only in a position that must be a
# digit. Guessing here would defeat the point of having a mask at all.
_TENS = {"TWENTY": 20, "THIRTY": 30, "FORTY": 40, "FOURTY": 40, "FIFTY": 50,
         "SIXTY": 60, "SEVENTY": 70, "EIGHTY": 80, "NINETY": 90}

# A repair is only trustworthy while it is a small minority of the string.
# Past this, the capture itself is bad and the caller should be asked again
# rather than shown a mangled guess.
MAX_COERCIONS = 2
_SCALES = {"HUNDRED": 100, "THOUSAND": 1000}
_REPEAT = {"DOUBLE": 2, "TRIPLE": 3}

# Discourse filler that creeps in when someone spells something aloud. Stripped
# only as a *fallback* candidate, never the first attempt, so a plate that
# genuinely contains these letters is still matched.
_FILLER = {"FOR", "IS", "AS", "LIKE", "MY", "THE", "REG", "REGO",
           "REGISTRATION", "PLATE", "NUMBER", "UH", "UM", "ER", "AH",
           "PLEASE", "THANKS", "THANK", "YOU", "OK", "OKAY", "YEAH", "YES",
           # Hedging. A caller spelling a plate is often unsure, and these are
           # how that sounds: "AB12CDE, I think". Without them the honest hedge
           # is treated as part of the plate and validation fails, so the flow
           # asks the caller to repeat something they said correctly.
           "THINK", "THOUGHT", "MAYBE", "BELIEVE", "SURE", "SORRY",
           "ACTUALLY", "PROBABLY", "PERHAPS", "GUESS", "HMM", "WELL",
           "JUST", "RIGHT", "CORRECT", "KNOW", "REMEMBER", "SUPPOSE",
           "RECKON", "AFRAID", "PARDON"}

# Multi-word hedges, removed *before* tokenising rather than during it.
#
# This cannot be a `_FILLER` entry. Tokenised, "I think" is a bare "I" followed
# by "THINK", and a single character is claimed by `_char_token` before the
# filler check ever runs — so "I" would be appended as the letter I. The phrase
# has to go while it is still a phrase. (Found by testing what callers actually
# say, not what the masks suggest they say.)
_HEDGE_PHRASES = [
    r"I\s+THINK", r"I\s+THOUGHT", r"I\s+BELIEVE", r"I\s+GUESS",
    r"I\s+SUPPOSE", r"I\s+RECKON", r"I\s+AM\s+NOT\s+SURE",
    r"I\s*'?M\s+NOT\s+SURE", r"I\s*'?M\s+SURE",
    r"NOT\s+SURE", r"NOT\s+CERTAIN", r"IS\s+THAT\s+RIGHT",
    r"IF\s+THAT\s+HELPS", r"IT\s*'?S", r"THIS\s+IS",
]
_HEDGE_RE = re.compile(r"\b(?:" + "|".join(_HEDGE_PHRASES) + r")\b")


def _char_token(token: str) -> str | None:
    """Map one spoken token to a single character, if it is one."""
    if token in _NATO_UPPER:
        return _NATO_UPPER[token]
    if len(token) == 1 and token.isalnum():
        return token
    return None


def _parse_number(tokens: list[str], index: int) -> tuple[int | None, int]:
    """Parse a spoken number starting at `index`.

    Handles "twelve", "two oh", "twenty one", "one hundred and twenty".
    Returns (value, tokens_consumed).
    """
    token = tokens[index]
    if token.isdigit():
        return int(token), 1

    if token in _UNITS:
        total = _UNITS[token]
        consumed = 1
        # "two oh eight" -> 208, "two hundred" -> 200
        while index + consumed < len(tokens) and consumed < 4:
            nxt = tokens[index + consumed]
            if nxt in _UNITS and _UNITS[nxt] == 0:
                total *= 10
                consumed += 1
            elif nxt in _SCALES:
                total *= _SCALES[nxt]
                consumed += 1
            else:
                break
        return total, consumed

    if token in _TENS:
        total = _TENS[token]
        consumed = 1
        if index + 1 < len(tokens) and tokens[index + 1] in _UNITS:
            unit = _UNITS[tokens[index + 1]]
            if 0 < unit < 10:
                total += unit
                consumed = 2
        return total, consumed

    return None, 0


def _normalise_pass(raw: str, strip_filler: bool) -> str:
    """One normalisation strategy.

    Filler removal is a *separate strategy* rather than an unconditional step,
    because dropping a word like "for" would corrupt a plate that genuinely
    ends in those letters. Validation tries the conservative pass first.
    """
    if not raw:
        return ""
    text = raw.upper()
    # Collapse the spoken idiom "A for Alpha" to just "Alpha".
    #
    # Callers say the letter and then its phonetic name, because that is how
    # you spell something unambiguously out loud. Keeping both would double the
    # character ("A for Alpha" -> "AALPHA" -> "AA"), so the joining word and
    # the bare letter are dropped and the phonetic name is kept.
    text = re.sub(r"\b[A-Z0-9]\s+(?:FOR|IS|AS|LIKE)\s+([A-Z]+)\b", r"\1", text)
    if strip_filler:
        # Only in the fallback pass. A plate genuinely containing these words as
        # letter runs is matched by the conservative pass first.
        text = _HEDGE_RE.sub(" ", text)
    text = re.sub(r"[^A-Z0-9]+", " ", text)

    tokens = text.split()
    out: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]

        if token in _REPEAT and index + 1 < len(tokens):
            char = _char_token(tokens[index + 1])
            if char:
                out.append(char * _REPEAT[token])
                index += 2
                continue

        value, consumed = _parse_number(tokens, index)
        # Only a *word* is trusted as a number. A bare digit token is kept
        # verbatim so "12" is not re-parsed as two separate characters.
        if value is not None and not token.isdigit() and consumed:
            out.append(str(value))
            index += consumed
            continue

        char = _char_token(token)
        if char:
            out.append(char)
            index += 1
            continue

        if strip_filler and token in _FILLER:
            index += 1
            continue

        out.append(token)
        index += 1

    return "".join(out)


def normalise(raw: str) -> str:
    """The conservative canonical form of a spoken identifier."""
    return _normalise_pass(raw, strip_filler=False)


def normalise_variants(raw: str) -> list[str]:
    """Candidate normalisations, most faithful first.

    Validation tries each against the country masks. Trying candidates beats
    guessing: the conservative reading wins whenever it is valid, and the
    aggressive one is only used to rescue a string that would otherwise fail.
    """
    variants = [_normalise_pass(raw, strip_filler=False),
                _normalise_pass(raw, strip_filler=True)]
    seen: list[str] = []
    for variant in variants:
        if variant and variant not in seen:
            seen.append(variant)
    return seen


def _fits_mask(value: str, mask: str) -> bool:
    if len(value) != len(mask):
        return False
    for char, slot in zip(value, mask):
        if slot == "L" and not char.isalpha():
            return False
        if slot == "D" and not char.isdigit():
            return False
    return True


def _apply_mask(value: str, mask: str) -> tuple[str, list[str]]:
    """Coerce characters into the shape the mask requires.

    Only *known* ASR confusions are repaired, and each one is reported. This is
    deliberately conservative: repairing "O"->"0" where a digit belongs is not a
    guess, it is the only thing the string could have been. Anything else stays
    invalid and goes back to the caller.
    """
    out: list[str] = []
    coercions: list[str] = []
    for char, slot in zip(value, mask):
        if slot == "D" and char.isalpha():
            replacement = _LETTER_TO_DIGIT.get(char)
            if replacement is None:
                return "", []
            out.append(replacement)
            coercions.append(f"{char}->{replacement} at position {len(out)}")
        elif slot == "L" and char.isdigit():
            replacement = _DIGIT_TO_LETTER.get(char)
            if replacement is None:
                return "", []
            out.append(replacement)
            coercions.append(f"{char}->{replacement} at position {len(out)}")
        else:
            out.append(char)
    return "".join(out), coercions


def _group(segments: str) -> list[str]:
    """Group a template into runs of like characters: 'LLDDLLL' -> ['LL','DD','LLL']."""
    if not segments:
        return []
    groups: list[str] = []
    run = segments[0]
    for slot in segments[1:]:
        if slot == run[-1]:
            run += slot
        else:
            groups.append(run)
            run = slot
    groups.append(run)
    return groups


def readback_grouped(value: str, mask: str) -> str:
    """Produce a grouped, phonetic read-back.

    'AB12CDE' with mask 'LLDDLLL' becomes
    "A for Alpha, B for Bravo, one, two, C for Charlie, D for Delta, E for Echo".

    Reading a plate back as a run of sounds repeats the ambiguity; spelling it
    phonetically forces every character into an unambiguous word and gives the
    caller a clear point to correct.
    """
    chunks: list[str] = []
    cursor = 0
    for group in _group(mask):
        piece = value[cursor : cursor + len(group)]
        spoken: list[str] = []
        for char, slot in zip(piece, group):
            if slot == "L":
                spoken.append(f"{char} for {_NATO.get(char, char)}")
            else:
                spoken.append(_DIGIT_WORDS.get(char, char))
        if group[0] == "D":
            # "one two" collides with "twelve"; a pause marker keeps it distinct.
            chunks.append(" ".join(spoken))
        else:
            chunks.append(", ".join(spoken))
        cursor += len(group)
    return " …  ".join(chunks)


# --------------------------------------------------------------------------- #
# Validators
# --------------------------------------------------------------------------- #

_VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
_VIN_VALUES = {
    **{str(d): d for d in range(10)},
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def validate_plate(raw: str, country: str = "GB",
                   masks: list[str] | None = None) -> ValidationResult:
    """Validate a registration plate against the country's format mask."""
    result = ValidationResult(kind="license_plate", raw=raw, country=country)
    variants = normalise_variants(raw)
    if not variants:
        result.reason = "Nothing usable was captured — ask the caller to spell it."
        return result
    result.normalised = variants[0]

    candidate_masks = masks or PLATE_MASKS.get(country.upper(), [])
    if not candidate_masks:
        result.reason = f"No plate format configured for country {country!r}."
        return result

    # Pass 1 — exact match on the most faithful normalisation. Most specific
    # mask first, so "LLDDLLL" is preferred over a shorter overlapping format.
    for value in variants:
        for mask in sorted(candidate_masks, key=len, reverse=True):
            if _fits_mask(value, mask):
                result.normalised = value
                result.valid = True
                result.matched_mask = mask
                result.confidence = "high"
                result.reason = "Matches the country format exactly."
                result.readback = readback_grouped(value, mask)
                return result

    # Pass 2 — repair known ASR confusions. Only characters the mask proves are
    # wrong are changed, and every change is reported so the read-back can
    # target them instead of re-reading the whole plate.
    best: ValidationResult | None = None
    refusal_reason: str | None = None
    for value in variants:
        for mask in sorted(candidate_masks, key=len, reverse=True):
            if len(value) != len(mask):
                continue
            repaired, coercions = _apply_mask(value, mask)
            if not (repaired and coercions and _fits_mask(repaired, mask)):
                continue
            if len(coercions) > MAX_COERCIONS:
                # Too much of the string had to be changed. Presenting that to
                # the caller as a plate would be guessing dressed up as
                # validation, so refuse and re-capture instead.
                refusal_reason = (
                    f"{len(coercions)} characters had to be repaired, which is "
                    "too many to be trustworthy. Ask the caller to spell the "
                    "whole plate again, character by character."
                )
                continue
            candidate = ValidationResult(
                kind="license_plate", raw=raw, country=country,
                normalised=repaired, matched_mask=mask, coercions=coercions,
                uncertain_positions=sorted(
                    {int(c.rsplit(" ", 1)[-1]) for c in coercions}
                ),
                # Repaired characters are plausible, not certain: the flow
                # must confirm these specific positions with the caller.
                valid=False, confidence="medium",
                reason=("Plausible after repairing likely misheard characters, "
                        "but the repaired positions must be confirmed with "
                        "the caller."),
                readback=readback_grouped(repaired, mask),
                suggestions=[repaired],
            )
            if best is None or len(candidate.coercions) < len(best.coercions):
                best = candidate
        if best is not None:
            return best

    if refusal_reason is not None:
        result.reason = refusal_reason
        return result

    result.reason = (
        "Does not match any configured format for this country. "
        "Do not guess — spell it back phonetically and re-capture."
    )
    return result


def validate_vin(raw: str) -> ValidationResult:
    """Validate a VIN: 17 chars, no I/O/Q, plus the ISO 3779 check digit."""
    result = ValidationResult(kind="vin", raw=raw)
    value = normalise(raw)
    result.normalised = value
    if not value:
        result.reason = "Nothing usable was captured."
        return result
    if len(value) != 17:
        result.reason = f"A VIN is 17 characters; {len(value)} were captured."
        return result
    if any(c in value for c in "IOQ"):
        result.reason = "A VIN never contains the letters I, O or Q — re-capture."
        return result
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", value):
        result.reason = "Contains characters that are not valid in a VIN."
        return result

    total = sum(_VIN_VALUES.get(c, 0) * w for c, w in zip(value, _VIN_WEIGHTS))
    remainder = total % 11
    expected = "X" if remainder == 10 else str(remainder)
    actual = value[8]
    result.valid = True
    result.matched_mask = "VIN17"
    result.confidence = "high"
    if expected == actual:
        result.reason = "Structure valid and the check digit agrees."
    else:
        # European VINs often do not honour the North-American check digit, so
        # this is informational: it *detects* an invalid string but cannot
        # reveal the correct one. Reconcile against the record.
        result.confidence = "medium"
        result.uncertain_positions = [8]
        result.reason = (
            "Structure valid, but the check digit disagrees. In the EU this is "
            "not always an error — reconcile against the vehicle record."
        )
    result.readback = readback_grouped(value, "L" * 17)
    return result


def validate_phone(raw: str, default_country_code: str = "+44") -> ValidationResult:
    """Loose E.164 validation. Phones have low ambiguity but high blast radius."""
    result = ValidationResult(kind="phone", raw=raw)
    value = normalise(raw)
    result.normalised = value
    if not value:
        result.reason = "No number captured."
        return result
    if not value.startswith("+"):
        value = default_country_code + value.lstrip("0")
        result.normalised = value
    if _E164.fullmatch(value):
        result.valid = True
        result.confidence = "high"
        result.reason = "Valid E.164 number."
        result.readback = " ".join(value[1:].lstrip("0"))
    else:
        result.reason = "Not a valid international number — confirm digit by digit."
    result.needs_lookup = False
    return result


def validate_mileage(raw: str, max_plausible: int = 500_000) -> ValidationResult:
    """Mileage is numeric and bounded — worth a sanity check before pricing."""
    result = ValidationResult(kind="mileage", raw=raw)
    value = normalise(raw)
    result.normalised = value
    digits = re.sub(r"[^0-9]", "", value)
    if not digits:
        result.reason = "Mileage must be a number."
        return result
    number = int(digits)
    result.normalised = str(number)
    if number > max_plausible:
        result.reason = f"{number} looks implausible for an odometer reading."
        result.confidence = "medium"
        result.uncertain_positions = list(range(len(digits)))
        return result
    result.valid = True
    result.confidence = "high"
    result.reason = "Plausible odometer reading."
    result.readback = f"{number:,}".replace(",", " ")
    result.needs_lookup = False
    return result


def validate_email(raw: str) -> ValidationResult:
    result = ValidationResult(kind="email", raw=raw)
    value = raw.strip().lower().replace(" at ", "@").replace(" dot ", ".")
    value = re.sub(r"\s+", "", value)
    result.normalised = value
    if _EMAIL.fullmatch(value):
        result.valid = True
        result.confidence = "high"
        result.reason = "Valid email address."
        result.readback = value
    else:
        result.reason = "Not a valid email — spell it back and confirm."
    result.needs_lookup = False
    return result


_VALIDATORS = {
    "license_plate": validate_plate,
    "vin": validate_vin,
    "phone": validate_phone,
    "mileage": validate_mileage,
    "email": validate_email,
}


def validate_identifier(kind: IdentifierKind, value: str,
                        country: str = "GB") -> dict[str, Any]:
    """Entry point used by the flow's tool layer.

    Returns a flat dict of strings so it can be mapped straight onto Retell
    dynamic variables and branched on with an equation edge.
    """
    validator = _VALIDATORS.get(kind)
    if validator is None:
        return ValidationResult(kind=kind, raw=value,
                                reason=f"Unknown identifier kind {kind!r}.").to_dict()
    if kind == "license_plate":
        return validator(value, country=country).to_dict()
    return validator(value).to_dict()
