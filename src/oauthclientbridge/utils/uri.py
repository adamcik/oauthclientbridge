import urllib.parse

URIParam = dict[str, str]


def _rewrite_query(original: str, params: URIParam) -> str:
    parts = []
    query = urllib.parse.parse_qs(original, keep_blank_values=True)
    for p, value in params.items():
        query[p] = [value]  # Override with new params.
    for q, values in query.items():
        for value in values:  # Turn query into list of tuples.
            if isinstance(value, str):
                parts.append((q, value.encode("utf-8")))
            else:
                parts.append((q, value))
    return urllib.parse.urlencode(parts)


def rewrite_uri(uri: str, params: URIParam) -> str:
    scheme, netloc, path, query, fragment = urllib.parse.urlsplit(uri)
    query = _rewrite_query(query, params)
    return urllib.parse.urlunsplit((scheme, netloc, path, query, fragment))
