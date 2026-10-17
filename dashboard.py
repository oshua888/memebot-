#!/usr/bin/env python3
"""Tableau de bord du bot simulé : python3 dashboard.py  ->  http://localhost:8765"""
import csv
import json
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import memescanner as ms

PORT = 8765


def state():
    pfs = ms.load_all(ms.CONFIG)
    trades = []
    if ms.JOURNAL_FILE.exists():
        with ms.JOURNAL_FILE.open() as f:
            trades = list(csv.DictReader(f))[-300:]
    status = {}
    if ms.STATUS_FILE.exists():
        try:
            status = json.loads(ms.STATUS_FILE.read_text())
        except ValueError:
            pass
    return {
        "now": time.time(), "start": ms.CONFIG["start_balance_usd"],
        "pfs": {n: dict(pf, equity=ms.equity(pf), params=ms.strat_cfg(ms.CONFIG, n))
                for n, pf in pfs.items()},
        "trades": trades[::-1], "status": status,
        "interval": ms.CONFIG["scan_interval_s"],
    }


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/state"):
            body, ctype = json.dumps(state()).encode(), "application/json"
        else:
            body, ctype = (ms.DATA_DIR / "dashboard.html").read_bytes(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def export(out_dir):
    """Version statique (GitHub Pages) : index.html + state.json."""
    out = ms.DATA_DIR / out_dir
    out.mkdir(exist_ok=True)
    (out / "index.html").write_bytes((ms.DATA_DIR / "dashboard.html").read_bytes())
    st = state()
    st["interval"] = 300  # GitHub Actions : un cycle toutes les ~5 min
    (out / "state.json").write_text(json.dumps(st))
    print(f"Tableau de bord exporté dans {out}")


if __name__ == "__main__":
    if "--export" in sys.argv:
        export(sys.argv[sys.argv.index("--export") + 1] if len(sys.argv) > sys.argv.index("--export") + 1 else "docs")
        sys.exit()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://localhost:{PORT}"
    print(f"Tableau de bord : {url}  (Ctrl+C pour arrêter)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
