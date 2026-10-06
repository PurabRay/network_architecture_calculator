"""
Tests for calc_server.py.   Run from the repo root:

    python3 -m unittest -v test_calc.py

The first test copies "How I will mark it" from the brief: one socket,
six requests, and the socket must still be open at the end.
"""
import os
import socket
import subprocess
import sys
import time
import unittest
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8123


def request(method, target, host=True, extra="", body=b""):
    lines = [f"{method} {target} HTTP/1.1"]
    if host:
        lines.append("Host: localhost")
    if body:
        lines.append(f"Content-Length: {len(body)}")
    if extra:
        lines.append(extra)
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


class Reader:
    """Reads responses off one socket, using Content-Length to find the end
    of each one - the same problem the server has, from the other side."""

    def __init__(self, sock):
        self.sock, self.buf = sock, b""

    def _fill(self):
        chunk = self.sock.recv(4096)
        if not chunk:
            raise ConnectionError("server closed the connection")
        self.buf += chunk

    def response(self):
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        status = int(lines[0].split()[1])
        headers = {l.split(":", 1)[0].lower(): l.split(":", 1)[1].strip()
                   for l in lines[1:]}
        n = int(headers["content-length"])
        while len(self.buf) < n:
            self._fill()
        body, self.buf = self.buf[:n], self.buf[n:]
        return status, body.decode().strip(), headers


def still_open(sock):
    """True if the server has not closed: a 0.2 s read must time out."""
    sock.settimeout(0.2)
    try:
        return sock.recv(1) != b""
    except socket.timeout:
        return True
    finally:
        sock.settimeout(5)


class CalculatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "calc_server.py"), str(PORT)],
            stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", PORT)).close()
                return
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait()

    def setUp(self):
        warnings.simplefilter("ignore", ResourceWarning)
        self.sock = socket.create_connection(("localhost", PORT), timeout=5)
        self.reader = Reader(self.sock)

    def tearDown(self):
        self.sock.close()

    def ask(self, raw):
        self.sock.sendall(raw)
        return self.reader.response()

    # ---- exactly how the brief says it will be marked ------------------------
    def test_marking_script_one_socket_six_requests(self):
        expected = [("/add?a=2&b=3", "GET", 200, "5"),
                    ("/sub?a=10&b=4", "GET", 200, "6"),
                    ("/mul?a=6&b=7", "GET", 200, "42"),
                    ("/div?a=1&b=0", "GET", 400, None),
                    ("/pow?a=2&b=8", "GET", 404, None),
                    ("/add", "POST", 405, None)]
        for target, method, status, answer in expected:
            got_status, got_body, _ = self.ask(request(method, target))
            self.assertEqual(got_status, status, target)
            if answer is not None:
                self.assertEqual(got_body, answer, target)
        self.assertTrue(still_open(self.sock), "socket still open: True")

    # ---- every line of "The task" ------------------------------------------------
    def test_div(self):
        self.assertEqual(self.ask(request("GET", "/div?a=9&b=3"))[:2], (200, "3"))

    def test_not_a_number(self):
        self.assertEqual(self.ask(request("GET", "/add?a=x&b=3"))[0], 400)

    def test_missing_host(self):
        self.assertEqual(self.ask(request("GET", "/add?a=2&b=3", host=False))[0], 400)

    def test_missing_argument(self):
        self.assertEqual(self.ask(request("GET", "/add?a=2"))[0], 400)

    def test_decimals_and_negatives(self):
        self.assertEqual(self.ask(request("GET", "/div?a=7&b=2"))[:2], (200, "3.5"))
        self.assertEqual(self.ask(request("GET", "/sub?a=-1&b=4"))[:2], (200, "-5"))

    def test_405_says_allow_get(self):
        self.assertEqual(self.ask(request("POST", "/add"))[2].get("allow"), "GET")

    def test_errors_do_not_close_the_connection(self):
        for target in ("/div?a=1&b=0", "/add?a=x&b=3", "/pow?a=2&b=8"):
            self.ask(request("GET", target))
        self.assertEqual(self.ask(request("GET", "/add?a=1&b=1"))[:2], (200, "2"))

    # ---- "the part that is actually hard": exactly Content-Length bytes ------
    def test_body_is_consumed_exactly(self):
        # A POST body that LOOKS like a request. If the server read too few
        # bytes, it would answer this fake GET; if too many, it would eat
        # the start of the real next request.
        tricky = b"GET /mul?a=9&b=9 HTTP/1.1\r\nHost: x\r\n\r\n"
        self.sock.sendall(request("POST", "/add", body=tricky) +
                          request("GET", "/add?a=20&b=22"))
        self.assertEqual(self.reader.response()[0], 405)
        self.assertEqual(self.reader.response()[:2], (200, "42"))
        self.assertTrue(still_open(self.sock))

    def test_request_split_across_many_packets(self):
        raw = request("GET", "/mul?a=6&b=7")
        for i in range(len(raw)):
            self.sock.send(raw[i:i + 1])
            time.sleep(0.002)
        self.assertEqual(self.reader.response()[:2], (200, "42"))

    def test_bad_content_length_is_400_then_close(self):
        status, _, headers = self.ask(b"POST /add HTTP/1.1\r\nHost: x\r\n"
                                      b"Content-Length: abc\r\n\r\n")
        self.assertEqual((status, headers.get("connection")), (400, "close"))

    def test_garbage_request_line_is_400(self):
        self.assertEqual(self.ask(b"HELLO\r\n\r\n")[0], 400)

    # ---- stretch goals --------------------------------------------------------
    def test_pipelining_all_six_at_once(self):
        targets = ["/add?a=2&b=3", "/sub?a=10&b=4", "/mul?a=6&b=7",
                   "/div?a=9&b=3", "/div?a=1&b=0", "/pow?a=2&b=8"]
        self.sock.sendall(b"".join(request("GET", t) for t in targets))
        got = [self.reader.response()[:2] for _ in targets]
        self.assertEqual([s for s, _ in got], [200, 200, 200, 200, 400, 404])
        self.assertEqual([b for _, b in got[:4]], ["5", "6", "42", "3"])

    def test_connection_close_is_honoured(self):
        status, _, headers = self.ask(request("GET", "/add?a=1&b=2",
                                              extra="Connection: close"))
        self.assertEqual((status, headers.get("connection")), (200, "close"))
        self.assertFalse(still_open(self.sock))

    def test_chunked_is_refused_cleanly(self):
        status, _, _ = self.ask(request("POST", "/add",
                                        extra="Transfer-Encoding: chunked"))
        self.assertEqual(status, 501)


if __name__ == "__main__":
    unittest.main()
