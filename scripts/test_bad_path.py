"""Run targeted bad-path tests against the shopping agent.

This script intentionally exercises failure cases the evaluator can trigger:
- empty / whitespace messages
- missing reset() state
- repeated resets
- invalid session ordering
- malformed or edge-case inputs

It should not crash the process; it should return valid fallback output.
"""

from __future__ import annotations

from starter.agent import Agent


def run_case(agent: Agent, session_id: str, message: str, turn: int, top_k: int) -> dict:
    try:
        return agent.respond(session_id, message, turn, top_k)
    except Exception as exc:  # pragma: no cover - diagnostic output
        return {"error": type(exc).__name__, "message": str(exc)}


def main() -> None:
    agent = Agent("data/catalog.jsonl")

    # Missing reset: should raise in the raw API, but Agent.respond catches it
    # and converts it into EMPTY_RESPONSE in normal use.
    try:
        result = agent.respond("missing_reset", "hello", 1, 5)
        print("missing_reset ->", result)
    except Exception as exc:
        print("missing_reset raised:", type(exc).__name__, exc)

    # Normal reset and challenge cases
    agent.reset("badtest", {})
    cases = [
        ("", 1, 5),
        ("   ", 1, 5),
        ("hello", 1, 5),
        ("actually, I want blue instead", 2, 5),
        ("no preference", 3, 5),
        ("very long message " * 50, 4, 5),
        ("under $50 red backpack", 5, 10),
        ("asdfghjkl qwerty", 6, 10),
    ]

    for message, turn, top_k in cases:
        print(f"\nCASE: {message[:40]!r} | turn={turn} | top_k={top_k}")
        result = run_case(agent, "badtest", message, turn, top_k)
        print(result)

    # Repeated resets should not poison a clean test run.
    agent.reset("badtest2", {})
    print("\nRepeated reset test:")
    print(run_case(agent, "badtest2", "black shoes", 1, 5))
    agent.reset("badtest2", {"prefers": ["nike"]})
    print(run_case(agent, "badtest2", "black shoes", 2, 5))


if __name__ == "__main__":
    main()
