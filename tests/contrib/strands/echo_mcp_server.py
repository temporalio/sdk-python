from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo-server")


@mcp.tool()
def echo(message: str) -> str:
    """Return the input message unchanged."""
    return message


if __name__ == "__main__":
    mcp.run()
