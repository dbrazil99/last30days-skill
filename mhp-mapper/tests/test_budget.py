import pytest

from mhp_mapper import budget


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MHP_DATA_DIR", str(tmp_path))
    for k in ["MHP_CAP_TOTAL_USD", "MHP_CAP_CLAUDE_USD", "MHP_CAP_GOOGLE_USD", "MHP_CAP_GOOGLE_CALLS",
              "MHP_CAP_PARCEL_USD", "MHP_CAP_PARCEL_CALLS", "MHP_PARCEL_USD_PER_CALL"]:
        monkeypatch.delenv(k, raising=False)


def test_google_stays_in_free_tier():
    for _ in range(4500):
        budget.guard("google_places")
    with pytest.raises(budget.BudgetExceeded):
        budget.guard("google_places")
    assert budget.report()["spent"]["google_places"]["usd"] == 0


def test_google_paid_calls_blocked_by_dollar_cap(monkeypatch):
    monkeypatch.setenv("MHP_CAP_GOOGLE_CALLS", "10000")
    monkeypatch.setenv("MHP_CAP_GOOGLE_USD", "0")
    for _ in range(5000):
        budget.guard("google_places")
    with pytest.raises(budget.BudgetExceeded):  # call 5,001 would cost money
        budget.guard("google_places")


def test_claude_cap_and_settlement(monkeypatch):
    monkeypatch.setenv("MHP_CAP_CLAUDE_USD", "0.20")
    budget.guard("claude")
    actual = budget.settle_claude("claude-opus-5-5", 3000, 1000)  # $0.012 + $0.02
    assert abs(actual - 0.032) < 1e-9
    assert abs(budget.report()["spent"]["claude"]["usd"] - 0.032) < 1e-4
    budget.guard("claude")
    budget.guard("claude")
    with pytest.raises(budget.BudgetExceeded):
        budget.guard("claude")


def test_total_cap(monkeypatch):
    monkeypatch.setenv("MHP_CAP_TOTAL_USD", "0.10")
    monkeypatch.setenv("MHP_CAP_CLAUDE_USD", "100")
    budget.guard("claude")
    with pytest.raises(budget.BudgetExceeded):
        budget.guard("claude")


def test_parcel_trial_cap(monkeypatch):
    monkeypatch.setenv("MHP_CAP_PARCEL_CALLS", "3")
    for _ in range(3):
        budget.guard("parcel_api")
    with pytest.raises(budget.BudgetExceeded):
        budget.guard("parcel_api")
