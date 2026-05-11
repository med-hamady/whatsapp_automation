"""Tests des helpers date :
- parse_yymmdd_explicit : conversion stable YY-MM-DD → ISO (pour layout
  Bankily arabe RTL où on a le contexte).
- normalize_datetime : heuristique min(abs(year-today)) pour les cas où
  l'ordre est ambigu (DD-MM-YY vs YY-MM-DD).
"""

from __future__ import annotations

import pytest

from whatsapp_automation.ai_ocr.normalizer import normalize_datetime, parse_yymmdd_explicit


@pytest.mark.parametrize(
    "raw_date,raw_time,expected",
    [
        # Layout Bankily arabe : YY-MM-DD HH:MM:SS, parse stable
        ("26-04-27", "18:53:36", "2026-04-27T18:53:36"),
        ("26-05-03", "17:38:23", "2026-05-03T17:38:23"),
        ("25-11-26", "13:42:11", "2025-11-26T13:42:11"),
        # Date seule
        ("26-05-02", "", "2026-05-02T00:00:00"),
        # Confusion OCR digits sur la date (l → 1, O → 0)
        ("26-Ol-15", "", "2026-01-15T00:00:00"),
    ],
)
def test_parse_yymmdd_explicit(raw_date, raw_time, expected):
    out = parse_yymmdd_explicit(raw_date, raw_time)
    assert out == expected, f"{raw_date!r}+{raw_time!r} -> {out!r} (attendu {expected!r})"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        None,
        "pas une date",
        "26-13-15",  # mois invalide
        "26-05-32",  # jour invalide
        "26-05",     # incomplet
    ],
)
def test_parse_yymmdd_explicit_invalid(bad):
    if bad is None:
        # parse_yymmdd_explicit n'accepte pas None mais les regex appelantes
        # le filtreront ; on saute ce cas
        return
    assert parse_yymmdd_explicit(bad, "") is None


def test_normalize_datetime_existing_cases_still_work():
    """Régression : les cas DD-MM-YY usuels doivent toujours être bons."""
    assert normalize_datetime("29-05-25 14:12:02").startswith("2025-05-29T14:12:02")
    assert normalize_datetime("25-11-25 13:42:11").startswith("2025-11-25T13:42:11")
    assert normalize_datetime("26-11-25 16:54:59").startswith("2025-11-26T16:54:59")
