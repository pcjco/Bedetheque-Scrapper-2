import argparse
import http.client
import http.cookiejar
import json
import logging
import os
import sys
import socket
import threading
import traceback
import urllib.error
import urllib.request
 
# When run via the dev-only fallback, this script lives in the published
# plugin folder alongside other files meant for IronPython 2.7 (e.g. a
# types.py shim defining py2-only names like `long`). Python auto-prepends
# this script's own directory to sys.path, so those same-named files can
# shadow real stdlib modules (types, and anything that imports it - re,
# enum, logging, ...) before any external module gets a chance to import.
# Strip it before importing anything else that could trigger that chain.
#
# Skip this entirely when frozen (running as the built exe): there,
# __file__'s directory is PyInstaller's extraction folder (_MEIPASS),
# which is the one sys.path entry the exe needs to find its own bundled
# modules - such as the bundled scraper library - so it must stay in sys.path.
if not getattr(sys, "frozen", False):
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    sys.path = [p for p in sys.path if os.path.abspath(p) != _SCRIPT_DIR]


"""
Small daemon that fetches bedetheque.com pages on demand.

It keeps one opener + cookie jar alive for the whole run and renews the
csrf_cookie_bel token automatically (on expiry, or when the server rejects
it). Clients talk to it over a local TCP socket, one JSON object per line:

    client > daemon:  {"url": "https://..."}\n
    daemon > client:  {"url": "...", "html": "..."}\n   or   {"error": "..."}\n

Usage:
    python BedethequeFetcher.py [--host 127.0.0.1] [--port 56789]
"""

BASE_URL = "https://www.bedetheque.com"
TIMEOUT = 30

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "bedetheque_cookies.txt")

HEADERS = [
    ("User-Agent",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/124.0.0.0 Safari/537.36"),
    ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
    ("Accept-Language", "en-US,en;q=0.5"),
    ("Connection", "keep-alive"),
    ("Upgrade-Insecure-Requests", "1"),
    ("Referer", BASE_URL + "/"),
]

log = logging.getLogger("fetch_daemon")


def _load_jar():
    """Read the cookie jar from disk, or create an empty one."""
    jar = http.cookiejar.MozillaCookieJar(COOKIE_FILE)
    if os.path.exists(COOKIE_FILE):
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.client.HTTPException):
            jar.clear()
    return jar


def _build_opener(jar):
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = list(HEADERS)
    return opener


class Daemon:
    """Shared state for outgoing requests: cookie jar, opener, CSRF token.
    Guarded by a lock because clients are served on separate threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jar = _load_jar()
        self._opener = _build_opener(self._jar)

    def _reset(self):
        """Drop the rejected token so the next fetch gets a fresh one."""
        self._jar.clear()
        self._opener = _build_opener(self._jar)

    def fetch(self, url):
        """Return the HTML of url; a 403 drops the cookies for a refresh."""
        with self._lock:
            opener = self._opener

        try:
            html, _ = self._read_html(opener, url)
            return html
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                log.info("403 on %s, token will be refreshed on next request", url)
                with self._lock:
                    self._reset()
            raise

    @staticmethod
    def _read_html(opener, url):
        with opener.open(url, timeout=TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            html = resp.read().decode(charset, errors="replace")
            final_url = resp.geturl()
        return html, final_url


_daemon = None


def daemon():
    global _daemon
    if _daemon is None:
        _daemon = Daemon()
    return _daemon


def _fetch_url(url):
    try:
        return {"url": url, "html": daemon().fetch(url)}
    except Exception as exc:
        return {"error": str(exc), "traceback": traceback.format_exc()}


def _read_line(conn):
    buf = bytearray()
    while not buf.endswith(b"\n"):
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf).decode("utf-8").strip()


def _send_json(conn, payload):
    conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))


def _handle_client(conn, addr):
    peer = f"{addr[0]}:{addr[1]}"
    log.info("connect  %s", peer)
    with conn:
        try:
            line = _read_line(conn)
            if not line:
                return

            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                log.warning("bad json from %s", peer)
                _send_json(conn, {"error": "Invalid JSON request"})
                return

            url = request.get("url")
            if not url or not isinstance(url, str):
                log.warning("missing 'url' from %s", peer)
                _send_json(conn, {"error": "Missing 'url' in request"})
                return

            log.info("request  %s", url)
            result = _fetch_url(url)
            if "error" in result:
                log.info("failed   %s: %s", url, result["error"])
            else:
                html = result["html"]
                log.info("served   %s (%d bytes)", url, len(html.encode("utf-8")))
            _send_json(conn, result)
        except (ConnectionResetError, BrokenPipeError):
            log.info("dropped  %s (client gone)", peer)
        except OSError:
            log.exception("socket error with %s", peer)
        except Exception:
            log.exception("internal error handling %s", peer)
            try:
                _send_json(conn, {"error": "daemon internal error"})
            except OSError:
                pass


def main(host="127.0.0.1", port=56789):
    """Run the daemon server.

    Returns an integer exit code: ``0`` for a clean shutdown (e.g. Ctrl‑C),
    ``1`` for any unexpected error that caused the loop to break.
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(message)s",
                        datefmt="%H:%M:%S")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    error_occurred = False
    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(5)
        # Poll every second: on Windows a blocking accept() keeps the main
        # thread inside a native call, where Ctrl-C never gets delivered.
        srv.settimeout(1.0)
        daemon()
        log.info("listening on %s:%s", host, port)
        while True:
            try:
                client, client_addr = srv.accept()
            except KeyboardInterrupt:
                log.info("shutting down (Ctrl-C)")
                break
            except socket.timeout:
                continue
            except OSError:
                traceback.print_exc()
                continue
            except Exception as exc:
                # Catch any unexpected exception that could crash the daemon loop
                log.exception("Daemon loop unexpected error: %s", exc)
                error_occurred = True
                break
            threading.Thread(target=_handle_client, args=(client, client_addr),
                             daemon=True).start()
    finally:
        srv.close()
    return 1 if error_occurred else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch bedetheque.com pages for local clients.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=56789)
    ns = parser.parse_args()
    # Run the daemon and exit with the appropriate status code.
    try:
        exit_code = main(ns.host, ns.port)
    except KeyboardInterrupt:
        # Ctrl‑C before or during daemon start; treat as clean shutdown.
        log.info("shutting down (Ctrl‑C) via top‑level handler")
        sys.exit(0)
    else:
        sys.exit(exit_code)