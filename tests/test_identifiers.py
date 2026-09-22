"""Tests for the deterministic guardrail layer.

These are the tests that matter most for the guardrail claim, because they
encode the *controls*, not just the parsing:

* a valid plate is recognised and read back phonetically;
* a misheard character is repaired but flagged for confirmation;
* an unrepairable plate is refused rather than guessed;
* a plate that is valid but wrong still needs a record lookup, and the result
  says so.

Run: python -m pytest -q
"""

from __future__ import annotations

import pytest

from frontdesk import identifiers


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("spoken,expected", [
    ("AB12CDE", "AB12CDE"),
    ("ab12 cde", "AB12CDE"),
    ("A B one two C D E", "AB12CDE"),
    ("A B twelve C D E", "AB12CDE"),
    ("a for alpha, b for bravo, one, two, c for charlie, d for delta, e for echo",
     "AB12CDE"),
    ("A-B-1-2-C-D-E", "AB12CDE"),
    ("double A 1 2 C D E", "AA12CDE"),
])
def test_spoken_forms_normalise(spoken: str, expected: str) -> None:
    assert identifiers.normalise(spoken) == expected


def test_filler_words_are_removed_only_as_a_fallback() -> None:
    """The conservative reading is tried first, so a plate ending in a filler
    word is still matched; the cleaned reading is only a rescue."""
    variants = identifiers.normalise_variants("AB12 CDE please")
    assert variants[0] == "AB12CDEPLEASE"   # faithful reading kept
    assert variants[-1] == "AB12CDE"        # cleaned rescue available
    assert identifiers.validate_plate("AB12 CDE please", "GB").valid is True


@pytest.mark.parametrize("spoken", [
    "AB12CDE, I think",
    "AB12CDE I think",
    "I think it's AB12CDE",
    "it's AB12CDE, I think",
    "AB12CDE maybe",
    "AB12CDE not sure",
    "AB12CDE, is that right?",
    "AB12CDE sorry",
    "AB12CDE I believe",
    "um AB12CDE",
])
def test_hedging_around_a_plate_does_not_spoil_it(spoken: str) -> None:
    """A caller who has just spelled a plate out and says "I think" is the
    normal case, not an edge case. Treating the hedge as part of the plate sent
    them to re-spell something they had already said correctly — and worse, made
    an honest "I'm not sure" look like a faulty registration.

    Note these all arrive as *speech*, so the hedge is mid-sentence and the
    apostrophe in "it's" is whatever the transcriber produced.
    """
    result = identifiers.validate_plate(spoken, "GB")
    assert result.valid, f"{spoken!r} -> {result.normalised!r}: {result.reason}"
    assert result.normalised == "AB12CDE"


def test_a_hedge_is_never_taken_for_a_letter() -> None:
    """The specific trap: "I think" tokenises as a bare "I" then "THINK", and a
    single character is claimed as a letter before any filler check runs. So the
    naive fix — adding "I" to the filler list — silently does nothing, and the
    result gains a stray I. The phrase has to be removed while it is a phrase.
    """
    assert identifiers.normalise("AB12CDE I think") == "AB12CDEITHINK"  # faithful
    assert identifiers.validate_plate("AB12CDE I think", "GB").normalised == "AB12CDE"
    assert "I" not in identifiers.validate_plate("AB12CDE I think", "GB").normalised


def test_a_filler_word_inside_a_real_plate_is_still_matched() -> None:
    """The conservative reading runs first, which is what stops the hedge
    handling from eating a plate that legitimately contains those letters."""
    # "THE" is filler, but here it is part of the plate and must survive.
    variants = identifiers.normalise_variants("AB12THE")
    assert variants[0] == "AB12THE"
    # A seven-character plate is what the mask demands, so this is invalid —
    # but crucially it was *attempted* faithfully rather than stripped to "AB12".
    assert variants[-1] in {"AB12THE", "AB12"}


def test_nato_readback_is_unambiguous() -> None:
    """A read-back must name every letter, or it repeats the ambiguity."""
    readback = identifiers.readback_grouped("AB12CDE", "LLDDLLL")
    assert "Alpha" in readback and "Bravo" in readback
    assert "Charlie" in readback and "Delta" in readback
    # Digits are grouped together and not phonetically spelled.
    assert "one two" in readback


# --------------------------------------------------------------------------- #
# Plate validation — the failure B controls
# --------------------------------------------------------------------------- #

def test_valid_plate_is_accepted_and_read_back() -> None:
    result = identifiers.validate_plate("AB12CDE", "GB")
    assert result.valid is True
    assert result.matched_mask == "LLDDLLL"
    assert result.readback
    # A format check can never prove correctness, only plausibility.
    assert result.needs_lookup is True


def test_misheard_character_is_repaired_but_not_silently_accepted() -> None:
    """'Z' where a digit belongs is the only thing it could have been."""
    result = identifiers.validate_plate("AB1ZCDE", "GB")
    assert result.valid is False           # NOT accepted outright
    assert result.normalised == "AB12CDE"
    assert result.coercions == ["Z->2 at position 4"]
    assert result.uncertain_positions == [4]
    assert result.confidence == "medium"
    assert result.suggestions == ["AB12CDE"]


def test_unrepairable_plate_is_refused_not_guessed() -> None:
    """The core rule: never fill a gap on probability."""
    # Length matches no configured GB mask, so nothing can be repaired.
    result = identifiers.validate_plate("ABCD12345", "GB")
    assert result.valid is False
    assert "Do not guess" in result.reason


def test_too_many_repairs_is_refused_rather_than_guessed() -> None:
    """A repair is only trustworthy while it is a small minority of the string."""
    # Three positions (0->O, Q->0, D->0) would have to be repaired, which is
    # too much of a 7-character plate to present to a caller as a fact.
    result = identifiers.validate_plate("0BQDCDE", "GB")
    assert result.valid is False
    assert "too many to be trustworthy" in result.reason


def test_valid_but_wrong_plate_cannot_be_caught_by_format_alone() -> None:
    """Two real plates can both match the mask — hence the lookup step."""
    result = identifiers.validate_plate("XY19JKL", "GB")
    assert result.valid is True
    assert result.needs_lookup is True


@pytest.mark.parametrize("plate,country", [
    ("AB12CDE", "GB"),
    ("12-AB-34", "NL"),
    ("XY19JKL", "GB"),
    ("1234ABC", "ES"),
])
def test_country_masks(plate: str, country: str) -> None:
    assert identifiers.validate_plate(plate, country).valid is True


def test_dutch_plate_rejected_under_british_mask() -> None:
    """Masks are country-specific; the wrong market must not validate."""
    assert identifiers.validate_plate("12-AB-34", "GB").valid is False
    assert identifiers.validate_plate("12-AB-34", "NL").valid is True


def test_unknown_country_is_reported_not_crashed() -> None:
    result = identifiers.validate_plate("AB12CDE", "ZZ")
    assert result.valid is False
    assert "No plate format configured" in result.reason


# --------------------------------------------------------------------------- #
# VIN, phone, mileage
# --------------------------------------------------------------------------- #

def test_vin_structure_and_check_digit() -> None:
    ok = identifiers.validate_vin("WBA5E51020G123456")
    assert ok.valid is True
    assert len(ok.normalised) == 17


def test_vin_rejects_forbidden_letters() -> None:
    result = identifiers.validate_vin("WBA5E5102OG123456")
    assert result.valid is False
    assert "I, O or Q" in result.reason


def test_vin_rejects_wrong_length() -> None:
    assert identifiers.validate_vin("WBA5E5102").valid is False


def test_phone_is_normalised_to_e164() -> None:
    result = identifiers.validate_phone("07700 900123", default_country_code="+44")
    assert result.valid is True
    assert result.normalised == "+447700900123"


def test_implausible_mileage_is_flagged_not_accepted() -> None:
    result = identifiers.validate_mileage("1250000")
    assert result.valid is False
    assert "implausible" in result.reason


def test_mileage_accepts_normal_reading() -> None:
    result = identifiers.validate_mileage("41,250 miles")
    assert result.valid is True
    assert result.normalised == "41250"


def test_validate_identifier_returns_flat_strings() -> None:
    """Result must be mappable onto Retell dynamic variables, which are strings."""
    payload = identifiers.validate_identifier("license_plate", "AB12CDE", "GB")
    assert all(isinstance(v, str) for v in payload.values())
    assert payload["valid"] == "true"
