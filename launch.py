"""
launch.py — Cross-platform launcher for the Hospital Price Explorer — API version.

Usage:
    python3 launch.py             # serves on default port 8765
    python3 launch.py --port 9000 # custom port
    python3 launch.py --no-open   # don't auto-open the browser

What it does:
    1. Finds a free TCP port (default 8765, falls back if taken)
    2. Starts an HTTP server serving the ./api folder
    3. Opens http://localhost:<port> in your default browser
    4. Runs until you press Ctrl+C

Requires Python 3.6+. No external dependencies.
"""

import argparse
import http.server
import os
import socket
import socketserver
import sys
import threading
import time
import webbrowser
from pathlib import Path


# ── config ──────────────────────────────────────────────────────────────
DEFAULT_PORT = 8765
SERVE_DIR    = "api"


def _find_free_port(preferred):
    """Return `preferred` if free, else the next available port."""
    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port in range")


def _open_browser_soon(url, delay=1.0):
    """Open the browser after a short delay so the server has time to bind."""
    def _open():
        time.sleep(delay)
        webbrowser.open(url)
    threading.Thread(target=_open, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"port to serve on (default {DEFAULT_PORT})")
    ap.add_argument("--no-open", action="store_true",
                    help="don't automatically open the browser")
    args = ap.parse_args()

    # Resolve the api/ folder relative to this launcher, regardless of CWD
    script_dir = Path(__file__).resolve().parent
    serve_root = script_dir / SERVE_DIR
    if not serve_root.is_dir():
        sys.exit(f"ERROR: could not find '{SERVE_DIR}/' next to launch.py. "
                 f"Expected: {serve_root}")

    os.chdir(serve_root)
    port = _find_free_port(args.port)
    url  = f"http://localhost:{port}"

    print("=" * 56)
    print("  Hospital Price Explorer — API Viewer")
    print("=" * 56)
    print(f"  Serving:    {serve_root}")
    print(f"  URL:        {url}")
    if port != args.port:
        print(f"  Note:       port {args.port} was busy; using {port} instead")
    print()
    print("  Keep this window open while using the viewer.")
    print("  Press Ctrl+C to stop.")
    print("=" * 56)
    print()

    if not args.no_open:
        _open_browser_soon(url, delay=1.0)

    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n\n  Stopping server. Goodbye.")


if __name__ == "__main__":
    main()
