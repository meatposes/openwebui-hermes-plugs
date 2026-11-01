#!/usr/bin/env python3
"""Sign in to the Hermes dashboard once and print a refresh token for the Hermes Media filter.

Usage:  python3 get_refresh_token.py http://hermes-host:9119

You are asked for the dashboard username and password. The password is sent only to the
dashboard and is never printed or saved. Paste the printed token into the filter's
"Refresh Token" setting in Open WebUI.
"""

import getpass
import json
import sys
import urllib.error
import urllib.request

RT_COOKIE = "hermes_session_rt"


def post_json(url: str, payload: dict) -> urllib.request.addinfourl:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    return urllib.request.urlopen(request, timeout=30)


def fail(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].startswith(("http://", "https://")):
        fail("give the dashboard address, for example: python3 get_refresh_token.py http://hermes-host:9119")
    base = sys.argv[1].rstrip("/")
    username = input("Dashboard username: ").strip()
    password = getpass.getpass("Dashboard password: ")

    try:
        resp = post_json(f"{base}/auth/password-login",
                         {"provider": "basic", "username": username, "password": password})
    except urllib.error.HTTPError as exc:
        fail({401: "wrong username or password",
              404: "this dashboard does not use username/password login",
              429: "too many attempts, wait a minute and try again"}.get(exc.code, f"login failed ({exc.code})"))
    except urllib.error.URLError as exc:
        fail(f"cannot reach {base} ({exc.reason})")

    refresh_token = ""
    for header in resp.headers.get_all("Set-Cookie") or []:
        name, _, rest = header.partition("=")
        if name.strip().endswith(RT_COOKIE):
            refresh_token = rest.split(";", 1)[0]
    if not refresh_token:
        fail("login worked but the dashboard returned no refresh token")

    # Trade it once: proves the token works and returns a fresh one for the filter.
    try:
        data = json.load(post_json(f"{base}/auth/native/refresh", {"refresh_token": refresh_token}))
    except urllib.error.HTTPError as exc:
        fail(f"token check failed ({exc.code})")
    print("\nRefresh token (paste into the filter's Refresh Token setting):\n")
    print(data["refresh_token"])


if __name__ == "__main__":
    main()
