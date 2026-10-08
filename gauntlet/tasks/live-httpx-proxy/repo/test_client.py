import httpx

from client import make_client


def test_make_client_returns_client():
    client = make_client("http://proxy.internal:8080")
    assert isinstance(client, httpx.Client)
    client.close()
