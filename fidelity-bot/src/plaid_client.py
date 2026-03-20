"""Plaid API client — link tokens, exchange, and read-only data access."""

from __future__ import annotations

import plaid
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest
from plaid.model.investments_transactions_get_request import (
    InvestmentsTransactionsGetRequest,
)
from plaid.model.investments_transactions_get_request_options import (
    InvestmentsTransactionsGetRequestOptions,
)
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products

from src.config import Config

_ENV_MAP = {
    "sandbox": plaid.Environment.Sandbox,
    "development": plaid.Environment.Development,
    "production": plaid.Environment.Production,
}


def build_plaid_client(cfg: Config) -> plaid_api.PlaidApi:
    env = _ENV_MAP.get(cfg.plaid_env.lower(), plaid.Environment.Sandbox)
    configuration = plaid.Configuration(
        host=env,
        api_key={
            "clientId": cfg.plaid_client_id,
            "secret": cfg.plaid_secret,
        },
    )
    api_client = plaid.ApiClient(configuration)
    return plaid_api.PlaidApi(api_client)


class PlaidClient:
    """Thin wrapper around the Plaid API for investment data."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._client = build_plaid_client(cfg)

    # ── Link Token (for Plaid Link UI) ────────────────────────────────────────

    def create_link_token(self, user_id: str = "portfolio-bot-user") -> str:
        """Create a link token to initialise Plaid Link in a browser/app."""
        request = LinkTokenCreateRequest(
            products=[Products("investments")],
            client_name="Portfolio Intelligence Bot",
            country_codes=[CountryCode("US")],
            language="en",
            user=LinkTokenCreateRequestUser(client_user_id=user_id),
        )
        response = self._client.link_token_create(request)
        return response["link_token"]

    # ── Holdings ──────────────────────────────────────────────────────────────

    def get_holdings(self, access_token: str) -> dict:
        """Return raw Plaid holdings response for an access token."""
        request = InvestmentsHoldingsGetRequest(access_token=access_token)
        response = self._client.investments_holdings_get(request)
        return response.to_dict()

    # ── Investment Transactions ───────────────────────────────────────────────

    def get_investment_transactions(
        self,
        access_token: str,
        start_date: str,
        end_date: str,
        count: int = 500,
        offset: int = 0,
    ) -> dict:
        """Return investment transactions in [start_date, end_date]."""
        options = InvestmentsTransactionsGetRequestOptions(count=count, offset=offset)
        from datetime import date

        request = InvestmentsTransactionsGetRequest(
            access_token=access_token,
            start_date=date.fromisoformat(start_date),
            end_date=date.fromisoformat(end_date),
            options=options,
        )
        response = self._client.investments_transactions_get(request)
        return response.to_dict()

    # ── Item / Institution Metadata ───────────────────────────────────────────

    def get_item(self, access_token: str) -> dict:
        request = ItemGetRequest(access_token=access_token)
        response = self._client.item_get(request)
        return response.to_dict()

    def get_institution_name(self, institution_id: str) -> str:
        try:
            request = InstitutionsGetByIdRequest(
                institution_id=institution_id,
                country_codes=[CountryCode("US")],
            )
            response = self._client.institutions_get_by_id(request)
            return response["institution"]["name"]
        except Exception:
            return institution_id

    # ── Convenience: all tokens ───────────────────────────────────────────────

    def get_all_holdings(self) -> list[dict]:
        """Fetch holdings for every configured access token."""
        results = []
        for token in self._cfg.plaid_access_tokens:
            try:
                data = self.get_holdings(token)
                data["_access_token"] = token
                results.append(data)
            except Exception as exc:
                results.append({"_access_token": token, "_error": str(exc)})
        return results
