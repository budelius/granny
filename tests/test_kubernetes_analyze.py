"""Unit tests for Kubernetes architecture scanner (kubectl mocked)."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest
from click.testing import CliRunner

from granny.analyze import kubernetes as k8s_mod
from granny.cli.analyze import analyze


def _payload(kind: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": kind, "items": items}


NAMESPACES = _payload(
    "NamespaceList",
    [
        {"metadata": {"name": "default"}},
        {"metadata": {"name": "prod"}},
    ],
)

SERVICES = _payload(
    "ServiceList",
    [
        {
            "metadata": {"namespace": "prod", "name": "api", "labels": {"app": "api"}},
            "spec": {
                "type": "ClusterIP",
                "clusterIP": "10.0.0.1",
                "selector": {"app": "api"},
                "ports": [{"name": "http", "port": 80, "targetPort": 8080, "protocol": "TCP"}],
            },
        },
        {
            "metadata": {"namespace": "prod", "name": "db"},
            "spec": {"type": "ExternalName", "externalName": "db.example.com", "ports": [{"port": 5432}]},
        },
    ],
)

DEPLOYMENTS = _payload(
    "DeploymentList",
    [
        {
            "kind": "Deployment",
            "metadata": {"namespace": "prod", "name": "api"},
            "spec": {
                "replicas": 2,
                "template": {
                    "metadata": {"labels": {"app": "api"}},
                    "spec": {"containers": [{"image": "example/api:1"}]},
                },
            },
            "status": {"readyReplicas": 2},
        }
    ],
)

EMPTY_LIST = _payload("List", [])

INGRESSES = _payload(
    "IngressList",
    [
        {
            "metadata": {"namespace": "prod", "name": "api"},
            "spec": {
                "ingressClassName": "nginx",
                "rules": [
                    {
                        "host": "api.example.com",
                        "http": {"paths": [{"backend": {"service": {"name": "api"}}}]},
                    }
                ],
                "tls": [{"hosts": ["api.example.com"]}],
            },
        }
    ],
)

ENDPOINTSLICES = _payload(
    "EndpointSliceList",
    [
        {
            "metadata": {
                "namespace": "prod",
                "name": "api-abc",
                "labels": {"kubernetes.io/service-name": "api"},
            },
            "endpoints": [{"addresses": ["10.1.0.1"]}, {"addresses": ["10.1.0.2"]}],
        }
    ],
)


def _fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
    text = " ".join(command)
    if " auth can-i " in text:
        return subprocess.CompletedProcess(command, 0, stdout="no\n", stderr="")
    if " get namespaces" in text:
        payload = NAMESPACES
    elif " get services" in text:
        payload = SERVICES
    elif " get deployments" in text:
        payload = DEPLOYMENTS
    elif " get ingress" in text:
        payload = INGRESSES
    elif " get endpointslices" in text:
        payload = ENDPOINTSLICES
    else:
        payload = EMPTY_LIST
    return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")


def test_scan_kubernetes_services_maps_ingress_service_workload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(k8s_mod.subprocess, "run", _fake_run)

    scan = k8s_mod.scan_kubernetes_services(
        context="prod",
        namespaces=["prod"],
        enforce_readonly=True,
    )

    assert scan.summary["services"] == 2
    assert scan.summary["externally_exposed_services"] == 1
    api = next(service for service in scan.services if service.name == "api")
    assert api.matched_workloads == ["Deployment/api"]
    assert api.endpoint_count == 2
    assert api.ingress_hosts == ["api.example.com"]
    assert scan.ingresses[0].services == ["prod/api"]


def test_scan_refuses_writable_context(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_writable_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if "auth" in command and "delete" in command and "services" in command:
            return subprocess.CompletedProcess(command, 0, stdout="yes\n", stderr="")
        if "auth" in command:
            return subprocess.CompletedProcess(command, 0, stdout="no\n", stderr="")
        return _fake_run(command, **_)

    monkeypatch.setattr(k8s_mod.subprocess, "run", fake_writable_run)

    with pytest.raises(RuntimeError, match="not read-only"):
        k8s_mod.scan_kubernetes_services(context="prod", enforce_readonly=True)


def test_k8s_services_cli_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(k8s_mod.subprocess, "run", _fake_run)

    result = CliRunner().invoke(
        analyze,
        ["k8s-services", "--context", "prod", "--namespace", "prod", "--json-output"],
    )

    assert result.exit_code == 0, result.output
    assert '"context": "prod"' in result.output
    assert '"name": "api"' in result.output
    assert '"matched_workloads": [\n        "Deployment/api"\n      ]' in result.output
