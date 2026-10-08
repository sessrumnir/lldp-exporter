# lldp-exporter

Prometheus exporter for LLDP neighbours. It reads lldpd through `lldpcli -f json0` and serves one series per neighbour on each local port, with the neighbour's system name, port and description as labels.

```sh
podman run --rm ghcr.io/sessrumnir/lldp-exporter:<version> --help
```

The image is upstream's `ghcr.io/lldpd/lldpd` plus the exporter, so `lldpcli` always matches the daemon it talks to. Run it beside lldpd as uid 100 and gid 101, lldpd's privilege-separation user, which owns the control socket. The `sessrumnir.observability.lldp` role does both.

`json0` is used instead of `json` because `json` returns an object for one neighbour and a list for several, while `json0` always returns lists.

## Development

`make deps` installs the pre-commit hooks, which run on every commit and push. `make lint` runs the same hooks on all files.

```sh
make lint
make test
make build
```

Releases are cut by Release Please. A merged release pull request builds the image for amd64 and arm64, pushes it to `ghcr.io/sessrumnir/lldp-exporter` and attests it.

## License

MIT
