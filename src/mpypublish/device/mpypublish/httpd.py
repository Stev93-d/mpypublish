"""
A small HTTP server for the updater, for the apps that have none

It only answers the requests of the updater. It is served from the main
loop: poll() takes one small step at a time and never waits. One client at
a time, the others stay in the queue of the socket:

  1. accept a connection, if there is one
  2. read the request as it arrives
  3. pass it to the updater
  4. send the answer, one block per call

    from mpypublish.updater import Updater
    from mpypublish.httpd import Server

    server = Server(Updater())
    while True:
        server.poll()             # the updater's poll() too
        ...

An asyncio app can start `server.serve()` as a task instead.
"""

import json
import select
import socket

from .updater import Countdown

EAGAIN = 11     # On a non blocking socket: nothing to read for now, or buffer full

_REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 409: "Conflict"}


class Server:
    REQUEST_TIMEOUT_MS = 3000   # Longest time to receive a whole request
    SEND_TIMEOUT_MS = 5000      # Longest time the client can go without reading
    MAX_REQUEST_BYTES = 16384
    BLOCK_BYTES = 1024          # Size of the blocks sent at every poll()

    def __init__(self, updater, port=80):
        self.updater = updater
        self.port = port
        self._client = None
        self._request = b""
        self._body_start = -1       # Position of the body in the request, -1 until the headers are there
        self._content_length = 0
        self._out = None            # What is left to send, None while reading
        self._deadline = Countdown()

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("", port))
        self._sock.listen(2)
        self._sock.setblocking(False)

        # To know whether a connection is waiting without an exception at every turn
        self._poller = select.poll()
        self._poller.register(self._sock, select.POLLIN)

    def poll(self):
        """One step for the client in progress, or a new one accepted, and one for the updater."""
        self.updater.poll()
        try:
            if self._client is None:
                self._accept()
            elif self._out is None:
                self._receive()
            else:
                self._send()
        except Exception as e:
            print("mpypublish: client dropped:", e)
            self._close_client()

    async def serve(self, interval_ms=20):
        """For asyncio apps: asyncio.create_task(server.serve())"""
        import asyncio
        while True:
            self.poll()
            await asyncio.sleep_ms(interval_ms)

    def close(self):
        self._close_client()
        self._sock.close()

    def _accept(self):
        for _ in self._poller.ipoll(0):
            self._client, _address = self._sock.accept()
            self._client.setblocking(False)
            self._request = b""
            self._body_start = -1
            self._content_length = 0
            self._deadline.start(self.REQUEST_TIMEOUT_MS)
            break

    def _receive(self):
        try:
            chunk = self._client.recv(1024)
        except OSError as e:
            if e.args[0] != EAGAIN:
                raise
            chunk = None

        if chunk:
            self._request += chunk
            if len(self._request) > self.MAX_REQUEST_BYTES:
                raise ValueError("request too large")
            if self._complete():
                self._answer()
                return
        elif chunk is not None:
            # The client closed before the end
            self._close_client()
            return

        if self._deadline.expired():
            # Silent or too slow: the place goes to the next one
            self._close_client()

    def _complete(self):
        """True when the headers and all the body they announce have arrived"""
        if self._body_start < 0:
            end = self._request.find(b"\r\n\r\n")
            if end < 0:
                return False
            self._body_start = end + 4
            for line in self._request[:end].decode().lower().split("\r\n"):
                if line.startswith("content-length:"):
                    self._content_length = int(line.split(":")[1].strip())
                    break
        return len(self._request) - self._body_start >= self._content_length

    def _answer(self):
        # Only the headers are text for sure: the body can be a piece of a .mpy
        head = self._request[:self._body_start].decode().split("\r\n")
        body = self._request[self._body_start:self._body_start + self._content_length]
        self._request = b""

        words = head[0].split(" ")
        method, target = (words[0], words[1]) if len(words) >= 2 else ("", "")
        path, query = target, {}
        if "?" in target:
            path, query_string = target.split("?", 1)
            for pair in query_string.split("&"):
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    query[key] = value

        reply = self.updater.handle(method, path, query, body)
        status, data = reply if reply is not None else (404, {"error": "not found"})

        payload = json.dumps(data).encode()
        self._out = (
            "HTTP/1.1 %d %s\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n"
            "\r\n" % (status, _REASONS.get(status, "Error"), len(payload))
        ).encode() + payload
        self._deadline.start(self.SEND_TIMEOUT_MS)

    def _send(self):
        if not self._out:
            self._close_client()
            return
        try:
            sent = self._client.send(self._out[:self.BLOCK_BYTES])
        except OSError as e:
            if e.args[0] != EAGAIN:
                raise
            sent = 0

        if sent:
            # send() may have taken only a part of the block
            self._out = self._out[sent:]
            self._deadline.start(self.SEND_TIMEOUT_MS)
        elif self._deadline.expired():
            self._close_client()

    def _close_client(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self._request = b""
        self._out = None
        self._deadline.stop()
