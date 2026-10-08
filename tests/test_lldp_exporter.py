"""Tests for the LLDP exporter.

The fixture beside this file is verbatim `lldpcli -f json0 show interfaces
details` from the pinned lldpd image, so the nesting under test is the real
thing rather than a guess: chassis/name arrives as [{"value": ...}] while
interface/name is a bare string, and getting that backwards silently yields
empty labels.
"""

import concurrent.futures
import json
import pathlib
import threading
import time
import urllib.request

import pytest

import lldp_exporter as lldp

_FIXTURE = pathlib.Path(__file__).with_name("lldpcli_json0_sample.json")


def _neighbour_payload():
    """The captured shape plus the keys only `show neighbors` carries."""
    payload = json.loads(_FIXTURE.read_text())
    interface = payload["lldp"][0]["interface"][0]
    interface["via"] = "LLDP"
    interface["age"] = "0 day, 00:02:03"
    return payload


def test_parses_real_lldpcli_nesting():
    [neighbour] = lldp.parse_neighbours(_neighbour_payload())
    assert neighbour["local_port"] == "eth0"
    # chassis/name and port/descr are [{"value": ...}], not bare strings.
    assert neighbour["remote_name"]
    assert not isinstance(neighbour["remote_name"], dict)
    assert neighbour["remote_port_descr"] == "eth0"
    assert neighbour["remote_chassis"].count(":") == 5
    assert neighbour["age"] == 123


def test_empty_output_is_not_an_error():
    # lldpd with no neighbours emits {"lldp": [{}]}, confirmed against 1.0.22.
    assert lldp.parse_neighbours({"lldp": [{}]}) == []
    assert lldp.parse_neighbours({}) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0 day, 00:00:11", 11),
        ("0 days, 00:02:03", 123),
        ("1 day, 01:00:00", 90000),
        ("2 days, 00:00:01", 172801),
        ("", None),
        ("not a duration", None),
    ],
)
def test_age_parsing(text, expected):
    assert lldp.age_seconds(text) == expected


def test_first_unwraps_json0_arrays():
    assert lldp.first(["a", "b"]) == "a"
    assert lldp.first([]) == ""
    assert lldp.first(None) == ""
    assert lldp.first("bare") == "bare"


def test_label_values_are_escaped():
    # A chassis description containing a quote would otherwise produce
    # unparseable exposition text.
    neighbours = [
        {
            "local_port": 'eth0"',
            "via": "LLDP",
            "age": None,
            "remote_chassis": "aa:bb",
            "remote_name": "back\\slash",
            "remote_port": "p1",
            "remote_port_descr": "line\nbreak",
        }
    ]
    body = lldp.render(neighbours, True, 0.1)
    assert 'local_port="eth0\\""' in body
    assert 'remote_name="back\\\\slash"' in body
    assert "line\\nbreak" in body
    # One physical line per sample, or the scrape is malformed.
    assert all(line.strip() for line in body.splitlines())


def test_neighbour_counts_group_by_local_port():
    def neighbour(port, name):
        return {
            "local_port": port,
            "via": "LLDP",
            "age": None,
            "remote_chassis": name,
            "remote_name": name,
            "remote_port": "p",
            "remote_port_descr": "p",
        }

    body = lldp.render(
        [neighbour("eth0", "a"), neighbour("eth0", "b"), neighbour("eth1", "c")],
        True,
        0.1,
    )
    assert 'lldp_neighbors{local_port="eth0"} 2' in body
    assert 'lldp_neighbors{local_port="eth1"} 1' in body
    assert body.count("lldp_neighbor_info{") == 3


def test_failure_reports_up_zero_and_a_reason():
    body = lldp.render([], False, 0.2, error='socket missing: "/run/x"')
    assert "lldp_up 0" in body
    assert "lldp_scrape_error{" in body
    assert 'reason="socket missing: \\"/run/x\\""' in body
    # No topology series when the collection failed, so stale edges cannot
    # be mistaken for current ones.
    assert "lldp_neighbor_info{" not in body


def test_successful_render_sets_up_and_duration():
    body = lldp.render([], True, 1.5)
    assert "lldp_up 1" in body
    assert "lldp_scrape_duration_seconds 1.500000" in body


def test_socket_path_is_passed_to_lldpcli(monkeypatch):
    """lldpcli does not inherit lldpd's -u; it defaults to /run/lldpd.socket.

    Confirmed live against lldpd 1.0.22: with the daemon started on a
    pod-scoped volume, lldpcli without -u reports "unable to connect to socket
    /run/lldpd.socket", so the exporter reads nothing while the daemon is fine.
    """
    seen = {}

    class Result:
        returncode = 0
        stdout = '{"lldp": [{}]}'
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return Result()

    monkeypatch.setattr(lldp.subprocess, "run", fake_run)

    lldp.run_lldpcli("lldpcli", 5, "/run/lldp/lldpd.socket")
    assert seen["argv"][:3] == ["lldpcli", "-u", "/run/lldp/lldpd.socket"]
    assert "json0" in seen["argv"]

    # Unset means "use lldpcli's own default", so no empty -u is emitted.
    lldp.run_lldpcli("lldpcli", 5, "")
    assert "-u" not in seen["argv"]


def test_nonzero_exit_is_reported_not_swallowed(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "unable to connect to socket /run/lldpd.socket"

    monkeypatch.setattr(lldp.subprocess, "run", lambda *a, **k: Result())
    body = lldp.collect("lldpcli", 5)
    assert "lldp_up 0" in body
    assert "unable to connect" in body


def test_unnamed_neighbours_on_one_port_get_distinct_age_series():
    def unnamed(chassis):
        return {
            "local_port": "eth0",
            "via": "LLDP",
            "age": 60,
            "remote_chassis": chassis,
            "remote_name": "",
            "remote_port": "p1",
            "remote_port_descr": "",
        }

    body = lldp.render([unnamed("aa:aa"), unnamed("bb:bb")], True, 0.1)
    series = [line.rsplit(" ", 1)[0] for line in body.splitlines() if line[0] != "#"]
    assert len(series) == len(set(series))
    assert body.count("lldp_neighbor_age_seconds{") == 2


class _ServerThatReturnsImmediately:
    def __init__(self, address, handler):
        self.address = address

    def serve_forever(self):
        pass


def test_lldpcli_timeout_does_not_become_the_socket_timeout(monkeypatch):
    monkeypatch.setattr(lldp, "ThreadingHTTPServer", _ServerThatReturnsImmediately)
    socket_timeout = lldp.Handler.timeout
    monkeypatch.setattr(lldp.Handler, "lldpcli_timeout", lldp.Handler.lldpcli_timeout)

    lldp.main(["--timeout", "3.5"])
    assert lldp.Handler.lldpcli_timeout == 3.5
    assert lldp.Handler.timeout == socket_timeout


@pytest.mark.parametrize("value", ["0", "-1", "nan"])
def test_non_positive_timeout_is_rejected(value):
    with pytest.raises(SystemExit):
        lldp.main(["--once", "--timeout", value])


def test_concurrent_scrapes_run_one_lldpcli_at_a_time(monkeypatch):
    state = {"running": 0, "peak": 0}
    guard = threading.Lock()

    def slow_collect(*args):
        with guard:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        time.sleep(0.05)
        with guard:
            state["running"] -= 1
        return "lldp_up 1\n"

    monkeypatch.setattr(lldp, "collect", slow_collect)
    server = lldp.ThreadingHTTPServer(("127.0.0.1", 0), lldp.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/metrics"
    try:
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            bodies = list(
                pool.map(lambda _: urllib.request.urlopen(url).read(), range(4))
            )
    finally:
        server.shutdown()
        server.server_close()
    assert bodies == [b"lldp_up 1\n"] * 4
    assert state["peak"] == 1
