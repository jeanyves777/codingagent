"""HTTP client factory for the inventory service."""
import httpx


def make_client(proxy_url):
    """Return an httpx.Client that sends all HTTPS traffic through proxy_url."""
    return httpx.Client(proxies={"https://": proxy_url}, timeout=10)
