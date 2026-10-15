"""URL の正規化と content_hash（specs/003-normalize-dedupe.md）。"""

import hashlib
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

# 末尾が * のものは前方一致、それ以外は完全一致で取り除く
DEFAULT_TRACKING_PARAMS: tuple[str, ...] = (
    "utm_*",
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
)

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(url: str, tracking_params: Iterable[str] = DEFAULT_TRACKING_PARAMS) -> str | None:
    """URL を正規化する。http / https でない、またはホスト名が無ければ None を返す。"""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    host = parts.hostname  # urllib が小文字にしている
    if scheme not in _DEFAULT_PORTS or not host:
        return None

    netloc = host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"

    path = parts.path.rstrip("/") or "/"
    query = _clean_query(parts.query, tuple(tracking_params))
    return urlunsplit((scheme, netloc, path, query, ""))


def _clean_query(query: str, tracking_params: tuple[str, ...]) -> str:
    """追跡パラメータを除き、名前の順に並べる。値のエンコードは変えない。"""
    pairs = [pair for pair in query.split("&") if pair]
    kept = [pair for pair in pairs if not _is_tracking(_param_name(pair), tracking_params)]
    kept.sort(key=_param_name)  # 安定ソートなので、同じ名前どうしの順は保たれる
    return "&".join(kept)


def _param_name(pair: str) -> str:
    return pair.split("=", 1)[0]


def _is_tracking(name: str, tracking_params: tuple[str, ...]) -> bool:
    return any(
        name.startswith(pattern[:-1]) if pattern.endswith("*") else name == pattern
        for pattern in tracking_params
    )


def content_hash(title: str, content: str) -> str:
    """空白の違いを無視した title と content の SHA-256（16 進の小文字）。"""
    text = f"{_squash(title)}\n{_squash(content)}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _squash(text: str) -> str:
    return " ".join(text.split())
