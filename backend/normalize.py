"""Normalization shared by the compiler, the gate, and the executor pre-check.

Two distinct jobs that must not be confused:

  * `lowercase_url_host` normalizes a URL for *storage* -- it lowercases scheme and
    host and otherwise leaves the URL alone, so what we persist is what the user
    approved.
  * `comparison_host` normalizes a host for *matching* -- it additionally drops a
    leading `www.`, because `www.shop.com` and `shop.com` are the same merchant.

Only comparison strips; storage never does. Mixing them would mean the mandate card
shows a URL the user did not write.
"""

import re
from urllib.parse import urlsplit, urlunsplit

NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def lowercase_url_host(url):
    """Lowercase scheme and host, preserve everything else verbatim."""
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, parts.fragment)
    )


def comparison_host(url):
    """Host for equality checks: lowercased, port and trailing dot dropped, `www.` stripped."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def visa_safe_name(name):
    """Merchant name reduced to the Visa-safe character set: `H&M` -> `HM`.

    Uppercased so comparison is case-insensitive, matching how the name is
    sanitized before it is forwarded to the network.
    """
    return NON_ALNUM.sub("", name or "").upper()
