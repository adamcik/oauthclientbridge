def test_metrics(client):
    resp = client.get('/metrics')

    assert 200 == resp.status_code
    assert b'auth_server_error_total' in resp.data
