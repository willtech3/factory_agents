"""Tiny synthetic greenfield fixture, run only inside a disposable Linux sandbox."""
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PID = ROOT / ".server.json"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        else:
            super().do_GET()


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "serve":
        http.server.ThreadingHTTPServer(("127.0.0.1", 8088), Handler).serve_forever()
    elif action == "start":
        with (ROOT / ".server.log").open("wb") as log:
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve"],
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        identity = Path(f"/proc/{proc.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        PID.write_text(json.dumps({"pid": proc.pid, "start": identity}))
    elif action == "stop" and PID.exists():
        saved = json.loads(PID.read_text())
        process = Path(f"/proc/{saved['pid']}")
        if process.exists():
            identity = (process / "stat").read_text().rsplit(")", 1)[1].split()[19]
            command = (process / "cmdline").read_bytes().split(b"\x00")
            if identity != saved["start"] or str(Path(__file__).resolve()).encode() not in command or b"serve" not in command:
                raise SystemExit("refusing to stop an unrelated process")
            os.kill(saved["pid"], signal.SIGTERM)
        PID.unlink()
