"""Variational Omni: discovery only.

The project has no Variational connector; its one known public endpoint is
`metadata/stats` (connections.py), whose body has a `listings` list. Nothing about its
fields is declared or documented here, so this client parses no market, funding or
settlement values. Each poll stores the raw response and a schema record (every JSON
path seen, with its value types); the report lists what the venue publishes, and a
later phase decides from that evidence which fields mean what. RFQ quotes may be
cached by the venue (the project notes up to 600 s), so the report also measures how
often the body actually changes.
"""

import hashlib
import json

from .base import Client, SchemaError

URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"
ENDPOINT = "metadata/stats"
MAX_PATHS = 400
MAX_DEPTH = 8


def shape(obj):
    """{JSON path: sorted value types} of a response; list elements are merged under
    `[]`. Returns (paths, truncated)."""
    paths = {}
    truncated = False

    def walk(x, path, depth):
        nonlocal truncated
        if len(paths) >= MAX_PATHS and path not in paths:
            truncated = True
            return
        paths.setdefault(path, set()).add(_type(x))
        if depth >= MAX_DEPTH:
            truncated = truncated or isinstance(x, (dict, list)) and bool(x)
            return
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, "%s.%s" % (path, k), depth + 1)
        elif isinstance(x, list):
            for v in x:
                walk(v, path + "[]", depth + 1)

    walk(obj, "$", 0)
    return {p: sorted(t) for p, t in sorted(paths.items())}, truncated


def _type(x):
    if x is None:
        return "null"
    if isinstance(x, bool):
        return "bool"
    if isinstance(x, (int, float)):
        return "number"
    if isinstance(x, str):
        return "string"
    if isinstance(x, list):
        return "array"
    return "object"


def listing_keys(obj):
    """{key: listings carrying it} over the `listings` list (None when it is absent)."""
    rows = obj.get("listings") if isinstance(obj, dict) else None
    if not isinstance(rows, list):
        return None, None
    keys = {}
    for r in rows:
        if isinstance(r, dict):
            for k in r:
                keys[k] = keys.get(k, 0) + 1
    return len(rows), dict(sorted(keys.items()))


def sample(obj, limit=60):
    """The first listing as published, long strings cut, for the report only."""
    rows = obj.get("listings") if isinstance(obj, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None

    def cut(v):
        if isinstance(v, str) and len(v) > limit:
            return v[:limit] + "..."
        if isinstance(v, (dict, list)):
            text = json.dumps(v, ensure_ascii=False)
            return text if len(text) <= limit else text[:limit] + "..."
        return v

    return {k: cut(v) for k, v in rows[0].items()}


def schema_record(obj, at, sha):
    paths, truncated = shape(obj)
    count, keys = listing_keys(obj)
    digest = hashlib.sha256(json.dumps(paths, sort_keys=True).encode()).hexdigest()
    return dict(
        venue="Variational",
        market_id=ENDPOINT,
        endpoint=ENDPOINT,
        schema_sha256=digest,
        paths=paths,
        paths_truncated=truncated,
        listing_count=count,
        listing_keys=keys,
        first_seen_at=at,
        raw_sha256=sha,
    )


class Variational(Client):
    venue = "Variational"
    discovery = True
    tasks = ("discovery",)

    def poll(self, at):
        obj, sha = self.http.request(ENDPOINT, URL)
        if not isinstance(obj, (dict, list)):
            raise SchemaError("metadata/stats: JSON nesnesi değil")
        return obj, sha, schema_record(obj, at, sha)

    def replay(self, endpoint, obj, ctx, at, sha):
        if endpoint != ENDPOINT:
            return {}
        return dict(schema=[schema_record(obj, at, sha)])
