"""Minimal smoke test for the agent pipeline.

This script checks that the project can at least initialize and respond in a
basic happy-path conversation when the catalog file is present.
"""

from __future__ import annotations

from starter.agent import Agent


def main() -> None:
    try:
        agent = Agent("data/catalog.jsonl")
    except FileNotFoundError:
        print("Catalog not found at data/catalog.jsonl")
        print("Place the catalog file there before running the evaluator or smoke tests.")
        return

    agent.reset("smoke_session", {"preferred_stores": []})

    turns = [
        "I want a black backpack",
        "under $100",
        "actually, make it a red one",
        "any color is fine",
    ]

    for i, msg in enumerate(turns, start=1):
        resp = agent.respond("smoke_session", msg, i, 5)
        print(f"Turn {i}: {msg}")
        print(resp)
        print()


if __name__ == "__main__":
    main()
