from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn
from dotenv import load_dotenv

from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Liner local music workbench")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser")
    args = parser.parse_args()
    load_dotenv()
    settings = Settings.from_env()
    if not args.no_browser:
        threading.Timer(
            1.2, lambda: webbrowser.open(f"http://127.0.0.1:{settings.port}")
        ).start()
    uvicorn.run(
        "liner.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=settings.port,
        reload=False,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
