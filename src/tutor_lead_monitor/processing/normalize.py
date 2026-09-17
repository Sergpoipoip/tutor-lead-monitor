import hashlib
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

URL_PATTERN = re.compile(r'https?://[^\s<>"«»]+', re.IGNORECASE)
PHONE_PATTERN = re.compile(
    r"(?<!\w)(?:\+?7|8)[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}(?!\d)"
)
TRACKING_KEYS = {"fbclid", "gclid", "yclid", "ysclid", "ref", "referrer"}
PUNCTUATION = str.maketrans(
    {"«": '"', "»": '"', "“": '"', "”": '"', "’": "'", "—": "-", "–": "-", "…": "..."}
)


@dataclass(frozen=True)
class NormalizedText:
    readable: str
    fingerprint_text: str
    exact_fingerprint: str


def canonical_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() not in {"https", "http"} or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        port = parts.port
        host = parts.hostname.lower().encode("idna").decode("ascii")
        if ":" in host:
            host = f"[{host}]"
        if port is not None and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        query = urlencode(
            sorted(
                (k, v)
                for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if not k.lower().startswith("utm_") and k.lower() not in TRACKING_KEYS
            )
        )
        return urlunsplit((parts.scheme.lower(), host, parts.path or "/", query, ""))
    except (ValueError, UnicodeError):
        return None


def contact_signature(text: str) -> tuple[str, ...]:
    """Comparison-only hashes; differing known phone contacts block a text merge."""
    numbers = {"7" + re.sub(r"\D", "", m.group())[1:] for m in PHONE_PATTERN.finditer(text)}
    return tuple(sorted(hashlib.sha256(number.encode()).hexdigest() for number in numbers))


def normalize(text: str, boilerplate: tuple[str, ...] = ()) -> NormalizedText:
    readable = unicodedata.normalize("NFKC", text).translate(PUNCTUATION)
    readable = "".join(c for c in readable if unicodedata.category(c) != "Cf")
    readable = URL_PATTERN.sub(
        lambda m: (
            (canonical_url(m.group().rstrip(".,;!?)")) or "[url]")
            + m.group()[len(m.group().rstrip(".,;!?)")) :]
        ),
        readable,
    )
    readable = re.sub(r"\s+", " ", readable).strip()
    fingerprint = readable.casefold().replace("ё", "е")
    for phrase in boilerplate:
        phrase = (
            unicodedata.normalize("NFKC", phrase)
            .translate(PUNCTUATION)
            .casefold()
            .replace("ё", "е")
        )
        fingerprint = fingerprint.replace(phrase, " ")
    fingerprint = URL_PATTERN.sub(" urltoken ", fingerprint)
    fingerprint = PHONE_PATTERN.sub(" phonetoken ", fingerprint)
    fingerprint = " ".join(re.findall(r"[^\W_]+", fingerprint, re.UNICODE))
    return NormalizedText(readable, fingerprint, hashlib.sha256(fingerprint.encode()).hexdigest())
