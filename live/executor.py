"""
Order execution via py-clob-client.

Status: stub — to be implemented in Phase 5.
"""
from __future__ import annotations

import argparse


def main(mode: str = "paper") -> None:
    raise NotImplementedError(f"Executor (mode={mode}) will be implemented in Phase 5")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    args = parser.parse_args()
    main(args.mode)
