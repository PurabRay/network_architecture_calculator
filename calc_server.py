#!/usr/bin/env python3
"""
calc_server.py - a calculator over HTTP/1.1 that stays on the line.

    python3 calc_server.py          # listens on port 8080
    python3 calc_server.py 9090     # or any port you like

    GET /add?a=2&b=3     -> 200  5
    GET /sub?a=10&b=4    -> 200  6
    GET /mul?a=6&b=7     -> 200  42
    GET /div?a=9&b=3     -> 200  3
    GET /div?a=1&b=0     -> 400  (division by zero)
    GET /add?a=x&b=3     -> 400  (not a number)
    GET /pow?a=2&b=8     -> 404  (no such operation)
    POST /add            -> 405  (only GET)
    GET /add  (no Host)  -> 400  (HTTP/1.1 requires Host)

The point of the exercise: ONE TCP connection carries every request. The
server never hangs up after a response, so it must know exactly where each
request ends:
    * the head ends at the first empty line (\\r\\n\\r\\n)
    * the body is exactly Content-Length bytes - not one more, because
      byte n+1 is the start of the next request.

Also done (the optional stretch goals):
    * Connection: close is honoured
    * a 30 s idle timeout (see IDLE_TIMEOUT below for why)
    * pipelining: send all six requests at once and they are answered in order

Python 3.8+, standard library only, no framework: just a socket.
"""
import socket
import sys
import threading
from urllib.parse import parse_qs, urlsplit

IDLE_TIMEOUT = 30       # seconds. Long enough for a human typing requests by
                        # hand; short enough that a client that vanished
                        # without closing does not hold a thread forever.
MAX_LINE = 8192         # refuse absurdly long lines instead of eating memory

OPERATIONS = {
    "/add": lambda a, b: a + b,
    "/sub": lambda a, b: a - b,
    "/mul": lambda a, b: a * b,
    "/div": lambda a, b: a / b,
}

REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found",
           405: "Method Not Allowed", 501: "Not Implemented"}


class BadRequest(Exception):
    """The bytes are not a valid HTTP request. We answer 400 and close,
    because we can no longer tell where the next request starts."""


# ---- Reading one request -----------------------------------------------------
def read_request(rfile):
    """Read exactly one request from the connection.

    Returns (method, target, headers, body), or None if the client closed
    the connection cleanly between requests.
    """
    # 1. The request line, e.g. "GET /add?a=2&b=3 HTTP/1.1"
    line = rfile.readline(MAX_LINE + 1)
    while line in (b"\r\n", b"\n"):          # RFC 9112: ignore stray blank lines
        line = rfile.readline(MAX_LINE + 1)
    if not line:
        return None                           # clean close: no more requests
    if len(line) > MAX_LINE or not line.endswith(b"\n"):
        raise BadRequest("request line too long or cut off")
    parts = line.decode("latin-1").split()
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        raise BadRequest("request line must be: METHOD TARGET HTTP/x.y")
    method, target, version = parts

    # 2. Header lines, until the empty line that ends the head.
    headers = {}
    while True:
        line = rfile.readline(MAX_LINE + 1)
        if not line:
            raise BadRequest("connection closed in the middle of the headers")
        if len(line) > MAX_LINE:
            raise BadRequest("header line too long")
        if line in (b"\r\n", b"\n"):
            break
        name, colon, value = line.decode("latin-1").partition(":")
        if not colon or not name or name != name.strip():
            raise BadRequest("bad header line")
        headers[name.lower()] = value.strip()

    # 3. The body: exactly Content-Length bytes. THIS is the hard part.
    #    Read too few and the leftover bytes look like the next request.
    #    Read too many and we eat the start of the next request.
    if "transfer-encoding" in headers:
        raise NotImplementedError("chunked bodies are not supported")
    length = headers.get("content-length", "0")
    if not length.isdigit():
        raise BadRequest("Content-Length must be a number")
    body = rfile.read(int(length))
    if len(body) != int(length):
        raise BadRequest("connection closed before the whole body arrived")

    return method, target, version, headers, body


# ---- Deciding the answer -------------------------------------------------------
def to_number(text):
    """'3' -> 3, '2.5' -> 2.5, 'x' -> ValueError."""
    try:
        return int(text)
    except ValueError:
        value = float(text)                   # raises ValueError for 'x'
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("not a finite number")
        return value


def format_number(value):
    """9/3 gives 3.0 in Python; print it as 3."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def compute(method, target, version, headers):
    """Return (status, body text) for one request."""
    # HTTP/1.1 makes Host mandatory (RFC 9112 s.3.2): no Host -> 400.
    if version == "HTTP/1.1" and "host" not in headers:
        return 400, "missing Host header"

    url = urlsplit(target)
    if url.path not in OPERATIONS:
        return 404, f"no operation {url.path}; try /add /sub /mul /div"
    if method != "GET":
        return 405, "only GET is allowed"

    query = parse_qs(url.query)
    if "a" not in query or "b" not in query:
        return 400, "need both a and b, e.g. ?a=2&b=3"
    try:
        a = to_number(query["a"][0])
        b = to_number(query["b"][0])
    except ValueError:
        return 400, "a and b must be numbers"
    if url.path == "/div" and b == 0:
        return 400, "division by zero"
    return 200, format_number(OPERATIONS[url.path](a, b))


def send_response(sock, status, text, close=False):
    body = (text + "\n").encode()
    head = [f"HTTP/1.1 {status} {REASONS[status]}",
            "Content-Type: text/plain; charset=utf-8",
            f"Content-Length: {len(body)}"]
    if status == 405:
        head.append("Allow: GET")
    if close:
        head.append("Connection: close")
    sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + body)


# ---- One connection, many requests ---------------------------------------------
def serve_connection(sock, addr):
    who = f"{addr[0]}:{addr[1]}"
    log = lambda msg: print(f"{who}  {msg}", file=sys.stderr, flush=True)
    sock.settimeout(IDLE_TIMEOUT)
    rfile = sock.makefile("rb")     # buffered reader: keeps any extra bytes
                                    # (the next pipelined request) for later
    count = 0
    try:
        while True:
            try:
                request = read_request(rfile)
            except BadRequest as e:
                send_response(sock, 400, str(e), close=True)
                log(f"400 and close: {e}")
                break
            except NotImplementedError as e:
                send_response(sock, 501, str(e), close=True)
                log(f"501 and close: {e}")
                break
            if request is None:
                break                               # client hung up
            method, target, version, headers, _body = request
            count += 1
            status, text = compute(method, target, version, headers)
            wants_close = (headers.get("connection", "").lower() == "close"
                           or version == "HTTP/1.0")
            send_response(sock, status, text, close=wants_close)
            log(f"#{count} {method} {target} -> {status} {text}")
            if wants_close:
                break
    except socket.timeout:
        log(f"idle for {IDLE_TIMEOUT}s, closing")
    except OSError as e:
        log(f"connection error: {e}")
    finally:
        rfile.close()
        sock.close()
        log(f"closed after {count} request(s) on one connection")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    try:
        # Listen on IPv6 AND IPv4 at once. On Windows "localhost" tries IPv6
        # first; an IPv4-only server makes every connection wait ~2 s.
        server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        server.bind(("::", port))
    except OSError:
        # This machine has no IPv6: plain IPv4 is fine.
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", port))
    server.listen()
    print(f"calc_server: listening on port {port}", file=sys.stderr, flush=True)
    while True:
        sock, addr = server.accept()
        threading.Thread(target=serve_connection, args=(sock, addr[:2]),
                         daemon=True).start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
