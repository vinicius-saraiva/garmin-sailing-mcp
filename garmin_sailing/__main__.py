"""CLI entry point: python -m garmin_sailing <command>."""

import sys

from garmin_sailing.auth import setup


def main():
    if len(sys.argv) < 2 or sys.argv[1] == "setup":
        setup()
    elif sys.argv[1] == "serve":
        from garmin_sailing.server import mcp
        mcp.run()
    else:
        print("Usage: python -m garmin_sailing [setup|serve]")
        print("  setup  — Authenticate with Garmin Connect")
        print("  serve  — Start the MCP server")
        sys.exit(1)


if __name__ == "__main__":
    main()
