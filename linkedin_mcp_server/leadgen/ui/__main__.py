"""``python -m linkedin_mcp_server.leadgen.ui`` / ``uv run leadgen-ui``."""

from __future__ import annotations

import argparse
import logging
import threading
import webbrowser

import uvicorn

from linkedin_mcp_server.leadgen.ui.server import create_app


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Lead-generation dashboard (local only)"
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--no-open", action="store_true", help="Do not open a browser tab"
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s"
    )
    url = f"http://127.0.0.1:{args.port}"
    print(f"\n  LEADGEN dashboard -> {url}\n")
    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(
        create_app(), host="127.0.0.1", port=args.port, log_level=args.log_level
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
