"""MCP v2 echo server used by transport integration tests."""

from mcp.server.mcpserver import MCPServer

server = MCPServer("echo")


@server.tool()
def echo(value: str) -> str:
    """Return the input unchanged."""
    return value


if __name__ == "__main__":
    server.run()
