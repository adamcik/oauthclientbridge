import argparse
import base64
import http.client
import json
import socket
import urllib.parse
from contextlib import closing
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path) -> None:
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(str(self.socket_path))
        self.sock = connection


def request(
    socket_path: Path,
    method: str,
    path: str,
    *,
    body: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[HTTPStatus, dict[str, str], bytes]:
    forwarded = {
        "X-Forwarded-For": "192.0.2.1",
        "X-Forwarded-Host": "auth.example.com",
        "X-Forwarded-Port": "443",
        "X-Forwarded-Proto": "https",
    }
    if headers is not None:
        forwarded.update(headers)

    with closing(UnixHTTPConnection(socket_path)) as connection:
        connection.request(method, path, body=body, headers=forwarded)
        response = connection.getresponse()
        return (
            HTTPStatus(response.status),
            {name.lower(): value for name, value in response.getheaders()},
            response.read(),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", choices=("asgi", "wsgi"), required=True)
    parser.add_argument("socket", type=Path)
    args = parser.parse_args()

    status, headers, _ = request(args.socket, "GET", "/?state=caller-state")
    assert status == HTTPStatus.FOUND, status
    location = urllib.parse.urlsplit(headers["location"])
    assert location.scheme == "http", location
    assert location.hostname == "127.0.0.1", location
    authorization_query = urllib.parse.parse_qs(location.query)
    assert authorization_query["client_id"] == ["smoke-client"]
    assert authorization_query["redirect_uri"] == [
        "https://auth.example.com/spotify/callback"
    ]
    state = authorization_query["state"][0]

    cookie = SimpleCookie()
    cookie.load(headers["set-cookie"])
    session = cookie["session"]
    if args.runtime == "asgi":
        assert session["domain"] == "auth.example.com", session
        assert session["path"] == "/spotify", session
    else:
        assert not session["domain"], session
        assert session["path"] == "/", session
    assert session["secure"], session
    assert not session["expires"], session
    assert not session["max-age"], session

    callback_query = urllib.parse.urlencode({"code": "smoke-code", "state": state})
    status, _, body = request(
        args.socket,
        "GET",
        f"/callback?{callback_query}",
        headers={"Cookie": f"session={session.value}"},
    )
    assert status == HTTPStatus.OK, (status, body)
    credentials = json.loads(body)

    basic = base64.b64encode(
        f"{credentials['client_id']}:{credentials['client_secret']}".encode()
    ).decode()
    status, headers, body = request(
        args.socket,
        "POST",
        "/token",
        body="grant_type=client_credentials",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "container-smoke",
        },
    )
    assert status == HTTPStatus.OK, (status, body)
    assert json.loads(body) == {
        "access_token": "smoke-provider-token",
        "token_type": "Bearer",
    }
    assert headers["cache-control"] == "no-store", headers

    status, _, body = request(args.socket, "GET", "/metrics")
    assert status == HTTPStatus.OK, (status, body)
    assert b"oauth_build_info" in body

    status, _, _ = request(args.socket, "GET", "/not-found")
    assert status == HTTPStatus.NOT_FOUND, status
    status, _, _ = request(args.socket, "GET", "/token")
    assert status == HTTPStatus.METHOD_NOT_ALLOWED, status


if __name__ == "__main__":
    main()
