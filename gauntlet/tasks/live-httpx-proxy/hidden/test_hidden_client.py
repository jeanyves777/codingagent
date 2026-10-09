import httpx

from client import make_client


def test_routes_https_through_proxy():
    client = make_client("http://proxy.internal:8080")
    transport = client._transport_for_url(httpx.URL("https://inventory.example.com/items"))
    assert transport._pool._proxy_url.host == b"proxy.internal"
    assert client.timeout.connect == 10
    client.close()
