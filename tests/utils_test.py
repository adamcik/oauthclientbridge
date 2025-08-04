from oauthclientbridge import utils


def test_rewrite_uri() -> None:
    result = utils.rewrite_uri("http://example.com/path?a=1&b=2", {"b": "3", "c": "4"})
    assert result == "http://example.com/path?a=1&b=3&c=4"

    result = utils.rewrite_uri("http://example.com/path", {"a": "1"})
    assert result == "http://example.com/path?a=1"

    result = utils.rewrite_uri("http://example.com/path?a=1", {"a": "2"})
    assert result == "http://example.com/path?a=2"

    result = utils.rewrite_uri("http://example.com/path?a=1#fragment", {"b": "3"})
    assert result == "http://example.com/path?a=1&b=3#fragment"
