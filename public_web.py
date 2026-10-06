"""Shared public HTTP destination checks for page fetching and sandbox egress."""
import ipaddress
import socket
from urllib.parse import urlsplit


def destination(host, port):
    if not isinstance(host, str) or not host or len(host) > 253 or any(c.isspace() for c in host):
        raise ValueError('Invalid web hostname.')
    host = host.rstrip('.').lower()
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        raise ValueError('Only public internet destinations are allowed.')
    if port not in (80, 443):
        raise ValueError('Only public HTTP/HTTPS ports 80 and 443 are allowed.')
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses=[ipaddress.ip_address(record[4][0].split('%')[0]) for record in records]
    if not records or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in addresses):
        raise ValueError('Private, loopback, link-local and metadata addresses are blocked.')
    return records


def connect_public(host, port, timeout=10):
    # Resolve, validate every answer, then connect to that numeric address. No second DNS lookup.
    records = destination(host, port)
    last = None
    for family, kind, protocol, _, address in records:
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(timeout)
            connection.connect(address)
            return connection
        except OSError as error:
            last = error
            connection.close()
    raise OSError('Could not connect to the public web destination.') from last


def web_url(url):
    if not isinstance(url, str) or len(url) > 4000 or any(ord(c) < 32 for c in url):
        raise ValueError('Invalid URL.')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError('Use a public http:// or https:// URL without credentials.')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    if port != (443 if parsed.scheme == 'https' else 80):
        raise ValueError('Use standard HTTP/HTTPS ports.')
    return parsed, port
