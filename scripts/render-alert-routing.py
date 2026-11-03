#!/usr/bin/env python3
"""Render a private, authenticated Prometheus alert route for operator deployment."""

import argparse
import ipaddress
import json
from pathlib import Path

import yaml


def render(config, target, server_name, environment):
    host, separator, port = target.rpartition(":")
    if not separator or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("private_alertmanager_address_required")
    address = ipaddress.ip_address(host)
    if not isinstance(address, ipaddress.IPv4Address) or not any(
        address in ipaddress.ip_network(network)
        for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ):
        raise ValueError("private_alertmanager_address_required")
    if server_name != "aler.codestra.media":
        raise ValueError("canonical_alertmanager_tls_name_required")
    if environment not in {"development", "test", "staging", "production"}:
        raise ValueError("environment_required")
    config = json.loads(json.dumps(config))
    config.setdefault("global", {}).setdefault("external_labels", {}).update(
        environment=environment, application="klyrow.com", service="gateway"
    )
    config["alerting"] = {
        "alertmanagers": [
            {
                "scheme": "https",
                "api_version": "v2",
                "timeout": "10s",
                "tls_config": {
                    "ca_file": "/run/secrets/alertmanager-ca.crt",
                    "cert_file": "/run/secrets/alertmanager-client.crt",
                    "key_file": "/run/secrets/alertmanager-client.key",
                    "server_name": server_name,
                    "insecure_skip_verify": False,
                },
                "static_configs": [{"targets": [target]}],
            }
        ]
    }
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("config/prometheus.yml"))
    parser.add_argument(
        "--target", required=True, help="Reviewed private IPv4:port of the mTLS listener"
    )
    parser.add_argument("--server-name", default="aler.codestra.media")
    parser.add_argument("--environment", required=True)
    args = parser.parse_args()
    try:
        output = render(
            yaml.safe_load(args.source.read_text()), args.target, args.server_name, args.environment
        )
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        parser.exit(2, "Alert routing configuration is invalid\n")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
