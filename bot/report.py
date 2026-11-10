"""Polish run summary for GitHub Actions and state/ostatni_raport.md."""

from __future__ import annotations


class Report:
    def __init__(self):
        self.mode = "paper"
        self.paper_reason = ""
        self.pubkey = ""
        self.sol = None
        self.usdc = None
        self.sol_reserve = None
        self.usdc_mint = ""
        self.day = ""
        self.realized = 0.0
        self.loss_limit = 10.0
        self.open_count = 0
        self.max_open = 3
        self.discovered = 0
        self.screened_out = 0
        self.sources: list[tuple[str, int]] = []
        self.cycles = 1
        self.notes: list[str] = []
        self.warnings: list[str] = []
        self.buys: list[str] = []
        self.sells: list[str] = []
        self.positions: list[str] = []
        self.skips: list[str] = []

    def render(self) -> str:
        lines = ["# Bot memecoinów Solana", ""]
        if self.mode == "live":
            sol_blocked = (
                self.sol is not None
                and self.sol_reserve is not None
                and self.sol < self.sol_reserve
            )
            if sol_blocked:
                lines.append(
                    "**Tryb: PRAWDZIWY HANDEL, ale nic nie wysyłam.** "
                    "Na portfelu jest za mało SOL na opłaty."
                )
            else:
                lines.append(
                    "**Tryb: PRAWDZIWY HANDEL.** Bot wysyła transakcje i wydaje USDC z portfela."
                )
        else:
            lines.append("**Tryb: PRÓBNY.** Bot nie wysyła transakcji i nie wydaje pieniędzy.")
            if self.paper_reason:
                lines.append(self.paper_reason)
        lines.append("")
        if self.pubkey:
            lines.append(f"**Portfel:** `{self.pubkey}`")
            lines.append(f"Portfel na Solscan: https://solscan.io/account/{self.pubkey}")
        if self.sol is not None:
            flag = ""
            if self.sol_reserve is not None and self.sol < self.sol_reserve:
                flag = " — ZA MAŁO NA OPŁATY"
            reserve = f"{self.sol_reserve:.4f}" if self.sol_reserve is not None else "?"
            lines.append(f"**SOL (tylko opłaty):** {self.sol:.4f}{flag}. Minimum: {reserve}.")
        if self.usdc is not None:
            lines.append(f"**USDC:** {self.usdc:.2f}")
        if self.usdc_mint:
            lines.append(f"**Płatność:** oficjalne USDC Solana `{self.usdc_mint}`")
        lines.append(f"**Dzień UTC:** {self.day or '—'}")
        lines.append(
            f"**Zrealizowany wynik dzisiaj:** {self.realized:+.2f} USDC "
            f"(limit straty {self.loss_limit:.0f} USD)."
        )
        lines.append(f"**Otwarte pozycje:** {self.open_count} / {self.max_open}")
        if self.cycles > 1:
            lines.append(
                f"**Sprawdzenia w tym uruchomieniu:** {self.cycles} "
                "(ceny co około 30 sekund)."
            )
        else:
            lines.append("**Sprawdzenia w tym uruchomieniu:** 1")
        lines.append("")
        lines.append(
            "GitHub często spóźnia uruchomienie co 5 minut. "
            "Stop nie zadziała w tej samej sekundzie, w której cena spadnie."
        )
        if self.discovered or self.sources:
            lines.append(
                f"Kandydaci: {self.discovered} unikalnych adresów Solana, "
                f"{self.screened_out} odpadło na prostym filtrze."
            )
            for label, count in self.sources:
                lines.append(f"- {label}: {count}")
        lines.append("")

        if self.notes:
            lines.append("## Ważne")
            for note in _unique(self.notes):
                lines.append(f"- {note}")
            lines.append("")

        lines.append("## Sprzedaże")
        lines.extend(_bullets(self.sells))
        lines.append("")
        lines.append("## Kupna")
        lines.extend(_bullets(self.buys))
        lines.append("")
        lines.append("## Otwarte pozycje")
        lines.extend(_bullets(self.positions))
        lines.append("")
        if self.skips:
            lines.append("## Pominięte tokeny")
            for skip in self.skips[:12]:
                lines.append(f"- {skip}")
            if len(self.skips) > 12:
                lines.append(f"- …i jeszcze {len(self.skips) - 12}")
            lines.append("")
        if self.warnings:
            lines.append("## Ostrzeżenia")
            for warning in _unique(self.warnings)[:8]:
                lines.append(f"- {warning}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _unique(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _bullets(items: list[str]) -> list[str]:
    if not items:
        return ["- brak"]
    return [f"- {item}" for item in items]


def redact(text: str, secret: str) -> str:
    raw = secret or ""
    stripped = raw.strip()
    for value in (raw, stripped):
        if len(value) >= 16 and value in text:
            text = text.replace(value, "[ukryte]")
    return text
