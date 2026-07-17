import urllib.parse

URIParam = dict[str, str]
REDACTED_URL_VALUE = "<REDACTED>"


def _rewrite_query(original: str, params: URIParam) -> str:
    parts: list[tuple[str, str]] = []
    query = urllib.parse.parse_qs(original, keep_blank_values=True)
    for p, value in params.items():
        query[p] = [value]  # Override with new params.
    for q, values in query.items():
        for value in values:  # Turn query into list of tuples.
            parts.append((q, value))
    return urllib.parse.urlencode(parts)


def rewrite_uri(uri: str, params: URIParam) -> str:
    scheme, netloc, path, query, fragment = urllib.parse.urlsplit(uri)
    query = _rewrite_query(query, params)
    return urllib.parse.urlunsplit((scheme, netloc, path, query, fragment))


def sanitize_url(url: str | None) -> str | None:
    if url is None:
        return None

    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url

    filtered_query = "&".join(
        f"{urllib.parse.quote(key, safe='')}={REDACTED_URL_VALUE}"
        for key, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    )
    return urllib.parse.urlunsplit(parts._replace(query=filtered_query))
