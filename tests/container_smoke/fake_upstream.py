import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode())
        expected = {
            "client_id": ["smoke-client"],
            "client_secret": ["smoke-secret"],
            "code": ["smoke-code"],
            "grant_type": ["authorization_code"],
            "redirect_uri": ["https://auth.example.com/spotify/callback"],
        }
        if self.path != "/token" or form != expected:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
            return

        self._respond(
            HTTPStatus.OK,
            {
                "access_token": "smoke-provider-token",
                "token_type": "Bearer",
            },
        )

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args

    def _respond(self, status: HTTPStatus, payload: dict[str, str]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port-file", required=True, type=Path)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    args.port_file.write_text(str(server.server_port))
    server.serve_forever()


if __name__ == "__main__":
    main()
