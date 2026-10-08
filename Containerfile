FROM ghcr.io/lldpd/lldpd:1.0.22@sha256:c12c113c432212b207e42d2a7caff81b22258c96858616c79c50af3c8fbb54c8

RUN apk add --no-cache python3

COPY --chmod=0755 lldp_exporter.py /usr/local/bin/lldp-exporter

USER 100:101

ENTRYPOINT ["/usr/local/bin/lldp-exporter"]
