#!/usr/bin/env python3
"""Managed process used by the native resource evaluation."""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ready": True, "managed_pid": os.getpid(), "python": list(sys.version_info[:3])}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
