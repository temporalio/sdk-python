"""A minimal MCP echo server for the google_genai MCP tests.

Run as a script it serves over stdio; imported, ``mcp`` can be connected to
in-memory via ``mcp.shared.memory``.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo-server")


@mcp.tool()
def echo(message: str) -> str:
    """Return the input message unchanged."""
    return message


if __name__ == "__main__":
    mcp.run()
