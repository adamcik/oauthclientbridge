from oauthclientbridge.utils import uri


def test_rewrite_uri() -> None:
    result = uri.rewrite_uri("http://example.com/path?a=1&b=2", {"b": "3", "c": "4"})
    assert result == "http://example.com/path?a=1&b=3&c=4"

    result = uri.rewrite_uri("http://example.com/path", {"a": "1"})
    assert result == "http://example.com/path?a=1"

    result = uri.rewrite_uri("http://example.com/path?a=1", {"a": "2"})
    assert result == "http://example.com/path?a=2"

    result = uri.rewrite_uri("http://example.com/path?a=1#fragment", {"b": "3"})
    assert result == "http://example.com/path?a=1&b=3#fragment"
