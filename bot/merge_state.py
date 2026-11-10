"""Resolve a git rebase conflict in state/ without dropping either side."""

from __future__ import annotations

import json
from pathlib import Path

from bot.reconcile import split_conflict, union_csv, union_state


def resolve_tree(root: Path) -> int:
    fixed = 0
    state_path = root / "state" / "state.json"
    if state_path.exists():
        text = state_path.read_text(encoding="utf-8")
        sides = split_conflict(text)
        if sides is not None:
            merged = union_state(json.loads(sides[0]), json.loads(sides[1]))
            state_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            fixed += 1
    csv_path = root / "state" / "trades.csv"
    if csv_path.exists():
        text = csv_path.read_text(encoding="utf-8")
        sides = split_conflict(text)
        if sides is not None:
            csv_path.write_text(union_csv(sides[0], sides[1]), encoding="utf-8")
            fixed += 1
    report_path = root / "state" / "ostatni_raport.md"
    if report_path.exists():
        text = report_path.read_text(encoding="utf-8")
        sides = split_conflict(text)
        if sides is not None:
            # The side being replayed is the run that just finished.
            report_path.write_text(sides[1] or sides[0], encoding="utf-8")
            fixed += 1
    return fixed


def main() -> None:
    resolve_tree(Path("."))


if __name__ == "__main__":
    main()
