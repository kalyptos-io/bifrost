"""normalizer unit tests: each capability + idempotency. no train import (app must not import train)."""  # noqa: E501

from bifrost.arms.normalize import fold, normalize


def test_url_decode_all_variants() -> None:
    # every variant train/gen's mutate._URL_VARIANTS emits must decode to the same canonical form
    assert normalize("%C3%B8stergade") == "oestergade"  # standard upper hex
    assert normalize("%c3%b8stergade") == "oestergade"  # lowercase hex
    assert normalize("%25C3%25B8stergade") == "oestergade"  # double-encoded (%2520-style)
    assert normalize("vester+gade") == "vester gade"  # + for space (form-encoding)
    assert normalize("vester%20gade") == "vester gade"  # partial: spaces only
    assert normalize("9800%20Hj%C3%B8rring") == "9800 hjoerring"  # url-encoded partial


def test_mojibake_repair() -> None:
    assert normalize("Ã¸stergade") == "oestergade"  # utf-8 shown as latin-1
    assert normalize("Ã¥rhus") == "aarhus"
    assert normalize("Ã\x85rhus") == "aarhus"  # uppercase danish (C1 byte) repairs too
    assert normalize("Søvej") == "soevej"  # already-correct text is left untouched


def test_fold_is_canonical_and_case_preserving() -> None:
    assert fold("Teglværksvej") == "Teglvaerksvej"
    assert fold("Århus") == "Aarhus"  # å -> aa (NOT a, the NFKD-only result)
    assert fold("Allé") == "Alle"


def test_fold_reconciles_both_spellings() -> None:
    assert normalize("TEGLVAERKSVEJ") == normalize("Teglværksvej") == "teglvaerksvej"
    assert normalize("Århus") == "aarhus" != "arhus"
    assert normalize("Nørrebrogade") == "noerrebrogade"


def test_recipient_stripping() -> None:
    assert normalize("Østerbro 5, c/o Hans Jensen") == "oesterbro 5"
    assert normalize("att: H Hansen, Vestergade 5, 9800 Hjørring") == "vestergade 5 9800 hjoerring"
    assert normalize("v/ Firma ApS, Algade 5") == "algade 5"
    assert normalize("Attemosevej 5") == "attemosevej 5"  # marker word-boundary, no false strip


def test_recipient_keeps_housenumber_segment() -> None:
    # keep the segment containing a house-number even when it carries a marker
    assert normalize("Algade 5 att Hans, 9800") == "algade 5 att hans 9800"


def test_punct_and_whitespace() -> None:
    assert normalize("Vester-gade  5 .") == "vester gade 5"
    assert normalize("  Algade   5  ") == "algade 5"


def test_idempotent() -> None:
    inputs = [
        "%C3%B8stergade",
        "Østerbro 5, c/o Hans",
        "TEGLVÆRKSVEJ",
        "Ã¸stergade",
        "Vester-gade 5",
    ]
    for q in inputs:
        once = normalize(q)
        assert normalize(once) == once


def test_strip_leading_zeros() -> None:
    assert normalize("Vestre Strandvej 030, 5450 Otterup") == "vestre strandvej 30 5450 otterup"
    assert normalize("Lindholmsvej 24, 0003") == "lindholmsvej 24 3"  # padded door -> bare
    assert normalize("Usnapvej 4, 01") == "usnapvej 4 1"  # padded floor -> bare
    assert normalize("Strandvej 130, 5450") == "strandvej 130 5450"  # postcode + husnr untouched


def test_empty() -> None:
    assert normalize("") == ""
    assert normalize("   ") == ""


def test_corpus_stable() -> None:
    # pins folded output per capability class; the url/mojibake fast-path guards stay bit-identical
    corpus = {
        "Vestergade 5 9800": "vestergade 5 9800",  # plain ascii, no url/mojibake markers
        "%C3%B8stergade 5": "oestergade 5",  # url-encoded
        "vester+gade": "vester gade",  # + decodes to space
        "Ã¸stergade": "oestergade",  # mojibake
        "Søndervej 12, 8000 Århus": "soendervej 12 8000 aarhus",  # æøå
        "att H, %C3%86blevej 5": "aeblevej 5",  # mixed: recipient marker + url-encoded digraph
    }
    for raw, want in corpus.items():
        assert normalize(raw) == want
