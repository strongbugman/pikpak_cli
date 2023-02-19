import logging

import httpx

ANT_PACKAGES = ["cli"]

# httpx config, see httpx.Client.__init__ for more detail
HTTPX_CONFIG = {
    "timeout": 5.0,
    "max_redirects": 20,
    "limits": httpx.Limits(max_connections=10, max_keepalive_connections=20),
    "trust_env": True,
    "proxies": None,
    "auth": None,
    "headers": None,
    "cookies": None,
}

POOL_CONFIG = {
    "limit": 100,
}
REPORTER = {
    "slot": 6000000000000000000000000000000000000000000000000000000,
}


# ANT config
HTTP_RETRIES = 0
HTTP_RETRY_DELAY = 5

# PikPak
PIKPAK_CLIENT_ID = "YNxT9w7GMdWvEOKa"
PIKPAK_CLIENT_SECRET = "dbw2OtmVEeuUvIptb1Coyg"


logging.basicConfig(level=logging.INFO)
