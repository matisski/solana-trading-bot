"""Wallet loading and Solana RPC. The private key is never logged."""

from __future__ import annotations

import base64
import json
import time

import base58
import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)


class WalletError(Exception):
    """The secret is missing or is not a Phantom private key."""


class RpcError(Exception):
    """A Solana RPC call failed. The message must not contain the key."""


def load_keypair(secret: str) -> Keypair:
    text = secret.strip().strip('"').strip("'")
    words = text.replace("\n", " ").replace("\r", " ").split()
    if len(words) >= 8:
        raise WalletError(
            "To wygląda jak lista słów (seed phrase), a nie klucz prywatny. "
            "W Phantom wyeksportuj private key tego jednego portfela bota. "
            "Nie wklejaj 12 ani 24 słów."
        )
    compact = "".join(words)
    if compact.startswith("["):
        try:
            raw = bytes(int(item) for item in json.loads(compact))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WalletError("Nie da się odczytać klucza w formacie listy liczb.") from exc
    else:
        try:
            raw = base58.b58decode(compact)
        except Exception as exc:
            raise WalletError(
                "Nie da się odczytać klucza. W Phantom skopiuj private key "
                "(długi ciąg liter i cyfr) i wklej go tylko do sekretu SOLANA_PRIVATE_KEY."
            ) from exc
    try:
        if len(raw) == 64:
            return Keypair.from_bytes(raw)
        if len(raw) == 32:
            return Keypair.from_seed(raw)
    except Exception as exc:
        raise WalletError("Klucz ma zły format i nie tworzy portfela.") from exc
    raise WalletError("Klucz ma złą długość. Użyj eksportu private key z Phantom.")


class SolanaRpc:
    def __init__(self, url: str, timeout: float):
        self.url = url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def call(self, method: str, params: list):
        last = "rpc"
        for attempt in range(3):
            try:
                response = self.session.post(
                    self.url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                last = str(exc)[:200]
                time.sleep(1 + attempt)
                continue
            if payload.get("error"):
                message = payload["error"].get("message", "błąd RPC") if isinstance(payload["error"], dict) else "błąd RPC"
                raise RpcError(str(message)[:200])
            return payload.get("result")
        raise RpcError(last)

    def sol_balance(self, owner: str) -> float:
        result = self.call("getBalance", [owner, {"commitment": "confirmed"}])
        return int(result["value"]) / 1_000_000_000

    def token_raw(self, owner: str, mint: str) -> int:
        total = self._by_filter(owner, {"mint": mint}, mint)
        if total > 0:
            return total
        extra_error = None
        for program in TOKEN_PROGRAMS:
            try:
                total += self._by_filter(owner, {"programId": program}, mint)
            except RpcError as exc:
                extra_error = exc
        if total == 0 and extra_error is not None:
            raise extra_error
        return total

    def _by_filter(self, owner: str, filt: dict, mint: str) -> int:
        result = self.call(
            "getTokenAccountsByOwner",
            [owner, filt, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        total = 0
        for account in (result or {}).get("value") or []:
            try:
                info = account["account"]["data"]["parsed"]["info"]
            except (KeyError, TypeError):
                continue
            if info.get("mint") != mint:
                continue
            amount = (info.get("tokenAmount") or {}).get("amount")
            if amount is not None:
                total += int(amount)
        return total

    def send_swap(self, keypair: Keypair, swap_tx_b64: str) -> str:
        try:
            raw = base64.b64decode(swap_tx_b64)
            unsigned = VersionedTransaction.from_bytes(raw)
            signed = VersionedTransaction(unsigned.message, [keypair])
            encoded = base64.b64encode(bytes(signed)).decode("ascii")
        except Exception as exc:
            raise RpcError("Nie udało się podpisać transakcji.") from exc
        result = self.call(
            "sendTransaction",
            [
                encoded,
                {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "preflightCommitment": "confirmed",
                    "maxRetries": 3,
                },
            ],
        )
        if not isinstance(result, str) or len(result) < 20:
            raise RpcError("Sieć nie zwróciła podpisu transakcji.")
        return result

    def confirm(self, signature: str, timeout_s: float) -> str:
        deadline = time.time() + timeout_s
        while True:
            result = self.call(
                "getSignatureStatuses",
                [[signature], {"searchTransactionHistory": True}],
            )
            status = None
            if isinstance(result, dict):
                values = result.get("value") or []
                status = values[0] if values else None
            if status:
                if status.get("err"):
                    return "failed"
                if status.get("confirmationStatus") in {"confirmed", "finalized"}:
                    return "confirmed"
            if time.time() >= deadline:
                return "timeout"
            time.sleep(2)
