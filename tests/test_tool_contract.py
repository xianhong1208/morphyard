"""The MCP tool list is a contract: what every client and model sees.

tests/fixtures/mcp_tools.json holds the tool list (names, descriptions, input
and output schemas) exactly as `tools/list` serves it. Any change to a tool's
name, description, parameters, defaults or output schema fails here, so a
change of the contract is always deliberate: regenerate the snapshot with

    .venv/bin/python tests/test_tool_contract.py --write

and review the diff. Refactors behind the tools (the operations registry) must
leave it untouched.
"""

import asyncio
import json
import sys
from pathlib import Path

from fastmcp import Client, FastMCP

SNAPSHOT = Path(__file__).parent / "fixtures" / "mcp_tools.json"


async def _tool_list() -> list[dict]:
    mcp = FastMCP("contract")
    from src.fastmcp_tools.morphyard_tools import register_morphyard_tools
    register_morphyard_tools(mcp)
    async with Client(mcp) as client:
        tools = await client.list_tools()
    return [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools]


def test_tool_list_matches_the_snapshot():
    """TC-OPS-001: names, order, descriptions and schemas of all 24 tools are unchanged."""
    current = asyncio.run(_tool_list())
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert [t["name"] for t in current] == [t["name"] for t in expected]
    for got, want in zip(current, expected):
        assert got == want, f"tool {want['name']} differs from tests/fixtures/mcp_tools.json"
        # Dict equality ignores key order; the order of parameters is part of the contract
        assert json.dumps(got) == json.dumps(want), f"tool {want['name']}: parameter order"
    assert len(current) == 24


if __name__ == "__main__" and "--write" in sys.argv:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    SNAPSHOT.write_text(json.dumps(asyncio.run(_tool_list()), indent=2, ensure_ascii=False)
                        + "\n", encoding="utf-8")
    print(f"wrote {SNAPSHOT}")
