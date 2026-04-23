#!/usr/bin/env python3
"""
Whisper OpenAI-Compatible Proxy

Translates OpenAI's /v1/audio/transcriptions endpoint to the
onerahmet/openai-whisper-asr-webservice /asr endpoint.

VoiceMode expects:  POST /v1/audio/transcriptions  (field: "file")
Whisper service:    POST /asr                       (field: "audio_file")

Usage:
    python3 whisper-proxy.py [--port 2022] [--whisper-url http://127.0.0.1:9000]
"""

import argparse
import io
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.error
import re


class WhisperProxyHandler(BaseHTTPRequestHandler):

    whisper_url = "http://127.0.0.1:9000"

    def do_POST(self):
        if self.path == "/v1/audio/transcriptions":
            self._proxy_transcription()
        else:
            self.send_error(404, "Not Found")

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        elif self.path == "/v1/models":
            response = json.dumps({
                "object": "list",
                "data": [
                    {
                        "id": "whisper-1",
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "local",
                    }
                ],
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response.encode())
        else:
            self.send_error(404, "Not Found")

    def _proxy_transcription(self):
        content_type = self.headers.get("Content-Type", "")
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Parse multipart form data to extract the file
        boundary = self._extract_boundary(content_type)
        if not boundary:
            self.send_error(400, "Missing multipart boundary")
            return

        file_data, file_name, fields = self._parse_multipart(body, boundary)
        if not file_data:
            self.send_error(400, "No audio file found in request")
            return

        # Build new multipart request for /asr endpoint
        new_boundary = b"----ProxyBoundary1234567890"
        new_body = self._build_multipart(
            new_boundary, file_data, file_name,
            language=fields.get("language"),
        )

        # Forward to whisper service (output=txt for plain text)
        url = f"{self.whisper_url}/asr?output=txt"
        language = fields.get("language", "")
        # Filter out "auto" — the whisper ASR backend doesn't support it
        # and returns 500. Omitting the param lets whisper auto-detect.
        if language and language != "auto":
            url += f"&language={language}"

        req = urllib.request.Request(
            url,
            data=new_body,
            headers={"Content-Type": f"multipart/form-data; boundary={new_boundary.decode()}"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result_text = resp.read().decode().strip()
        except urllib.error.URLError as e:
            self.send_error(502, f"Whisper service error: {e}")
            return

        # Return response in the requested format
        response_format = fields.get("response_format", "json")
        if response_format == "text":
            response = result_text
            content_type = "text/plain"
        else:
            response = json.dumps({"text": result_text})
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response.encode())

    def _extract_boundary(self, content_type):
        match = re.search(r"boundary=(.+)", content_type)
        if match:
            return match.group(1).strip().encode()
        return None

    def _parse_multipart(self, body, boundary):
        file_data = None
        file_name = "audio.wav"
        fields = {}

        parts = body.split(b"--" + boundary)
        for part in parts:
            if b"Content-Disposition" not in part:
                continue

            header_end = part.find(b"\r\n\r\n")
            if header_end == -1:
                continue

            header = part[:header_end].decode(errors="replace")
            data = part[header_end + 4:]
            # Strip trailing \r\n
            if data.endswith(b"\r\n"):
                data = data[:-2]

            name_match = re.search(r'name="([^"]+)"', header)
            if not name_match:
                continue
            name = name_match.group(1)

            if name == "file":
                file_data = data
                fn_match = re.search(r'filename="([^"]+)"', header)
                if fn_match:
                    file_name = fn_match.group(1)
            else:
                fields[name] = data.decode(errors="replace")

        return file_data, file_name, fields

    def _build_multipart(self, boundary, file_data, file_name, language=None):
        body = io.BytesIO()
        body.write(b"--" + boundary + b"\r\n")
        body.write(
            f'Content-Disposition: form-data; name="audio_file"; filename="{file_name}"\r\n'.encode()
        )
        body.write(b"Content-Type: application/octet-stream\r\n\r\n")
        body.write(file_data)
        body.write(b"\r\n")
        body.write(b"--" + boundary + b"--\r\n")
        return body.getvalue()

    def log_message(self, format, *args):
        print(f"[whisper-proxy] {args[0]}")


def main():
    parser = argparse.ArgumentParser(description="Whisper OpenAI-compatible proxy")
    parser.add_argument("--port", type=int, default=2022, help="Port to listen on (default: 2022)")
    parser.add_argument(
        "--whisper-url",
        default="http://127.0.0.1:9000",
        help="Upstream whisper service URL (default: http://127.0.0.1:9000)",
    )
    args = parser.parse_args()

    WhisperProxyHandler.whisper_url = args.whisper_url
    server = HTTPServer(("127.0.0.1", args.port), WhisperProxyHandler)
    print(f"[whisper-proxy] Listening on http://127.0.0.1:{args.port}")
    print(f"[whisper-proxy] Forwarding to {args.whisper_url}/asr")
    print(f"[whisper-proxy] OpenAI endpoint: POST /v1/audio/transcriptions")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[whisper-proxy] Shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
