"""Pull portfolio data from Plaid and normalise into clean Python structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from src.plaid_client import PlaidClient


@dataclass
class Security:
    security_id: str
    ticker: str
    name: str
    asset_type: str  # equity, mutual fund, etf, fixed income, cash, …
    close_price: Decimal | None
    close_price_as_of: date | None
    iso_currency_code: str = "USD"


@dataclass
class Holding:
    account_id: str
    security_id: str
    ticker: str
    name: str
    quantity: Decimal
    institution_price: Decimal
    institution_value: Decimal
    cost_basis: Decimal | None
    iso_currency_code: str = "USD"

    @property
    def unrealised_gain(self) -> Decimal | None:
        if self.cost_basis is None:
            return None
        return self.institution_value - self.cost_basis

    @property
    def unrealised_gain_pct(self) -> Decimal | None:
        if self.cost_basis is None or self.cost_basis == 0:
            return None
        return (self.institution_value - self.cost_basis) / self.cost_basis


@dataclass
class Account:
    account_id: str
    name: str
    official_name: str | None
    account_type: str
    subtype: str
    balances_current: Decimal | None
    balances_available: Decimal | None
    iso_currency_code: str = "USD"


@dataclass
class InvestmentTransaction:
    investment_transaction_id: str
    account_id: str
    security_id: str | None
    ticker: str | None
    name: str
    date: date
    quantity: Decimal
    amount: Decimal
    price: Decimal
    fees: Decimal | None
    transaction_type: str  # buy, sell, dividend, …
    subtype: str
    iso_currency_code: str = "USD"


@dataclass
class PortfolioSnapshot:
    """All data for a single point-in-time portfolio pull."""

    fetched_at: datetime
    accounts: list[Account] = field(default_factory=list)
    holdings: list[Holding] = field(default_factory=list)
    securities: dict[str, Security] = field(default_factory=dict)
    transactions: list[InvestmentTransaction] = field(default_factory=list)

    @property
    def total_value(self) -> Decimal:
        return sum(h.institution_value for h in self.holdings)

    @property
    def holdings_by_ticker(self) -> dict[str, list[Holding]]:
        result: dict[str, list[Holding]] = {}
        for h in self.holdings:
            result.setdefault(h.ticker or "UNKNOWN", []).append(h)
        return result


# ── Normalisation helpers ─────────────────────────────────────────────────────


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _securities_map(raw_securities: list[dict]) -> dict[str, Security]:
    result: dict[str, Security] = {}
    for s in raw_securities:
        sid = s.get("security_id", "")
        result[sid] = Security(
            security_id=sid,
            ticker=s.get("ticker_symbol") or s.get("name") or sid,
            name=s.get("name") or sid,
            asset_type=s.get("type") or "unknown",
            close_price=_dec(s.get("close_price")),
            close_price_as_of=(
                date.fromisoformat(str(s["close_price_as_of"]))
                if s.get("close_price_as_of")
                else None
            ),
            iso_currency_code=s.get("iso_currency_code") or "USD",
        )
    return result


def _parse_accounts(raw_accounts: list[dict]) -> list[Account]:
    accounts = []
    for a in raw_accounts:
        bal = a.get("balances") or {}
        accounts.append(
            Account(
                account_id=a["account_id"],
                name=a.get("name") or "",
                official_name=a.get("official_name"),
                account_type=a.get("type") or "",
                subtype=a.get("subtype") or "",
                balances_current=_dec(bal.get("current")),
                balances_available=_dec(bal.get("available")),
                iso_currency_code=bal.get("iso_currency_code") or "USD",
            )
        )
    return accounts


def _parse_holdings(
    raw_holdings: list[dict], securities: dict[str, Security]
) -> list[Holding]:
    holdings = []
    for h in raw_holdings:
        sid = h.get("security_id", "")
        sec = securities.get(sid)
        holdings.append(
            Holding(
                account_id=h.get("account_id") or "",
                security_id=sid,
                ticker=(sec.ticker if sec else sid),
                name=(sec.name if sec else sid),
                quantity=_dec(h.get("quantity")) or Decimal("0"),
                institution_price=_dec(h.get("institution_price")) or Decimal("0"),
                institution_value=_dec(h.get("institution_value")) or Decimal("0"),
                cost_basis=_dec(h.get("cost_basis")),
                iso_currency_code=h.get("iso_currency_code") or "USD",
            )
        )
    return holdings


def _parse_transactions(
    raw_txns: list[dict], securities: dict[str, Security]
) -> list[InvestmentTransaction]:
    txns = []
    for t in raw_txns:
        sid = t.get("security_id")
        sec = securities.get(sid) if sid else None
        txns.append(
            InvestmentTransaction(
                investment_transaction_id=t.get("investment_transaction_id") or "",
                account_id=t.get("account_id") or "",
                security_id=sid,
                ticker=(sec.ticker if sec else None),
                name=t.get("name") or "",
                date=date.fromisoformat(str(t["date"])),
                quantity=_dec(t.get("quantity")) or Decimal("0"),
                amount=_dec(t.get("amount")) or Decimal("0"),
                price=_dec(t.get("price")) or Decimal("0"),
                fees=_dec(t.get("fees")),
                transaction_type=t.get("type") or "",
                subtype=t.get("subtype") or "",
                iso_currency_code=t.get("iso_currency_code") or "USD",
            )
        )
    return txns


# ── DataFetcher ───────────────────────────────────────────────────────────────


class DataFetcher:
    """Orchestrates fetching and normalising data across all Plaid accounts."""

    def __init__(self, plaid: PlaidClient) -> None:
        self._plaid = plaid

    def fetch_snapshot(
        self,
        txn_days: int = 30,
    ) -> PortfolioSnapshot:
        """Pull a full portfolio snapshot: accounts + holdings + recent transactions."""
        snapshot = PortfolioSnapshot(fetched_at=datetime.utcnow())

        end_date = date.today().isoformat()
        start_date = (date.today() - timedelta(days=txn_days)).isoformat()

        for token in self._plaid._cfg.plaid_access_tokens:
            try:
                holdings_resp = self._plaid.get_holdings(token)
            except Exception as exc:
                print(f"[DataFetcher] holdings error for token …{token[-6:]}: {exc}")
                continue

            securities = _securities_map(holdings_resp.get("securities") or [])
            snapshot.securities.update(securities)

            accounts = _parse_accounts(holdings_resp.get("accounts") or [])
            snapshot.accounts.extend(accounts)

            holdings = _parse_holdings(holdings_resp.get("holdings") or [], securities)
            snapshot.holdings.extend(holdings)

            # Transactions (best-effort)
            try:
                txn_resp = self._plaid.get_investment_transactions(
                    token, start_date, end_date
                )
                txn_securities = _securities_map(txn_resp.get("securities") or [])
                snapshot.securities.update(txn_securities)
                txns = _parse_transactions(
                    txn_resp.get("investment_transactions") or [],
                    {**securities, **txn_securities},
                )
                snapshot.transactions.extend(txns)
            except Exception as exc:
                print(f"[DataFetcher] transactions error for token …{token[-6:]}: {exc}")

        return snapshot
