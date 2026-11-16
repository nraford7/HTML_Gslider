"""Linked images: read an `<img src>` / `url(...)` that is a path or an http(s) URL.

resolve_image(src, base_dir) -> (bytes, mime), or FetchError(reason). Spec B2 and review
amendments 3 and 8.

Local paths (relative, absolute, or `file://` with no host) are percent-decoded, joined to
base_dir (the HTML file's folder), resolved with symlinks followed, and must still lie inside
base_dir.resolve(). Anything outside is refused.

Downloads (`http`, `https` only):
- Each hop's host is resolved once. Every address it resolves to must be public: private,
  loopback, link-local, unspecified, multicast, reserved and shared (CGNAT) addresses are
  refused, also when they hide inside an IPv6 form (IPv4-mapped, IPv4-compatible,
  IPv4-translated, NAT64 64:ff9b::/96, 6to4, Teredo).
- The socket connects to that checked address, not to a second DNS answer. HTTPS keeps the
  hostname for SNI and certificate checks, and the Host header carries the hostname.
- Redirects: at most 5; each target must be http(s) and passes the same checks.
- 20 s socket timeout, 120 s for the whole download; at most 50 MB, counted while reading.
- Proxy settings are ignored (a proxy would make the checked address meaningless).
- The bytes must start like a PNG, JPEG or GIF; the server's Content-Type is not trusted.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_BYTES = 50 * 1024 * 1024
TIMEOUT = 20  # seconds, per socket operation
DEADLINE = 120  # seconds, whole download
MAX_REDIRECTS = 5
CHUNK = 64 * 1024

_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
          (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class FetchError(Exception):
    """A linked image could not be read; `reason` says why (it ends up in the fallback reason)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _sniff(data: bytes) -> str:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    raise FetchError("not a PNG, JPEG or GIF image")


def _too_big() -> FetchError:
    return FetchError(f"image is over the {MAX_BYTES / 1024 / 1024:.0f} MB limit")


# --------------------------------------------------------------------- local files

def _local(path_text: str, base_dir: Path) -> tuple[bytes, str]:
    try:
        return _read_local(path_text, base_dir)
    except (OSError, ValueError) as e:  # a NUL byte, a permission error, a symlink loop
        raise FetchError(f"{path_text} cannot be read: {e}") from None


def _read_local(path_text: str, base_dir: Path) -> tuple[bytes, str]:
    base = Path(base_dir).resolve()
    target = (base / path_text).resolve()  # an absolute path_text replaces base; symlinks followed
    if not target.is_relative_to(base):
        raise FetchError(f"{path_text} is outside the HTML file's folder")
    if not target.exists():
        raise FetchError(f"{path_text} not found")
    if not target.is_file():
        raise FetchError(f"{path_text} is not a file")
    if target.stat().st_size > MAX_BYTES:
        raise _too_big()
    data = target.read_bytes()
    return data, _sniff(data)


# --------------------------------------------------------------------- addresses

def _embedded_v4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    out = [a for a in (ip.ipv4_mapped, ip.sixtofour) if a is not None]
    if ip.teredo:
        out.append(ip.teredo[1])
    if ip in _NAT64 or int(ip) >> 32 == 0:  # NAT64, or IPv4-compatible ::a.b.c.d
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if int(ip) >> 32 == 0xFFFF0000:  # IPv4-translated ::ffff:0:a.b.c.d
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return out


def _blocked(ip) -> bool:
    if (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        return any(_blocked(v4) for v4 in _embedded_v4(ip))
    return False


def _vetted_ip(host: str, port: int) -> str:
    """The address to connect to for host; FetchError if any address host resolves to is not public."""
    if not host:
        raise FetchError("URL has no host")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as e:
        raise FetchError(f"cannot resolve {host}: {e}") from None
    ips = []
    for info in infos:
        text = str(info[4][0]).split("%", 1)[0]  # drop an IPv6 zone id
        try:
            ips.append(ipaddress.ip_address(text))
        except ValueError:
            raise FetchError(f"refused: {host} resolves to an unreadable address {text!r}") from None
    if not ips:
        raise FetchError(f"cannot resolve {host}")
    bad = [ip for ip in ips if _blocked(ip)]
    if bad:
        raise FetchError(f"refused: {host} resolves to a private or reserved address ({bad[0]})")
    return str(ips[0])


# --------------------------------------------------------------------- pinned connections

def _tls_context() -> ssl.SSLContext:
    return ssl.create_default_context()


class _PinnedHTTP(http.client.HTTPConnection):
    """Connects to the vetted address of self.host; the Host header keeps the hostname."""

    def connect(self):
        ip = _vetted_ip(self.host, self.port)
        self.sock = socket.create_connection((ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    """As _PinnedHTTP, then TLS with SNI and certificate checks against the hostname."""

    def connect(self):
        ip = _vetted_ip(self.host, self.port)
        sock = socket.create_connection((ip, self.port), self.timeout)
        try:
            self.sock = _tls_context().wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PinnedHTTP, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPS, req)


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        visited = getattr(req, "redirect_dict", {})
        if len(visited) >= MAX_REDIRECTS:
            raise FetchError(f"too many redirects (more than {MAX_REDIRECTS})")
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme.lower() not in ("http", "https"):
            raise FetchError(f"redirect to {parts.scheme or 'a relative'}: URL refused (http and https only)")
        _vetted_ip(parts.hostname or "", parts.port or (443 if parts.scheme.lower() == "https" else 80))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    """Only these handlers: no proxy, file, ftp or data handler."""
    op = urllib.request.OpenerDirector()
    for h in (_HTTPHandler(), _HTTPSHandler(), _Redirects(), urllib.request.HTTPDefaultErrorHandler(),
              urllib.request.HTTPErrorProcessor()):
        op.add_handler(h)
    return op


def _download(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "gslider", "Accept": "image/*"})
    start = time.monotonic()
    try:
        with _opener().open(req, timeout=TIMEOUT) as resp:
            length = resp.headers.get("Content-Length")
            if length and length.strip().isdigit() and int(length) > MAX_BYTES:
                raise _too_big()
            buf = bytearray()
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_BYTES:
                    raise _too_big()
                if time.monotonic() - start > DEADLINE:
                    raise FetchError(f"download took longer than {DEADLINE} s")
    except FetchError:
        raise
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            raise FetchError(f"too many redirects (more than {MAX_REDIRECTS})") from None
        raise FetchError(f"HTTP {e.code} from {urllib.parse.urlsplit(url).hostname}") from None
    except urllib.error.URLError as e:
        raise FetchError(f"download failed: {e.reason}") from None
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise FetchError(f"download failed: {e or repr(e)}") from None
    data = bytes(buf)
    return data, _sniff(data)


# --------------------------------------------------------------------- entry point

def resolve_image(src: str, base_dir: Path) -> tuple[bytes, str]:
    """Bytes and mime ("image/png", "image/jpeg" or "image/gif") of a linked image."""
    src = (src or "").strip()
    if not src:
        raise FetchError("empty image source")
    if src.startswith("//"):
        raise FetchError(f"{src}: a URL without a scheme is not supported")
    parts = urllib.parse.urlsplit(src)
    scheme = parts.scheme.lower()
    if scheme in ("http", "https"):
        if not parts.hostname:
            raise FetchError(f"{src}: URL has no host")
        return _download(src)
    if scheme == "file":
        if parts.netloc not in ("", "localhost"):
            raise FetchError(f"{src}: a file URL with a host is not supported")
        return _local(urllib.parse.unquote(parts.path), base_dir)
    if scheme:
        raise FetchError(f"{scheme}: images are not supported (http, https and local paths only)")
    return _local(urllib.parse.unquote(parts.path), base_dir)
