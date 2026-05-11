import pytest

from whatsapp_automation.webhook.validators import (
    should_unblock_client,
    validate_amount,
    validate_client,
    validate_crm_balance,
    validate_extraction,
)


def test_validate_extraction_ok():
    assert validate_extraction({"montant": 1500, "txn_id": "xx"}).ok


def test_validate_extraction_no_amount():
    r = validate_extraction({"montant": None})
    assert not r.ok and r.reason == "no_or_invalid_amount"


def test_validate_extraction_negative():
    r = validate_extraction({"montant": -10})
    assert not r.ok


def test_validate_client_none():
    r = validate_client(None)
    assert not r.ok and r.reason == "client_not_found"


def test_validate_client_not_suspended():
    r = validate_client({"statu": 0, "mac": "AA:BB:CC:DD:EE:FF"})
    assert not r.ok and r.reason == "client_not_suspended"


def test_validate_client_no_mac():
    r = validate_client({"statu": 1, "mac": ""})
    assert not r.ok and r.reason == "client_has_no_mac"


def test_validate_client_ok():
    r = validate_client({"statu": 1, "mac": "AA:BB:CC:DD:EE:FF"})
    assert r.ok


def test_validate_amount():
    assert validate_amount(100).ok
    assert not validate_amount(0).ok
    assert not validate_amount(-5).ok


def test_validate_crm_balance_positive():
    assert validate_crm_balance(1500).ok


def test_validate_crm_balance_zero_means_paid_up():
    r = validate_crm_balance(0)
    assert not r.ok and r.reason == "client_already_paid_up"


def test_validate_crm_balance_negative_means_paid_up():
    r = validate_crm_balance(-100)
    assert not r.ok and r.reason == "client_already_paid_up"


def test_validate_crm_balance_none_blocks():
    """CRM injoignable : on ne peut pas décider, donc on bloque."""
    r = validate_crm_balance(None)
    assert not r.ok and r.reason == "crm_unreachable"


@pytest.mark.parametrize("balance,paid,expected_unblock", [
    (1500, 1500, True),    # paiement exact → unblock
    (1500, 1600, True),    # sur-paiement → unblock (avoir crédit)
    (1500, 1400, True),    # sous-paiement 100 MRU (≤ 150) → unblock
    (1500, 1350, True),    # sous-paiement 150 MRU (= seuil) → unblock
    (1500, 1349, False),   # sous-paiement 151 MRU (> seuil) → NO unblock
    (1500, 1000, False),   # sous-paiement 500 MRU → NO unblock
    (2000, 500, False),    # gros sous-paiement → NO unblock
])
def test_should_unblock_client(balance, paid, expected_unblock):
    assert should_unblock_client(amount_paid=paid, crm_balance=balance, threshold=150) is expected_unblock
