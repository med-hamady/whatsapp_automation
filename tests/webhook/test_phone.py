import pytest

from whatsapp_automation.webhook.phone import parse_from_field, parse_body_number


@pytest.mark.parametrize("raw,expected", [
    ("22237697850@c.us", "37697850"),
    ("22233848414@c.us", "33848414"),
    ("22249593871", "49593871"),
    ("37697850", "37697850"),
    ("", ""),
    ("not-a-number", ""),
])
def test_parse_from_field(raw, expected):
    assert parse_from_field(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Bonjour mon numero 37697850", "37697850"),
    ("Voici 22237697850 pour mon ami", "37697850"),
    ("Aucun chiffre ici", ""),
])
def test_parse_body_number(raw, expected):
    assert parse_body_number(raw) == expected
