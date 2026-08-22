#!/usr/bin/env python3
"""
Single-file HTTPS dashboard for Dell NetWorker backup and recovery status.

The password is accepted only for the current browser request. This server does
not write credentials to disk, does not place them in URLs, and serves every
response with no-store cache headers.

This is a thin launcher; the implementation lives in the nwdash package.
"""

import sys


def _launch() -> int:
    # Key material is loaded during import (nwdash.secrets). An unusable key file
    # is refused rather than silently replaced — every stored credential depends
    # on it — so turn that into an operator-readable message, not a traceback.
    try:
        from nwdash.main import main
    except Exception as exc:
        if type(exc).__name__ != "KeyMaterialError":
            raise
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return main()


if __name__ == "__main__":
    raise SystemExit(_launch())
