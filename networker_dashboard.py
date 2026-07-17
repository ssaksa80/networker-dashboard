#!/usr/bin/env python3
"""
Single-file HTTPS dashboard for Dell NetWorker backup and recovery status.

The password is accepted only for the current browser request. This server does
not write credentials to disk, does not place them in URLs, and serves every
response with no-store cache headers.

This is a thin launcher; the implementation lives in the nwdash package.
"""

from nwdash.main import main

if __name__ == "__main__":
    raise SystemExit(main())
