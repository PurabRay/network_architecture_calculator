# Network Architecture Assignment: a calculator that stays on the line

An HTTP/1.1 server written with a plain socket and no framework. It does arithmetic, and it keeps **one TCP connection open for every request**.

```bash
python3 calc_server.py                     # port 8080  (Windows: python calc_server.py)
curl "http://localhost:8080/add?a=2&b=3"   # -> 5       (Windows PowerShell: curl.exe ...)
```

You can also open `http://localhost:8080/add?a=2&b=3` in a browser.

| Request | Response |
|---|---|
| `GET /add?a=2&b=3` | `200` `5` |
| `GET /sub?a=10&b=4` | `200` `6` |
| `GET /mul?a=6&b=7` | `200` `42` |
| `GET /div?a=9&b=3` | `200` `3` |
| `GET /div?a=1&b=0` | `400` division by zero |
| `GET /add?a=x&b=3` | `400` not a number |
| `GET /pow?a=2&b=8` | `404` no such operation |
| `POST /add` | `405`, with `Allow: GET` |
| `GET /add` with no `Host` header | `400` (HTTP/1.1 requires `Host`) |

**The hard part: where one request ends and the next begins.** The server never hangs up, so EOF can't mark the end of a request. The head ends at the first empty line, and the body is exactly `Content-Length` bytes. The server reads that many and stops, because byte n+1 belongs to the next request. `test_body_is_consumed_exactly` checks this by sending a POST whose body *looks like* a GET request, followed by a real GET. The server has to answer the POST and the real GET, and never the fake one.

**Stretch goals, all done:**

- **`Connection: close` is honoured.** The server says so in its response, then closes.
- **Idle timeout of 30 s.** That's long enough for a person typing requests by hand. A client that vanished without closing doesn't hold a thread forever.
- **Pipelining.** Send all six requests in one write, and the answers come back in order. This works because the server reads through a buffer that keeps any extra bytes for the next request.
- **Chunked encoding** is refused cleanly with `501` (then close), not misread.

**Tests:** `python3 -m unittest -v test_calc.py` (Windows: `python -m unittest -v test_calc.py`) runs 15 tests. The first copies the brief's "How I will mark it" step for step: one socket, six requests, and the socket still open at the end.

## Files

```
calc_server.py   the server (one file, standard library only)
test_calc.py     15 tests
```
