import errno
import socket
from typing import Any

import numpy as np

import flaschen

UDP_IP = "localhost"
UDP_PORT = 1337

WIDTH = 45
HEIGHT = 35


def test_flaschen_init(benchmark: Any) -> None:
    benchmark(lambda: flaschen.Flaschen(UDP_IP, UDP_PORT, WIDTH, HEIGHT))


def test_send(benchmark: Any) -> None:
    ft = flaschen.Flaschen(UDP_IP, UDP_PORT, WIDTH, HEIGHT)
    benchmark(lambda: ft.send())


def test_set(benchmark: Any) -> None:
    ft = flaschen.Flaschen(UDP_IP, UDP_PORT, WIDTH, HEIGHT)
    benchmark(lambda: ft.set(0, 0, (1, 1, 1)))


def test_set_full(benchmark: Any) -> None:
    ft = flaschen.Flaschen(UDP_IP, UDP_PORT, WIDTH, HEIGHT)

    def test() -> None:
        for y in range(HEIGHT):
            for x in range(WIDTH):
                ft.set(x, y, (1, 1, 1))

    benchmark(test)


def test_send_array(benchmark: Any) -> None:
    ft = flaschen.Flaschen(UDP_IP, UDP_PORT)
    ones_array = np.ones((HEIGHT, WIDTH, 3), dtype=np.uint8)
    benchmark(lambda: ft.send_array(ones_array, (0, 0, 0)))


def test_send_array_transparent(benchmark: Any) -> None:
    ft = flaschen.Flaschen(UDP_IP, UDP_PORT, transparent=True)
    ones_array = np.ones((HEIGHT, WIDTH, 3), dtype=np.uint8)
    benchmark(lambda: ft.send_array(ones_array, (0, 0, 0)))


class _FailingSocket:
    """Stands in for the real socket so send errors are deterministic."""

    def __init__(self, err: int, times: int = 10 ** 9) -> None:
        self.err = err
        self.times = times
        self.sends = 0

    def send(self, data: bytes) -> int:
        self.sends += 1
        if self.times > 0:
            self.times -= 1
            raise OSError(self.err, "injected")
        return len(data)

    def setsockopt(self, *a: Any) -> None:
        pass


def _client() -> "flaschen.Flaschen":
    return flaschen.Flaschen(UDP_IP, UDP_PORT, 2, 2)


def test_unreachable_server_drops_instead_of_raising() -> None:
    """Restarting the server must not kill a running demo."""
    ft = _client()
    ft._sock = _FailingSocket(errno.ECONNREFUSED)
    ft.send()
    ft.send()
    assert ft.dropped == 2


def test_sends_resume_when_the_server_returns() -> None:
    ft = _client()
    sock = _FailingSocket(errno.ECONNREFUSED, times=2)
    ft._sock = sock
    for _ in range(2 + flaschen._RECOVERY_STREAK):
        ft.send()
    assert ft.dropped == 2
    assert ft._unreachable is False


def test_recovery_is_not_declared_on_a_single_success() -> None:
    """A dead server fails every other send, so one success proves nothing."""
    ft = _client()
    ft._sock = _FailingSocket(errno.ECONNREFUSED, times=1)
    ft.send()                                     # fails, marks unreachable
    ft.send()                                     # the gap between ICMPs
    assert ft._unreachable is True


def test_outage_reports_once_and_does_not_flap(capture_stderr: Any = None) -> None:
    """Alternating failure must not print a notice per frame."""
    import io
    import contextlib

    ft = _client()

    class Alternating:
        def __init__(self) -> None:
            self.n = 0

        def send(self, data: bytes) -> int:
            self.n += 1
            if self.n % 2:                        # every other send fails
                raise OSError(errno.ECONNREFUSED, "Connection refused")
            return len(data)

        def setsockopt(self, *a: Any) -> None:
            pass

    ft._sock = Alternating()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        for _ in range(200):
            ft.send()
    lines = [l for l in err.getvalue().splitlines() if l]
    assert len(lines) == 1, "expected one notice, got:\n" + "\n".join(lines)
    assert "unreachable" in lines[0]


def test_enobufs_is_retried_once() -> None:
    ft = _client()
    sock = _FailingSocket(errno.ENOBUFS, times=1)
    ft._sock = sock
    ft.send()
    assert sock.sends == 2, "should have retried"
    assert ft.dropped == 0


def test_unexpected_errors_still_propagate() -> None:
    """Dropping frames is for an absent server, not for real bugs."""
    ft = _client()
    ft._sock = _FailingSocket(errno.EMSGSIZE)
    try:
        ft.send()
    except OSError:
        return
    raise AssertionError("EMSGSIZE should not have been swallowed")


def test_survives_a_real_server_restart() -> None:
    """End to end over loopback: bind, send, close, send, rebind, send."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    ft = flaschen.Flaschen("127.0.0.1", port, 2, 2)
    ft.send()
    assert srv.recv(65535)

    srv.close()                       # the server goes away
    for _ in range(6):
        ft.send()                     # must not raise
    assert ft.dropped > 0, "expected ICMP port unreachable to surface"

    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))     # and comes back
    srv.settimeout(2)
    for _ in range(3):
        ft.send()
    assert srv.recv(65535), "sender should recover on its own"
    srv.close()


def test_send_array_banded_single(benchmark: Any) -> None:
    ft = flaschen.Flaschen(UDP_IP, UDP_PORT, transparent=True)
    ones_array = np.ones((HEIGHT, WIDTH, 3), dtype=np.uint8)
    benchmark(lambda: ft.send_array_banded(ones_array, (0, 0, 0)))


def test_send_array_banded_split(benchmark: Any) -> None:
    ft = flaschen.Flaschen(UDP_IP, UDP_PORT, transparent=True)
    ones_array = np.ones((HEIGHT, WIDTH, 3), dtype=np.uint8)
    benchmark(lambda: ft.send_array_banded(ones_array, (0, 0, 0), band_rows=8))


def test_send_new_array(benchmark: Any) -> None:
    ft = flaschen.Flaschen(UDP_IP, UDP_PORT)
    benchmark(
        lambda: ft.send_array(np.ones((HEIGHT, WIDTH, 3), dtype=np.uint8), (0, 0, 0))
    )
