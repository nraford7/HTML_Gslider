"""Linked images (Task 8): local paths and http(s) downloads, with the address checks.

No test touches the network: DNS (getaddrinfo) and the TCP connection are replaced by fakes
that answer with canned HTTP responses, so the real redirect handler and pinned connection run."""
import io
import socket
import ssl

import pytest

from gslider import fetch
from gslider.fetch import FetchError, resolve_image

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32


class FakeNet:
    """DNS: host -> [ip]. Routes: (host, path) -> (status, headers, body).

    create_connection records the address dialled; the request's Host header and path pick
    the canned response, so a test can see which IP was used for which host."""

    def __init__(self, monkeypatch):
        self.dns: dict[str, list[str]] = {}
        self.routes: dict[tuple[str, str], tuple] = {}
        self.dialled: list[tuple[str, int]] = []
        self.requests: list[bytes] = []
        monkeypatch.setattr(fetch.socket, "getaddrinfo", self.getaddrinfo)
        monkeypatch.setattr(fetch.socket, "create_connection", self.create_connection)

    def getaddrinfo(self, host, port, *a, **k):
        try:
            ips = self.dns[host]
        except KeyError:
            raise socket.gaierror(8, "nodename nor servname provided") from None
        fam = lambda ip: socket.AF_INET6 if ":" in ip else socket.AF_INET  # noqa: E731
        return [(fam(ip), socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    def create_connection(self, address, timeout=None, *a, **k):
        self.dialled.append(address)
        return FakeSock(self)

    def respond(self, raw: bytes) -> bytes:
        self.requests.append(raw)
        head = raw.split(b"\r\n\r\n", 1)[0].decode()
        lines = head.split("\r\n")
        path = lines[0].split(" ")[1]
        host = next(h.split(":", 1)[1].strip() for h in lines[1:] if h.lower().startswith("host:"))
        status, headers, body = self.routes.get((host.split(":")[0], path), (404, {}, b"nope"))
        hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        if "Content-Length" not in headers and "Transfer-Encoding" not in headers:
            hdr += "Connection: close\r\n"
        return f"HTTP/1.1 {status} X\r\n{hdr}\r\n".encode() + body


class FakeSock:
    def __init__(self, net):
        self.net, self.sent = net, b""

    def sendall(self, data):
        self.sent += data

    def makefile(self, mode, *a, **k):
        return io.BytesIO(self.net.respond(self.sent))

    def settimeout(self, t):
        pass

    def setsockopt(self, *a):
        pass

    def close(self):
        pass


@pytest.fixture
def net(monkeypatch):
    return FakeNet(monkeypatch)


def ok(body, ctype="image/png"):
    return 200, {"Content-Type": ctype, "Content-Length": str(len(body))}, body


# --- local paths ---------------------------------------------------------------

def test_relative_path_inside_folder(tmp_path):
    (tmp_path / "img").mkdir()
    (tmp_path / "img" / "logo.png").write_bytes(PNG)
    (tmp_path / "my logo.jpg").write_bytes(JPEG)
    assert resolve_image("img/logo.png", tmp_path) == (PNG, "image/png")
    assert resolve_image("./img/logo.png?v=2#x", tmp_path) == (PNG, "image/png")
    assert resolve_image("my%20logo.jpg", tmp_path) == (JPEG, "image/jpeg")
    assert resolve_image((tmp_path / "img" / "logo.png").as_uri(), tmp_path) == (PNG, "image/png")


@pytest.mark.parametrize("src", ["../secret.png", "img/../../secret.png", "/etc/hosts",
                                 "file:///etc/hosts", "file://otherhost/share/x.png"])
def test_path_outside_folder_refused(tmp_path, src):
    deck = tmp_path / "deck"
    deck.mkdir()
    (tmp_path / "secret.png").write_bytes(PNG)
    with pytest.raises(FetchError) as e:
        resolve_image(src, deck)
    assert "outside" in e.value.reason or "host" in e.value.reason


def test_absolute_path_inside_folder_allowed(tmp_path):
    (tmp_path / "a.gif").write_bytes(GIF)
    assert resolve_image(str(tmp_path / "a.gif"), tmp_path) == (GIF, "image/gif")


def test_symlink_escape_refused(tmp_path):
    deck = tmp_path / "deck"
    deck.mkdir()
    (tmp_path / "secret.png").write_bytes(PNG)
    (deck / "link.png").symlink_to(tmp_path / "secret.png")
    with pytest.raises(FetchError, match="outside"):
        resolve_image("link.png", deck)


def test_missing_and_non_image_local_files(tmp_path):
    with pytest.raises(FetchError, match="not found"):
        resolve_image("nope.png", tmp_path)
    (tmp_path / "page.png").write_bytes(b"<html>hi</html>")
    with pytest.raises(FetchError, match="not a PNG, JPEG or GIF"):
        resolve_image("page.png", tmp_path)
    (tmp_path / "dir.png").mkdir()
    with pytest.raises(FetchError, match="not a file"):
        resolve_image("dir.png", tmp_path)


def test_unreadable_local_path_is_fetch_error(tmp_path):
    with pytest.raises(FetchError, match="cannot be read"):
        resolve_image("a%00b.png", tmp_path)


@pytest.mark.parametrize("src", ["ftp://example.com/a.png", "javascript:alert(1)", "//example.com/a.png",
                                 "data:image/png;base64,AAAA", "gopher://x/a.png"])
def test_other_schemes_refused(tmp_path, src):
    with pytest.raises(FetchError):
        resolve_image(src, tmp_path)


# --- downloads -----------------------------------------------------------------------

def test_http_download_checks_bytes(net, tmp_path):
    net.dns["cdn.example.com"] = ["93.184.216.34"]
    net.routes[("cdn.example.com", "/a.png")] = ok(PNG)
    net.routes[("cdn.example.com", "/b.jpg")] = ok(JPEG, "application/octet-stream")  # bytes decide
    net.routes[("cdn.example.com", "/c.gif")] = ok(GIF)
    net.routes[("cdn.example.com", "/page.png")] = ok(b"<!doctype html><html></html>", "image/png")
    assert resolve_image("http://cdn.example.com/a.png", tmp_path) == (PNG, "image/png")
    assert resolve_image("http://cdn.example.com/b.jpg", tmp_path) == (JPEG, "image/jpeg")
    assert resolve_image("http://cdn.example.com/c.gif", tmp_path) == (GIF, "image/gif")
    with pytest.raises(FetchError, match="not a PNG, JPEG or GIF"):
        resolve_image("http://cdn.example.com/page.png", tmp_path)
    with pytest.raises(FetchError, match="HTTP 404"):
        resolve_image("http://cdn.example.com/missing.png", tmp_path)
    assert all(addr == ("93.184.216.34", 80) for addr in net.dialled)
    assert b"Host: cdn.example.com\r\n" in net.requests[0]


def test_connects_to_the_checked_ip(net, tmp_path, monkeypatch):
    """DNS is asked once per hop; the socket goes to that vetted IP, not to a second lookup."""
    answers = [["93.184.216.34"], ["127.0.0.1"]]  # a rebinding resolver: public first, then loopback
    monkeypatch.setattr(fetch.socket, "getaddrinfo", lambda host, port, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in answers.pop(0)])
    net.routes[("cdn.example.com", "/a.png")] = ok(PNG)
    assert resolve_image("http://cdn.example.com/a.png", tmp_path)[0] == PNG
    assert net.dialled == [("93.184.216.34", 80)]


def test_https_pins_ip_and_keeps_sni(net, tmp_path, monkeypatch):
    wrapped = []

    class Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            wrapped.append(server_hostname)
            return sock

    monkeypatch.setattr(fetch, "_tls_context", lambda: Ctx())
    net.dns["img.example.org"] = ["2606:2800:220:1:248:1893:25c8:1946"]
    net.routes[("img.example.org", "/x.png")] = ok(PNG)
    assert resolve_image("https://img.example.org/x.png", tmp_path)[0] == PNG
    assert net.dialled == [("2606:2800:220:1:248:1893:25c8:1946", 443)]
    assert wrapped == ["img.example.org"]


def test_tls_context_verifies_certificates():
    ctx = fetch._tls_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254",
                                "0.0.0.0", "100.64.0.1", "224.0.0.1", "240.0.0.1", "255.255.255.255",
                                "::1", "::", "fe80::1", "fc00::1", "ff02::1"])
def test_private_address_refused(net, tmp_path, ip):
    net.dns["evil.example.com"] = [ip]
    net.routes[("evil.example.com", "/a.png")] = ok(PNG)
    with pytest.raises(FetchError, match="refused"):
        resolve_image("http://evil.example.com/a.png", tmp_path)
    assert net.dialled == []


@pytest.mark.parametrize("url", ["http://127.0.0.1/a.png", "http://[::1]/a.png", "http://169.254.169.254/latest",
                                 "http://[::ffff:127.0.0.1]/a.png", "http://localhost/a.png"])
def test_ip_literal_and_localhost_refused(net, tmp_path, url):
    net.dns.update({"127.0.0.1": ["127.0.0.1"], "::1": ["::1"], "169.254.169.254": ["169.254.169.254"],
                    "::ffff:127.0.0.1": ["::ffff:127.0.0.1"], "localhost": ["127.0.0.1"]})
    with pytest.raises(FetchError, match="refused"):
        resolve_image(url, tmp_path)
    assert net.dialled == []


@pytest.mark.parametrize("ip", ["::ffff:127.0.0.1", "::ffff:10.1.2.3", "::ffff:169.254.169.254",
                                "::ffff:0:192.168.0.1", "64:ff9b::7f00:1", "2002:7f00:1::1", "::127.0.0.1"])
def test_ipv4_mapped_private_refused(net, tmp_path, ip):
    net.dns["mapped.example.com"] = [ip]
    with pytest.raises(FetchError, match="refused"):
        resolve_image("http://mapped.example.com/a.png", tmp_path)
    assert net.dialled == []


def test_any_private_answer_refuses_the_host(net, tmp_path):
    net.dns["mixed.example.com"] = ["93.184.216.34", "10.0.0.1"]
    with pytest.raises(FetchError, match="refused"):
        resolve_image("http://mixed.example.com/a.png", tmp_path)


def test_unresolvable_host(net, tmp_path):
    with pytest.raises(FetchError, match="cannot resolve"):
        resolve_image("http://nowhere.example.com/a.png", tmp_path)


def test_redirect_to_private_refused(net, tmp_path):
    """Review Focus 3: a public URL that redirects to a private address is refused."""
    net.dns["cdn.example.com"] = ["93.184.216.34"]
    net.dns["internal.example.com"] = ["10.1.2.3"]
    net.routes[("cdn.example.com", "/a.png")] = (302, {"Location": "http://internal.example.com/a.png",
                                                       "Content-Length": "0"}, b"")
    net.routes[("internal.example.com", "/a.png")] = ok(PNG)
    with pytest.raises(FetchError, match="refused"):
        resolve_image("http://cdn.example.com/a.png", tmp_path)
    assert net.dialled == [("93.184.216.34", 80)]  # the private host was never dialled


def test_redirect_to_metadata_ip_literal_refused(net, tmp_path):
    net.dns["cdn.example.com"] = ["93.184.216.34"]
    net.dns["169.254.169.254"] = ["169.254.169.254"]
    net.routes[("cdn.example.com", "/a.png")] = (301, {"Location": "http://169.254.169.254/latest/meta-data/",
                                                       "Content-Length": "0"}, b"")
    with pytest.raises(FetchError, match="refused"):
        resolve_image("http://cdn.example.com/a.png", tmp_path)
    assert net.dialled == [("93.184.216.34", 80)]


@pytest.mark.parametrize("target", ["file:///etc/passwd", "ftp://example.com/a.png", "data:image/png;base64,AA"])
def test_redirect_to_file_scheme_refused(net, tmp_path, target):
    net.dns["cdn.example.com"] = ["93.184.216.34"]
    net.routes[("cdn.example.com", "/a.png")] = (302, {"Location": target, "Content-Length": "0"}, b"")
    with pytest.raises(FetchError, match="redirect"):
        resolve_image("http://cdn.example.com/a.png", tmp_path)


def test_redirect_followed_to_public_host(net, tmp_path):
    net.dns["a.example.com"] = ["93.184.216.34"]
    net.dns["b.example.net"] = ["151.101.1.1"]
    net.routes[("a.example.com", "/x.png")] = (302, {"Location": "http://b.example.net/y.png",
                                                     "Content-Length": "0"}, b"")
    net.routes[("b.example.net", "/y.png")] = ok(PNG)
    assert resolve_image("http://a.example.com/x.png", tmp_path)[0] == PNG
    assert net.dialled == [("93.184.216.34", 80), ("151.101.1.1", 80)]


def test_max_five_redirects(net, tmp_path):
    net.dns["r.example.com"] = ["93.184.216.34"]
    for i in range(7):
        net.routes[("r.example.com", f"/{i}")] = (302, {"Location": f"/{i + 1}", "Content-Length": "0"}, b"")
    net.routes[("r.example.com", "/5")] = ok(PNG)  # reached after exactly 5 redirects
    assert resolve_image("http://r.example.com/0", tmp_path)[0] == PNG
    net.routes[("r.example.com", "/5")] = (302, {"Location": "/6", "Content-Length": "0"}, b"")
    net.routes[("r.example.com", "/6")] = ok(PNG)  # would need a 6th redirect
    with pytest.raises(FetchError, match="redirect"):
        resolve_image("http://r.example.com/0", tmp_path)


def test_size_cap_50mb(net, tmp_path):
    assert fetch.MAX_BYTES == 50 * 1024 * 1024
    net.dns["big.example.com"] = ["93.184.216.34"]
    net.routes[("big.example.com", "/a.png")] = (200, {"Content-Length": str(fetch.MAX_BYTES + 1)}, PNG)
    with pytest.raises(FetchError, match="50 MB"):
        resolve_image("http://big.example.com/a.png", tmp_path)


def test_stream_cap_without_content_length(net, tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "MAX_BYTES", 1000)
    net.dns["big.example.com"] = ["93.184.216.34"]
    net.routes[("big.example.com", "/a.png")] = (200, {}, PNG + b"\x00" * 5000)  # no Content-Length
    net.routes[("big.example.com", "/b.png")] = (200, {}, PNG)
    with pytest.raises(FetchError, match="over the"):
        resolve_image("http://big.example.com/a.png", tmp_path)
    assert resolve_image("http://big.example.com/b.png", tmp_path)[0] == PNG


def test_local_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "MAX_BYTES", 10)
    (tmp_path / "a.png").write_bytes(PNG)
    with pytest.raises(FetchError, match="over the"):
        resolve_image("a.png", tmp_path)


def test_timeout_is_twenty_seconds(net, tmp_path, monkeypatch):
    seen = []
    orig = net.create_connection
    monkeypatch.setattr(fetch.socket, "create_connection",
                        lambda addr, timeout=None, *a, **k: seen.append(timeout) or orig(addr, timeout))
    net.dns["cdn.example.com"] = ["93.184.216.34"]
    net.routes[("cdn.example.com", "/a.png")] = ok(PNG)
    resolve_image("http://cdn.example.com/a.png", tmp_path)
    assert fetch.TIMEOUT == 20 and seen == [20]


def test_no_proxy_is_used(net, tmp_path, monkeypatch):
    """A proxy would make the vetted IP meaningless; downloads ignore proxy settings."""
    monkeypatch.setenv("http_proxy", "http://10.0.0.1:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.1:3128")
    net.dns["cdn.example.com"] = ["93.184.216.34"]
    net.routes[("cdn.example.com", "/a.png")] = ok(PNG)
    assert resolve_image("http://cdn.example.com/a.png", tmp_path)[0] == PNG
    assert net.dialled == [("93.184.216.34", 80)]
