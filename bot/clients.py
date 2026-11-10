"""DexScreener, RugCheck and the Jupiter quote/swap API."""

from __future__ import annotations

import time

import requests


class Http:
    def __init__(self, timeout: float):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "solana-memecoin-bot",
                "Accept": "application/json",
            }
        )

    def get_json(self, url: str, params: dict | None = None):
        return self._request("GET", url, params=params)

    def post_json(self, url: str, body: dict):
        return self._request("POST", url, json_body=body)

    def _request(self, method: str, url: str, params: dict | None = None, json_body: dict | None = None):
        last = "brak odpowiedzi"
        for attempt in range(3):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last = type(exc).__name__
                time.sleep(1.0 * (attempt + 1))
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {response.status_code}"
                time.sleep(1.2 * (attempt + 1))
                continue
            if response.status_code >= 400:
                text = response.text[:180].replace("\n", " ")
                return None, f"HTTP {response.status_code}: {text}"
            try:
                return response.json(), ""
            except ValueError:
                return None, "odpowiedź nie jest JSON"
        return None, last


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


class DexScreener:
    def __init__(self, http: Http):
        self.http = http

    def trending_mints(self, limit: int = 60) -> tuple[list[str], list[str]]:
        warnings: list[str] = []
        found: list[str] = []
        urls = (
            "https://api.dexscreener.com/token-boosts/top/v1",
            "https://api.dexscreener.com/token-boosts/latest/v1",
            "https://api.dexscreener.com/token-profiles/latest/v1",
        )
        for url in urls:
            data, err = self.http.get_json(url)
            if err or not isinstance(data, list):
                warnings.append(f"DexScreener: {err or 'zła odpowiedź'}")
                continue
            for item in data:
                if not isinstance(item, dict) or item.get("chainId") != "solana":
                    continue
                address = item.get("tokenAddress")
                if isinstance(address, str) and address and address not in found:
                    found.append(address)
                if len(found) >= limit:
                    return found, warnings
        return found, warnings

    def pairs_for(self, mints: list[str]) -> tuple[list[dict], list[str]]:
        pairs: list[dict] = []
        warnings: list[str] = []
        for chunk in _chunks(mints, 30):
            url = "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(chunk)
            data, err = self.http.get_json(url)
            if err or not isinstance(data, list):
                warnings.append(f"Pary DexScreener: {err or 'zła odpowiedź'}")
                continue
            pairs.extend(item for item in data if isinstance(item, dict))
        return pairs, warnings


class RugCheck:
    def __init__(self, http: Http):
        self.http = http

    def report(self, mint: str):
        data, err = self.http.get_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
        if err or not isinstance(data, dict):
            return None, err or "brak raportu RugCheck"
        if data.get("error"):
            return None, str(data.get("error"))[:180]
        return data, ""


class Jupiter:
    def __init__(self, http: Http, quote_url: str, swap_url: str):
        self.http = http
        self.quote_url = quote_url
        self.swap_url = swap_url

    def quote(self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int):
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount_raw)),
            "slippageBps": str(int(slippage_bps)),
            "restrictIntermediateTokens": "true",
        }
        data, err = self.http.get_json(self.quote_url, params=params)
        if err or not isinstance(data, dict):
            return None, err or "brak wyceny Jupiter"
        if data.get("inputMint") != input_mint or data.get("outputMint") != output_mint:
            return None, "Jupiter zwrócił inną parę niż żądana"
        if not data.get("outAmount") or not data.get("routePlan"):
            message = data.get("error") or data.get("message") or "brak trasy"
            return None, str(message)[:180]
        return data, ""

    def swap_transaction(self, quote: dict, user_pubkey: str, max_priority_lamports: int):
        body = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": int(max_priority_lamports),
                    "priorityLevel": "high",
                }
            },
        }
        data, err = self.http.post_json(self.swap_url, body)
        if err or not isinstance(data, dict):
            return None, err or "brak transakcji Jupiter"
        if data.get("simulationError"):
            return None, "symulacja Jupiter nie przeszła: " + str(data["simulationError"])[:160]
        transaction = data.get("swapTransaction")
        if not isinstance(transaction, str) or not transaction:
            return None, "Jupiter nie zwrócił transakcji"
        return transaction, ""
