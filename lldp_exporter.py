#!/usr/bin/env python3
"""Prometheus exporter for LLDP neighbours, via lldpcli.

No maintained standalone LLDP exporter exists (checked 2026-10-01): the usual
answer is SNMP.

It shells out to `lldpcli -f json0`, not `-f json`. That matters: with `json`,
lldpd emits a bare object for a single element and a list for several, so a
parser written against one neighbour breaks the moment a second appears.
`json0` always emits arrays. Confirmed against lldpd 1.0.22: `json` gives
{"lldp": {}} where `json0` gives {"lldp": [{}]}.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# "2 days, 01:02:03" / "0 day, 00:00:11" — lldpd spells the unit both ways.
_AGE = re.compile(r"(?:(\d+)\s+days?,\s*)?(\d+):(\d{2}):(\d{2})")


def first(value, default=""):
    """Unwrap json0's arrays-everywhere encoding.

    Every scalar arrives as a one-element list, and some keys are absent
    entirely, so callers would otherwise need the same guard at each level.
    """
    if isinstance(value, list):
        value = value[0] if value else default
    return default if value is None else value


def age_seconds(text):
    """lldpd reports neighbour age as prose; Prometheus wants seconds."""
    match = _AGE.search(text or "")
    if not match:
        return None
    days, hours, minutes, seconds = match.groups()
    return int(days or 0) * 86400 + int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def escape(value):
    """Escape a Prometheus label value (backslash, quote, newline)."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def run_lldpcli(binary, timeout, socket_path=""):
    # -u must be passed even though lldpd was started with it: lldpcli does not
    # learn the daemon's socket path, it just defaults to /run/lldpd.socket and
    # reports "No such file or directory" when the pod put it elsewhere.
    argv = [binary]
    if socket_path:
        argv += ["-u", socket_path]
    argv += ["-f", "json0", "show", "neighbors", "details"]
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{binary} exited {completed.returncode}: {completed.stderr.strip()[:200]}"
        )
    return json.loads(completed.stdout or "{}")


def parse_neighbours(payload):
    """Flatten lldpcli json0 into one record per (local port, neighbour)."""
    neighbours = []
    for block in payload.get("lldp") or []:
        if not isinstance(block, dict):
            continue
        for interface in block.get("interface") or []:
            if not isinstance(interface, dict):
                continue
            chassis = first(interface.get("chassis"), {}) or {}
            port = first(interface.get("port"), {}) or {}
            # chassis.name is sometimes a bare string and sometimes a dict
            # carrying {"value": ...}; both forms appear across lldpd versions.
            chassis_name = first(chassis.get("name"), "")
            if isinstance(chassis_name, dict):
                chassis_name = chassis_name.get("value", "")
            chassis_id = first(chassis.get("id"), {}) or {}
            port_id = first(port.get("id"), {}) or {}
            port_descr = first(port.get("descr"), "")
            if isinstance(port_descr, dict):
                port_descr = port_descr.get("value", "")
            neighbours.append(
                {
                    "local_port": first(interface.get("name"), ""),
                    "via": first(interface.get("via"), ""),
                    "age": age_seconds(first(interface.get("age"), "")),
                    "remote_chassis": first(chassis_id.get("value"), ""),
                    "remote_name": chassis_name,
                    "remote_port": first(port_id.get("value"), ""),
                    "remote_port_descr": port_descr,
                }
            )
    return neighbours


def render(neighbours, up, duration, error=""):
    """Render the Prometheus exposition text."""
    out = [
        "# HELP lldp_up Whether lldpcli answered on the last scrape.",
        "# TYPE lldp_up gauge",
        f"lldp_up {1 if up else 0}",
        "# HELP lldp_scrape_duration_seconds Time taken to collect from lldpd.",
        "# TYPE lldp_scrape_duration_seconds gauge",
        f"lldp_scrape_duration_seconds {duration:.6f}",
    ]
    if not up:
        # Emit the reason as a label so a broken socket is diagnosable from
        # the TSDB alone, rather than only from the container's log.
        out += [
            "# HELP lldp_scrape_error Last collection error, 1 per reason.",
            "# TYPE lldp_scrape_error gauge",
            f'lldp_scrape_error{{reason="{escape(error)}"}} 1',
        ]
        return "\n".join(out) + "\n"

    out += [
        (
            "# HELP lldp_neighbor_info A discovered LLDP neighbour. Always 1;"
            " the labels carry the topology edge."
        ),
        "# TYPE lldp_neighbor_info gauge",
    ]
    for n in neighbours:
        labels = (
            f'local_port="{escape(n["local_port"])}",'
            f'remote_chassis="{escape(n["remote_chassis"])}",'
            f'remote_name="{escape(n["remote_name"])}",'
            f'remote_port="{escape(n["remote_port"])}",'
            f'remote_port_descr="{escape(n["remote_port_descr"])}",'
            f'via="{escape(n["via"])}"'
        )
        out.append(f"lldp_neighbor_info{{{labels}}} 1")

    per_port = {}
    for n in neighbours:
        per_port[n["local_port"]] = per_port.get(n["local_port"], 0) + 1
    out += [
        "# HELP lldp_neighbors Number of neighbours seen on a local port.",
        "# TYPE lldp_neighbors gauge",
    ]
    for local_port, count in sorted(per_port.items()):
        out.append(f'lldp_neighbors{{local_port="{escape(local_port)}"}} {count}')

    aged = [n for n in neighbours if n["age"] is not None]
    if aged:
        out += [
            (
                "# HELP lldp_neighbor_age_seconds How long this neighbour has been"
                " known. Resets on every re-learn, so a flapping link keeps it near"
                " zero."
            ),
            "# TYPE lldp_neighbor_age_seconds gauge",
        ]
        for n in aged:
            labels = (
                f'local_port="{escape(n["local_port"])}",'
                f'remote_name="{escape(n["remote_name"])}"'
            )
            out.append(f"lldp_neighbor_age_seconds{{{labels}}} {n['age']}")

    return "\n".join(out) + "\n"


def collect(binary, timeout, socket_path=""):
    started = time.monotonic()
    try:
        payload = run_lldpcli(binary, timeout, socket_path)
    except Exception as exc:  # noqa: BLE001 - surfaced as lldp_scrape_error
        return render([], False, time.monotonic() - started, str(exc))
    neighbours = parse_neighbours(payload)
    return render(neighbours, True, time.monotonic() - started)


class Handler(BaseHTTPRequestHandler):
    binary = "lldpcli"
    timeout = 10
    socket_path = ""

    def do_GET(self):
        if self.path.split("?")[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        body = collect(self.binary, self.timeout, self.socket_path).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Silence per-request logging; a scrape every 30s is not news."""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1:9333")
    parser.add_argument("--lldpcli", default="lldpcli")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--socket",
        default="",
        help="lldpd control socket; passed to lldpcli as -u. Needed whenever "
        "lldpd was started with a non-default -u, which a pod-scoped volume "
        "requires.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print one scrape and exit, for debugging.",
    )
    args = parser.parse_args(argv)

    if args.once:
        sys.stdout.write(collect(args.lldpcli, args.timeout, args.socket))
        return 0

    host, _, port = args.listen.rpartition(":")
    Handler.binary = args.lldpcli
    Handler.timeout = args.timeout
    Handler.socket_path = args.socket
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
