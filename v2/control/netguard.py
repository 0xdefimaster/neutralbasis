"""Offline proof: a context that makes ANY Python socket connect or name lookup fail.

Used by `nb_doctor --offline` and by the read API tests: if a check or a handler tried
to reach a venue (or anything) the attempt raises OfflineViolation and is recorded.
Loopback may be allowed for the local server tests (allow_loopback=True)."""
import contextlib
import socket

LOOPBACK = ("127.0.0.1", "::1", "localhost")


class OfflineViolation(RuntimeError):
    pass


@contextlib.contextmanager
def offline(allow_loopback=False):
    attempts = []
    orig_connect = socket.socket.connect
    orig_cc = socket.create_connection
    orig_gai = socket.getaddrinfo

    def host_of(addr):
        return addr[0] if isinstance(addr, tuple) else str(addr)

    def guard(host):
        if allow_loopback and str(host) in LOOPBACK:
            return
        attempts.append(str(host))
        raise OfflineViolation("network access attempted in offline mode: %s" % host)

    def connect(self, addr):
        guard(host_of(addr))
        return orig_connect(self, addr)

    def cc(addr, *a, **k):
        guard(host_of(addr))
        return orig_cc(addr, *a, **k)

    def gai(host, *a, **k):
        guard(host)
        return orig_gai(host, *a, **k)

    socket.socket.connect, socket.create_connection, socket.getaddrinfo = connect, cc, gai
    try:
        yield attempts
    finally:
        socket.socket.connect, socket.create_connection, socket.getaddrinfo = (
            orig_connect, orig_cc, orig_gai)
