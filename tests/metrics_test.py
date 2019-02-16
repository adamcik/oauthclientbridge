def test_metrics(client):
    resp = client.get('/metrics')

    assert 200 == resp.status_code
    assert 'oauth_tokens_count{state="active"} 0.0' in resp.data
