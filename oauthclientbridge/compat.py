# flake8: noqa

import sys

if sys.version_info[0] == 3:
    from http.client import responses
    from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

    text_type = str
else:
    from urllib import urlencode

    from httplib import responses
    from urlparse import parse_qs, urlsplit, urlunsplit

    text_type = unicode

__all__ = [
    "responses",
    "parse_qs",
    "urlencode",
    "urlsplit",
    "urlunsplit",
    "text_type",
]
