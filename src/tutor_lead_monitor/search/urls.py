"""Conservative discovery URL identity without network/DNS lookups."""

import hashlib
import ipaddress
from urllib.parse import unquote_plus, urlsplit, urlunsplit

TRACKING_KEYS = frozenset({"fbclid", "gclid", "yclid", "ysclid", "msclkid"})


def public_url(value: str) -> str | None:
    if len(value) > 4096 or any(ord(c) <= 32 for c in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        host = parts.hostname.lower().encode("idna").decode("ascii")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            host = host.removesuffix(".")
            if (
                "." not in host
                or len(host) > 253
                or host in {"localhost", "local", "internal", "test"}
                or all(char.isdigit() or char == "." for char in host)
                or host.endswith((".localhost", ".local", ".internal", ".test"))
                or any(
                    not label
                    or len(label) > 63
                    or label.startswith("-")
                    or label.endswith("-")
                    or not all(char.isascii() and (char.isalnum() or char == "-") for char in label)
                    for label in host.split(".")
                )
            ):
                return None
        else:
            if not address.is_global:
                return None
            if address.version == 6:
                host = f"[{host}]"
        port = parts.port
        if port == 0:
            return None
        if port is not None and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        # Preserve order, repeats, encoding, and meaningful keys such as ref/id/search.
        query = "&".join(
            part
            for part in parts.query.split("&")
            if not (
                unquote_plus(part.split("=", 1)[0]).lower().startswith("utm_")
                or unquote_plus(part.split("=", 1)[0]).lower() in TRACKING_KEYS
            )
        )
        return urlunsplit((parts.scheme.lower(), host, parts.path or "/", query, ""))
    except (ValueError, UnicodeError):
        return None


def external_id(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
