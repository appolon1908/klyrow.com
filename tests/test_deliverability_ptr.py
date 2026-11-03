"""Verify reverse-DNS evidence through the tenant-scoped deliverability route."""

import socket

import dns.resolver
import pytest
from sqlalchemy import select

from apps.gateway.app import saas


def dns_answers(monkeypatch, records, *, require_reverse_name=False):
    queries = []

    def forward(host):
        assert host == "mail.example.com"
        return "192.0.2.45"

    def resolve(name, kind):
        if kind != "PTR":
            return []
        queries.append(str(name).rstrip("."))
        if require_reverse_name and queries[-1] != "45.2.0.192.in-addr.arpa":
            raise dns.resolver.NXDOMAIN
        if isinstance(records, Exception):
            raise records
        return records

    monkeypatch.setattr(saas.socket, "gethostbyname", forward)
    monkeypatch.setattr(saas.dns.resolver, "resolve", resolve)
    return queries


def check(client):
    response = client.post("/v1/deliverability/domains/tenant-a/check")
    assert response.status_code == 200, response.text
    return response.json()


def test_ptr_queries_all_address_octets_and_persists_evidence(gateway, monkeypatch):
    client, sessions, _ = gateway
    queries = dns_answers(monkeypatch, ["mail.example.com."], require_reverse_name=True)
    result = check(client)
    assert result["ptr"] is True
    assert queries == ["45.2.0.192.in-addr.arpa"]
    assert result["launch_ready"] is False
    with sessions() as session:
        snapshot = session.scalar(select(saas.DeliverabilitySnapshot))
        assert snapshot.tenant_id == "tenant-a" and snapshot.ptr is True


@pytest.mark.parametrize(
    "records, expected",
    [
        (["MAIL.EXAMPLE.COM."], True),
        (["mail.example.com"], True),
        (["unrelated.example.", "mail.example.com."], True),
        (["mail.example.com.attacker.invalid."], False),
        (["evilmail.example.com."], False),
        (["unrelated.example."], False),
        ([], False),
        (dns.resolver.NXDOMAIN(), False),
        (dns.resolver.LifetimeTimeout(), False),
    ],
)
def test_ptr_requires_exact_normalized_mail_host(gateway, monkeypatch, records, expected):
    client, _, _ = gateway
    dns_answers(monkeypatch, records)
    result = check(client)
    assert result["ptr"] is expected
    assert ("ptr_missing" in {alert["code"] for alert in result["alerts"]}) is (not expected)
    assert result["launch_ready"] is False


def test_missing_forward_address_fails_closed(gateway, monkeypatch):
    client, _, _ = gateway
    queries = dns_answers(monkeypatch, ["mail.example.com."])

    def unavailable(_host):
        raise socket.gaierror("synthetic lookup failure")

    monkeypatch.setattr(saas.socket, "gethostbyname", unavailable)
    assert check(client)["ptr"] is False
    assert queries == []


def test_other_tenant_domain_is_rejected_before_dns(gateway, monkeypatch):
    client, sessions, _ = gateway
    queries = dns_answers(monkeypatch, ["mail.example.com."])
    response = client.post("/v1/deliverability/domains/tenant-b/check")
    assert response.status_code == 404
    assert queries == []
    with sessions() as session:
        assert session.scalar(select(saas.DeliverabilitySnapshot)) is None
