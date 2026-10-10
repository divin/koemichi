"""Run the NoteDiscovery MCP server over stdio for local development."""

from koemichi.services.mcp.server import mcp

if __name__ == "__main__":
    mcp.run()
