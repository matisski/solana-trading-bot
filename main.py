"""Start bota. Ustawienia są w desk.yaml. Jedno uruchomienie GitHuba, w nim kilka sprawdzeń."""

from __future__ import annotations

import os
import sys

from bot.engine import run
from bot.report import redact


def main() -> int:
    try:
        return run()
    except Exception as exc:
        secret = os.environ.get("SOLANA_PRIVATE_KEY", "")
        message = redact(f"Nieoczekiwany błąd: {type(exc).__name__}: {exc}", secret)
        print(message, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
