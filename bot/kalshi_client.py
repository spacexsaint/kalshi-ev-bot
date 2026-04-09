"""
kalshi_client.py — All Kalshi API calls, RSA-PSS auth, rate-limit handling.

BASE URLS (verified 2026-04-08 via docs.kalshi.com):
  Production: https://api.elections.kalshi.com/trade-api/v2
  Demo:       https://demo-api.kalshi.co/trade-api/v2

AUTH METHOD: RSA-PSS with SHA-256 (source: https://docs.kalshi.com/getting_started/api_keys)
  Headers required on every authenticated request:
    KALSHI-ACCESS-KEY:       Your API Key ID (UUID)
    KALSHI-ACCESS-TIMESTAMP: Current time in milliseconds (string)
    KALSHI-ACCESS-SIGNATURE: base64(RSA-PSS-SHA256(timestamp_ms + METHOD + path_no_query))
  IMPORTANT: Sign the path WITHOUT query parameters.

ENDPOINTS (all relative to base URL):
  GET  /markets                          — list markets (paginated)
  GET  /markets/{ticker}/orderbook       — get orderbook
  POST /portfolio/orders                 — place order
  GET  /portfolio/orders/{order_id}      — get single order status
  DELETE /portfolio/orders/{order_id}    — cancel order
  GET  /portfolio/balance                — get balance (cents → divide by 100 for USD)
  GET  /portfolio/positions              — get open positions

RATE LIMITS (Basic tier — source: https://docs.kalshi.com/getting_started/rate_limits):
  Read: 20 req/s | Write: 10 req/s
  On 429: wait 60s then retry. On 5xx: wait 10s then retry. Max 3 retries.

FEE SCHEDULE: see fee_calculator.py
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from bot import config
from bot import logger as bot_logger

_log = logging.getLogger(__name__)


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _load_private_key(path: str):
    """Load RSA private key from PEM file."""
    with open(path, "rb") as fh:
        return serialization.load_pem_private_key(
            fh.read(), password=None, backend=default_backend()
        )


def _sign_request(private_key, timestamp_ms: str, method: str, path: str) -> str:
    """
    Create RSA-PSS-SHA256 signature for a Kalshi API request.

    Signing string: timestamp_ms + HTTP_METHOD + path_without_query_params
    Algorithm: RSA-PSS, MGF1(SHA256), salt_length=DIGEST_LENGTH (32 bytes)
    Encoding: base64

    Source: https://docs.kalshi.com/getting_started/api_keys
    """
    path_no_query = path.split("?")[0]
    message = f"{timestamp_ms}{method}{path_no_query}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def _make_auth_headers(
    private_key,
    api_key_id: str,
    method: str,
    path: str,
) -> Dict[str, str]:
    """Return the three authentication headers for a Kalshi request."""
    timestamp_ms = str(int(time.time() * 1000))
    signature = _sign_request(private_key, timestamp_ms, method, path)
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": signature,
    }


# ── Retry logic ────────────────────────────────────────────────────────────────

async def _request_with_retry(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    private_key,
    api_key_id: str,
    *,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
    max_retries: int = config.MAX_RETRIES,
) -> Optional[dict]:
    """
    Execute an authenticated Kalshi API request with exponential backoff.

    Rate-limit (429): wait 60 seconds then retry.
    Server error (5xx): wait 10 seconds then retry.
    Max 3 retries with backoff: 1s, 2s, 4s.
    """
    parsed = urlparse(url)
    path = parsed.path  # Already the full path from root e.g. /trade-api/v2/...

    for attempt in range(max_retries + 1):
        t0 = time.monotonic()
        headers = _make_auth_headers(private_key, api_key_id, method.upper(), path)
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        try:
            async with session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                latency_ms = (time.monotonic() - t0) * 1000
                status = resp.status

                # Log every API call
                bot_logger.log_api_call(
                    method=method.upper(),
                    endpoint=parsed.path,
                    status_code=status,
                    latency_ms=latency_ms,
                )

                if status in (200, 201, 204):
                    if status == 204:
                        return {}
                    return await resp.json(content_type=None)

                if status == 429:
                    wait = config.RATE_LIMIT_WAIT_S
                    _log.warning("Rate limited (429). Waiting %ds before retry.", wait)
                    await asyncio.sleep(wait)
                    continue

                if 500 <= status < 600:
                    wait = config.SERVER_ERROR_WAIT_S * (2 ** attempt)
                    _log.warning("Server error %d. Waiting %ds before retry.", status, wait)
                    await asyncio.sleep(wait)
                    continue

                # Client errors (4xx other than 429)
                body = await resp.text()
                _log.error("Client error %d for %s %s: %s", status, method, url, body[:300])
                bot_logger.log_event(
                    "api_error",
                    f"Client error {status} on {method} {parsed.path}",
                    extra={"status": status, "body": body[:300]},
                )
                return None

        except asyncio.TimeoutError:
            latency_ms = (time.monotonic() - t0) * 1000
            bot_logger.log_api_call(
                method=method.upper(),
                endpoint=parsed.path,
                status_code=0,
                latency_ms=latency_ms,
                error="timeout",
            )
            _log.warning("Timeout on attempt %d for %s %s", attempt + 1, method, url)
        except aiohttp.ClientError as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            bot_logger.log_api_call(
                method=method.upper(),
                endpoint=parsed.path,
                status_code=0,
                latency_ms=latency_ms,
                error=str(exc),
            )
            _log.warning("Network error on attempt %d: %s", attempt + 1, exc)

        if attempt < max_retries:
            backoff = config.RETRY_BACKOFF_BASE * (2 ** attempt)
            await asyncio.sleep(backoff)

    _log.error("All %d retries exhausted for %s %s", max_retries + 1, method, url)
    return None


# ── Kalshi client class ────────────────────────────────────────────────────────

@dataclass
class OrderBook:
    yes_bid: float   # Best bid for YES (decimal)
    yes_ask: float   # Best ask for YES (decimal)
    no_bid: float    # Best bid for NO (decimal)
    no_ask: float    # Best ask for NO (decimal)


class KalshiClient:
    """
    Async client for the Kalshi Trading API v2.

    Usage:
        async with aiohttp.ClientSession() as session:
            client = KalshiClient(session)
            balance = await client.get_balance()
    """

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._api_key_id = os.getenv("KALSHI_API_KEY", "")
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi_private_key.pem")

        paper_mode = config.PAPER_MODE
        self._base_url = (
            config.KALSHI_BASE_URL_DEMO if paper_mode else config.KALSHI_BASE_URL_PROD
        )

        if not self._api_key_id:
            _log.warning("KALSHI_API_KEY not set — authenticated endpoints will fail.")
            self._private_key = None
        else:
            try:
                self._private_key = _load_private_key(key_path)
            except FileNotFoundError:
                _log.error("Private key file not found: %s", key_path)
                self._private_key = None

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    async def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        if not self._private_key:
            _log.error("No private key loaded — cannot make authenticated GET request")
            return None
        return await _request_with_retry(
            self._session,
            "GET",
            self._url(path),
            self._private_key,
            self._api_key_id,
            params=params,
        )

    async def _post(self, path: str, body: dict) -> Optional[dict]:
        if not self._private_key:
            _log.error("No private key loaded — cannot make authenticated POST request")
            return None
        return await _request_with_retry(
            self._session,
            "POST",
            self._url(path),
            self._private_key,
            self._api_key_id,
            json_body=body,
        )

    async def _delete(self, path: str) -> Optional[dict]:
        if not self._private_key:
            _log.error("No private key loaded — cannot make authenticated DELETE request")
            return None
        return await _request_with_retry(
            self._session,
            "DELETE",
            self._url(path),
            self._private_key,
            self._api_key_id,
        )

    # ── Public methods ─────────────────────────────────────────────────────────

    async def get_balance(self) -> Optional[float]:
        """
        Return account balance in USD.
        API returns balance in cents; we convert to dollars.
        Endpoint: GET /portfolio/balance
        """
        data = await self._get("/portfolio/balance")
        if data is None:
            return None
        # balance field is in cents
        return data.get("balance", 0) / 100.0

    async def get_markets(
        self,
        status: str = "open",
        limit: int = 1000,
        cursor: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch a page of markets.
        Endpoint: GET /markets
        Returns raw API response dict with 'markets' and 'cursor' keys.
        """
        params: dict = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._get("/markets", params=params)

    async def get_all_open_markets(self) -> List[dict]:
        """
        Paginate through ALL open markets and return a flat list.
        Handles cursor-based pagination automatically.
        """
        all_markets: List[dict] = []
        cursor: Optional[str] = None

        while True:
            page = await self.get_markets(status="open", limit=1000, cursor=cursor)
            if page is None:
                break
            markets = page.get("markets", [])
            all_markets.extend(markets)
            cursor = page.get("cursor")
            if not cursor or not markets:
                break

        _log.info("Fetched %d total open Kalshi markets", len(all_markets))
        return all_markets

    async def get_orderbook(self, ticker: str, depth: int = 5) -> Optional[OrderBook]:
        """
        Fetch the orderbook for a market.
        Endpoint: GET /markets/{ticker}/orderbook

        Response schema (orderbook_fp):
          yes_dollars: [[price_str, size_str], ...]  — sorted best-to-worst
          no_dollars:  [[price_str, size_str], ...]

        Returns OrderBook with best bid/ask on each side as decimals,
        or None if unavailable.
        """
        data = await self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})
        if data is None:
            return None

        ob = data.get("orderbook_fp", {})
        yes_levels: List[List[str]] = ob.get("yes_dollars", [])
        no_levels: List[List[str]] = ob.get("no_dollars", [])

        def best_ask(levels: List[List[str]]) -> float:
            """Lowest price = best ask."""
            if not levels:
                return 0.0
            prices = [float(l[0]) for l in levels if len(l) >= 1]
            return min(prices) if prices else 0.0

        def best_bid(levels: List[List[str]]) -> float:
            """Highest price = best bid."""
            if not levels:
                return 0.0
            prices = [float(l[0]) for l in levels if len(l) >= 1]
            return max(prices) if prices else 0.0

        yes_ask = best_ask(yes_levels)
        yes_bid = best_bid(yes_levels)
        no_ask = best_ask(no_levels)
        no_bid = best_bid(no_levels)

        if yes_ask == 0.0 and no_ask == 0.0:
            return None

        return OrderBook(
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
        )

    async def place_order(
        self,
        ticker: str,
        side: str,           # "yes" | "no"
        price_cents: int,    # 1–99
        num_contracts: int,
        client_order_id: str,
        action: str = "buy",  # "buy" | "sell"
    ) -> Optional[dict]:
        """
        Place a limit order.
        Endpoint: POST /portfolio/orders

        Args:
            ticker:          Market ticker
            side:            "yes" or "no"
            price_cents:     Limit price in cents (1–99)
            num_contracts:   Whole contracts only
            client_order_id: UUID for deduplication
            action:          "buy" (default) or "sell" (for closing positions)

        Returns:
            Raw order dict from API, or None on failure.
        """
        if price_cents < 1 or price_cents > 99:
            raise ValueError(f"price_cents must be 1–99, got {price_cents}")
        if num_contracts < 1:
            raise ValueError(f"num_contracts must be >= 1, got {num_contracts}")
        if action not in ("buy", "sell"):
            raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")

        body: dict = {
            "ticker": ticker,
            "action": action,
            "side": side.lower(),
            "count": num_contracts,
            "type": "limit",
            "client_order_id": client_order_id,
        }
        # Set price for the correct side
        if side.lower() == "yes":
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents

        data = await self._post("/portfolio/orders", body)
        if data is None:
            return None
        return data.get("order", data)

    async def cancel_order(self, order_id: str) -> Optional[dict]:
        """
        Cancel a resting order.
        Endpoint: DELETE /portfolio/orders/{order_id}

        Returns the cancelled order object or None on failure.
        """
        data = await self._delete(f"/portfolio/orders/{order_id}")
        if data is None:
            return None
        return data.get("order", data)

    async def get_order_status(self, order_id: str) -> Optional[dict]:
        """
        Get the current status of a single order.
        Endpoint: GET /portfolio/orders/{order_id}

        Returns dict with:
          status:             "resting" | "filled" | "canceled" | ...
          fill_count_fp:      str — filled contracts (e.g. "5.00")
          remaining_count_fp: str — remaining contracts
          initial_count_fp:   str — original count
        """
        data = await self._get(f"/portfolio/orders/{order_id}")
        if data is None:
            return None
        return data.get("order", data)

    async def get_positions(self) -> List[dict]:
        """
        Get all open positions.
        Endpoint: GET /portfolio/positions

        Returns list of position objects.
        """
        data = await self._get("/portfolio/positions")
        if data is None:
            return []
        return data.get("market_positions", data.get("positions", []))

    async def get_market(self, ticker: str) -> Optional[dict]:
        """
        Get a single market by ticker.
        Endpoint: GET /markets/{ticker}
        """
        data = await self._get(f"/markets/{ticker}")
        if data is None:
            return None
        return data.get("market", data)
