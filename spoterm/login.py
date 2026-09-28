"""SpoTerm's own sign-in (your app's client id, PKCE), run as its own short-lived process.

Signing in needs TLS and an HTTP client, which the running UI never loads (the engine
makes its API calls). Running the login separately keeps those modules out of the
UI's memory for good. Exit code 0 means signed in.
"""

import sys

from . import auth, config


def main() -> int:
    try:
        settings = config.load()
        auth.Auth(settings).login()
    except (KeyboardInterrupt, EOFError):
        return 1
    except Exception as e:
        print(f"Spotify sign-in failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
