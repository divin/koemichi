"""Dry-run the research agent against live Sagashi and the configured LLM.

Runs the real bounded agentic loop end to end but never touches NoteDiscovery,
the database, or the vault: the typed result, usage, and timing are printed to
stdout only. Useful to verify tool-calling behavior and budget handling before
deploying the research workflow.

Usage:
    uv run python scripts/research_dry_run.py "the history of the Berlin U-Bahn"

Environment:
    LLM_URL, LLM_MODEL_NAME, SAGASHI_MCP_URL as for the worker, plus
    RESEARCH_MAX_TOOL_CALLS / RESEARCH_TIMEOUT_SECONDS overrides.
"""

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

load_dotenv()


def main() -> int:
    """Parse arguments, run one research agent loop, and print the result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("topic", help="Research topic, without the 'research' keyword")
    parser.add_argument(
        "--max-calls",
        type=int,
        default=None,
        help="Override RESEARCH_MAX_TOOL_CALLS for this run",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Override RESEARCH_TIMEOUT_SECONDS for this run",
    )
    args = parser.parse_args()

    if args.max_calls is not None:
        os.environ["RESEARCH_MAX_TOOL_CALLS"] = str(args.max_calls)
    if args.timeout is not None:
        os.environ["RESEARCH_TIMEOUT_SECONDS"] = str(args.timeout)

    # Import after overrides so settings.py reads the patched values.
    from koemichi.services.workflows import research
    from koemichi.shared.settings import (
        LLM_MODEL_NAME,
        LLM_URL,
        RESEARCH_MAX_TOOL_CALLS,
        RESEARCH_TIMEOUT_SECONDS,
        SAGASHI_MCP_URL,
    )

    if not SAGASHI_MCP_URL or not LLM_URL or not LLM_MODEL_NAME:
        print("Missing SAGASHI_MCP_URL / LLM_URL / LLM_MODEL_NAME", file=sys.stderr)
        return 2

    print(f"Sagashi: {SAGASHI_MCP_URL}")
    print(f"Model:   {LLM_MODEL_NAME} at {LLM_URL}")
    print(
        f"Budget:  {RESEARCH_MAX_TOOL_CALLS} tool calls / {RESEARCH_TIMEOUT_SECONDS}s"
    )
    print(f"Topic:   {args.topic}")
    print("---")

    start = time.monotonic()
    from pydantic_ai.usage import UsageLimits

    try:
        result = asyncio.run(
            research._get_research_agent().run(
                args.topic,
                usage_limits=UsageLimits(request_limit=RESEARCH_MAX_TOOL_CALLS * 2 + 2),
            )
        )
    except Exception as exc:  # noqa: BLE001 - the dry run reports any failure
        print(f"FAILED after {time.monotonic() - start:.1f}s: {exc!r}")
        return 1
    elapsed = time.monotonic() - start

    draft = research.ResearchResult.model_validate(result.output)
    usage = result.usage
    print(f"Elapsed:      {elapsed:.1f}s")
    print(f"Model requ.:  {usage.requests}")
    print(f"Tool calls:   {usage.tool_calls}")
    print(f"Title:        {draft.title}")
    print(f"Sources:      {len(draft.sources)}")
    for source in draft.sources:
        print(f"  - {source.title}: {source.url}")
    print("---")
    print(draft.summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
