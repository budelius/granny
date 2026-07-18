"""Kubernetes service inventory for architecture analysis."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class KubernetesPort:
    name: str | None
    port: int | None
    target_port: str | int | None
    protocol: str


@dataclass(frozen=True)
class KubernetesServiceRef:
    namespace: str
    name: str
    kind: str
    service_type: str
    cluster_ip: str | None
    external_name: str | None
    external_ips: list[str]
    load_balancer_ips: list[str]
    ports: list[KubernetesPort]
    selector: dict[str, str]
    matched_workloads: list[str]
    endpoint_count: int
    ingress_hosts: list[str]
    labels: dict[str, str]
    annotations: dict[str, str]


@dataclass(frozen=True)
class KubernetesWorkloadRef:
    namespace: str
    kind: str
    name: str
    replicas: int | None
    ready_replicas: int | None
    labels: dict[str, str]
    images: list[str]


@dataclass(frozen=True)
class KubernetesIngressRef:
    namespace: str
    name: str
    class_name: str | None
    hosts: list[str]
    services: list[str]
    tls_hosts: list[str]


@dataclass(frozen=True)
class KubernetesServiceScan:
    context: str | None
    namespaces: list[str]
    services: list[KubernetesServiceRef]
    workloads: list[KubernetesWorkloadRef]
    ingresses: list[KubernetesIngressRef]
    findings: list[str] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        exposed = [s for s in self.services if _is_exposed(s)]
        return {
            "namespaces": len(self.namespaces),
            "services": len(self.services),
            "workloads": len(self.workloads),
            "ingresses": len(self.ingresses),
            "externally_exposed_services": len(exposed),
            "services_without_matched_workloads": len(
                [s for s in self.services if s.selector and not s.matched_workloads]
            ),
            "services_without_ready_endpoints": len(
                [s for s in self.services if s.kind != "ExternalName" and s.endpoint_count == 0]
            ),
            "selectorless_services": len([s for s in self.services if not s.selector]),
        }


def scan_kubernetes_services(
    *,
    context: str | None = None,
    kubeconfig: str | None = None,
    namespaces: list[str] | None = None,
    include_annotations: bool = False,
    enforce_readonly: bool = False,
) -> KubernetesServiceScan:
    """Scan Kubernetes Services and their ingress/workload relationships via kubectl."""
    scope = _Scope(context=context, kubeconfig=kubeconfig, namespaces=namespaces or [])
    if enforce_readonly:
        assert_readonly_access(scope)
    namespaces_seen = _namespace_names(_kubectl_json(scope, ["get", "namespaces"]))

    services_payload = _filter_payload(
        scope,
        _kubectl_json(scope, ["get", "services", *_namespace_args(scope), "-o", "json"]),
    )
    workloads_payloads = [
        _filter_payload(
            scope,
            _kubectl_json(
                scope,
                ["get", kind, *_namespace_args(scope), "-o", "json"],
                allow_not_found=True,
            ),
        )
        for kind in ("deployments", "statefulsets", "daemonsets", "jobs", "cronjobs")
    ]
    ingress_payload = _filter_payload(
        scope,
        _kubectl_json(
            scope,
            ["get", "ingress", *_namespace_args(scope), "-o", "json"],
            allow_not_found=True,
        ),
    )
    endpoints_by_service = _endpoint_counts(scope)

    workloads = [
        workload
        for payload in workloads_payloads
        for workload in _parse_workloads(payload)
    ]
    ingresses = _parse_ingresses(ingress_payload)
    ingress_hosts_by_service = _ingress_hosts_by_service(ingresses)
    services = _parse_services(
        services_payload,
        workloads=workloads,
        endpoints_by_service=endpoints_by_service,
        ingress_hosts_by_service=ingress_hosts_by_service,
        include_annotations=include_annotations,
    )

    namespaces_filter = set(scope.namespaces)
    active_namespaces = sorted(
        namespaces_filter
        or {item.namespace for item in [*services, *workloads, *ingresses]}
        or set(namespaces_seen)
    )
    findings = _build_findings(services)
    return KubernetesServiceScan(
        context=context,
        namespaces=active_namespaces,
        services=services,
        workloads=workloads,
        ingresses=ingresses,
        findings=findings,
    )


def render_mermaid(scan: KubernetesServiceScan) -> str:
    """Render a compact Mermaid graph of ingress -> service -> workload edges."""
    lines = ["flowchart LR"]
    for ingress in scan.ingresses:
        ingress_id = _node_id("ing", ingress.namespace, ingress.name)
        hosts = ", ".join(ingress.hosts) or ingress.name
        lines.append(f'  {ingress_id}["Ingress {ingress.namespace}/{hosts}"]')
        for service in ingress.services:
            namespace, name = service.split("/", 1)
            lines.append(f"  {ingress_id} --> {_node_id('svc', namespace, name)}")
    for service in scan.services:
        service_id = _node_id("svc", service.namespace, service.name)
        label = f"Service {service.namespace}/{service.name}\\n{service.service_type}"
        lines.append(f'  {service_id}["{label}"]')
        for workload in service.matched_workloads:
            kind_name = workload.split("/", 1)
            if len(kind_name) != 2:
                continue
            workload_id = _node_id("wl", service.namespace, workload.replace("/", "-"))
            lines.append(f'  {workload_id}["{workload}"]')
            lines.append(f"  {service_id} --> {workload_id}")
        if service.external_name:
            ext_id = _node_id("ext", service.namespace, service.name)
            lines.append(f'  {ext_id}(("{service.external_name}"))')
            lines.append(f"  {service_id} --> {ext_id}")
    return "\n".join(lines)


@dataclass(frozen=True)
class _Scope:
    context: str | None
    kubeconfig: str | None
    namespaces: list[str]


_WRITE_VERBS = ("create", "update", "patch", "delete", "deletecollection")
_WRITE_RESOURCES = (
    "pods",
    "services",
    "endpoints",
    "endpointslices.discovery.k8s.io",
    "deployments.apps",
    "statefulsets.apps",
    "daemonsets.apps",
    "jobs.batch",
    "cronjobs.batch",
    "ingresses.networking.k8s.io",
    "configmaps",
    "secrets",
)


def assert_readonly_access(scope: _Scope) -> None:
    """Fail when the active Kubernetes identity can mutate scanned resources."""
    writable: list[str] = []
    for verb in _WRITE_VERBS:
        for resource in _WRITE_RESOURCES:
            args = ["auth", "can-i", verb, resource, *_auth_namespace_args(scope)]
            try:
                allowed = _kubectl_text(scope, args).strip().lower()
            except RuntimeError as exc:
                raise RuntimeError(f"read-only preflight failed: {exc}") from exc
            if allowed == "yes":
                writable.append(f"{verb} {resource}")
    if writable:
        sample = ", ".join(writable[:8])
        more = "..." if len(writable) > 8 else ""
        raise RuntimeError(
            "Kubernetes context is not read-only; refusing to scan with writable credentials. "
            f"Allowed mutations: {sample}{more}"
        )


def _kubectl_json(scope: _Scope, args: list[str], *, allow_not_found: bool = False) -> dict[str, Any]:
    result = _run_kubectl(scope, args)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if allow_not_found and ("the server doesn't have a resource type" in stderr or "NotFound" in stderr):
            return {"items": []}
        raise RuntimeError(f"kubectl failed ({result.returncode}): {stderr[:500]}")
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("kubectl returned invalid JSON") from exc


def _kubectl_text(scope: _Scope, args: list[str]) -> str:
    result = _run_kubectl(scope, args)
    if result.returncode != 0:
        raise RuntimeError(f"kubectl failed ({result.returncode}): {result.stderr.strip()[:500]}")
    return result.stdout


def _run_kubectl(scope: _Scope, args: list[str]) -> subprocess.CompletedProcess[str]:
    command = ["kubectl"]
    if scope.context:
        command.extend(["--context", scope.context])
    if scope.kubeconfig:
        command.extend(["--kubeconfig", scope.kubeconfig])
    command.extend(args)
    try:
        return subprocess.run(command, check=False, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as exc:
        raise RuntimeError("kubectl not found in PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("kubectl command timed out") from exc


def _namespace_args(scope: _Scope) -> list[str]:
    if len(scope.namespaces) == 1:
        return ["-n", scope.namespaces[0]]
    if len(scope.namespaces) > 1:
        return ["--all-namespaces"]
    return ["--all-namespaces"]


def _auth_namespace_args(scope: _Scope) -> list[str]:
    if len(scope.namespaces) == 1:
        return ["-n", scope.namespaces[0]]
    return ["--all-namespaces"]


def _namespace_names(payload: dict[str, Any]) -> list[str]:
    return sorted(
        item.get("metadata", {}).get("name", "")
        for item in payload.get("items", [])
        if item.get("metadata", {}).get("name")
    )


def _filter_payload(scope: _Scope, payload: dict[str, Any]) -> dict[str, Any]:
    if len(scope.namespaces) <= 1:
        return payload
    wanted = set(scope.namespaces)
    return {
        **payload,
        "items": [
            item
            for item in payload.get("items", [])
            if item.get("metadata", {}).get("namespace") in wanted
        ],
    }


def _parse_services(
    payload: dict[str, Any],
    *,
    workloads: list[KubernetesWorkloadRef],
    endpoints_by_service: dict[str, int],
    ingress_hosts_by_service: dict[str, list[str]],
    include_annotations: bool,
) -> list[KubernetesServiceRef]:
    services: list[KubernetesServiceRef] = []
    for item in payload.get("items", []):
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        namespace = metadata.get("namespace", "default")
        name = metadata.get("name", "")
        selector = {str(k): str(v) for k, v in spec.get("selector", {}).items()}
        matched = [
            f"{w.kind}/{w.name}"
            for w in workloads
            if w.namespace == namespace and selector and _matches_selector(selector, w.labels)
        ]
        lb_ips = _load_balancer_ips(item)
        key = f"{namespace}/{name}"
        annotations = metadata.get("annotations", {}) if include_annotations else {}
        services.append(
            KubernetesServiceRef(
                namespace=namespace,
                name=name,
                kind=spec.get("type", "ClusterIP"),
                service_type=spec.get("type", "ClusterIP"),
                cluster_ip=spec.get("clusterIP"),
                external_name=spec.get("externalName"),
                external_ips=list(spec.get("externalIPs", [])),
                load_balancer_ips=lb_ips,
                ports=[_parse_port(p) for p in spec.get("ports", [])],
                selector=selector,
                matched_workloads=sorted(matched),
                endpoint_count=endpoints_by_service.get(key, 0),
                ingress_hosts=ingress_hosts_by_service.get(key, []),
                labels={str(k): str(v) for k, v in metadata.get("labels", {}).items()},
                annotations={str(k): str(v) for k, v in annotations.items()},
            )
        )
    return sorted(services, key=lambda s: (s.namespace, s.name))


def _parse_workloads(payload: dict[str, Any]) -> list[KubernetesWorkloadRef]:
    workloads: list[KubernetesWorkloadRef] = []
    for item in payload.get("items", []):
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})
        kind = item.get("kind", "Workload")
        pod_template = _pod_template(item)
        labels = pod_template.get("metadata", {}).get("labels", {}) or metadata.get("labels", {})
        containers = pod_template.get("spec", {}).get("containers", [])
        workloads.append(
            KubernetesWorkloadRef(
                namespace=metadata.get("namespace", "default"),
                kind=kind,
                name=metadata.get("name", ""),
                replicas=spec.get("replicas"),
                ready_replicas=status.get("readyReplicas") or status.get("numberReady"),
                labels={str(k): str(v) for k, v in labels.items()},
                images=sorted({str(c.get("image")) for c in containers if c.get("image")}),
            )
        )
    return sorted(workloads, key=lambda w: (w.namespace, w.kind, w.name))


def _parse_ingresses(payload: dict[str, Any]) -> list[KubernetesIngressRef]:
    ingresses: list[KubernetesIngressRef] = []
    for item in payload.get("items", []):
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        namespace = metadata.get("namespace", "default")
        services: set[str] = set()
        hosts: list[str] = []
        for rule in spec.get("rules", []):
            host = rule.get("host")
            if host:
                hosts.append(str(host))
            for path in rule.get("http", {}).get("paths", []):
                name = path.get("backend", {}).get("service", {}).get("name")
                if name:
                    services.add(f"{namespace}/{name}")
        default_service = spec.get("defaultBackend", {}).get("service", {}).get("name")
        if default_service:
            services.add(f"{namespace}/{default_service}")
        tls_hosts = [str(h) for tls in spec.get("tls", []) for h in tls.get("hosts", [])]
        ingresses.append(
            KubernetesIngressRef(
                namespace=namespace,
                name=metadata.get("name", ""),
                class_name=spec.get("ingressClassName"),
                hosts=sorted(set(hosts)),
                services=sorted(services),
                tls_hosts=sorted(set(tls_hosts)),
            )
        )
    return sorted(ingresses, key=lambda i: (i.namespace, i.name))


def _endpoint_counts(scope: _Scope) -> dict[str, int]:
    try:
        payload = _filter_payload(
            scope,
            _kubectl_json(
                scope,
                ["get", "endpointslices", *_namespace_args(scope), "-o", "json"],
                allow_not_found=True,
            ),
        )
        counts: dict[str, int] = {}
        for item in payload.get("items", []):
            metadata = item.get("metadata", {})
            namespace = metadata.get("namespace", "default")
            service = metadata.get("labels", {}).get("kubernetes.io/service-name")
            if not service:
                continue
            key = f"{namespace}/{service}"
            counts[key] = counts.get(key, 0) + len(item.get("endpoints", []))
        if counts:
            return counts
    except RuntimeError:
        pass

    payload = _filter_payload(
        scope,
        _kubectl_json(
            scope,
            ["get", "endpoints", *_namespace_args(scope), "-o", "json"],
            allow_not_found=True,
        ),
    )
    counts = {}
    for item in payload.get("items", []):
        metadata = item.get("metadata", {})
        key = f"{metadata.get('namespace', 'default')}/{metadata.get('name', '')}"
        counts[key] = sum(len(subset.get("addresses", [])) for subset in item.get("subsets", []))
    return counts


def _pod_template(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("kind") == "CronJob":
        return item.get("spec", {}).get("jobTemplate", {}).get("spec", {}).get("template", {})
    return item.get("spec", {}).get("template", {})


def _parse_port(port: dict[str, Any]) -> KubernetesPort:
    return KubernetesPort(
        name=port.get("name"),
        port=port.get("port"),
        target_port=port.get("targetPort"),
        protocol=port.get("protocol", "TCP"),
    )


def _matches_selector(selector: dict[str, str], labels: dict[str, str]) -> bool:
    return all(labels.get(key) == value for key, value in selector.items())


def _load_balancer_ips(item: dict[str, Any]) -> list[str]:
    ingresses = item.get("status", {}).get("loadBalancer", {}).get("ingress", [])
    values = []
    for ingress in ingresses:
        value = ingress.get("ip") or ingress.get("hostname")
        if value:
            values.append(str(value))
    return sorted(values)


def _ingress_hosts_by_service(ingresses: list[KubernetesIngressRef]) -> dict[str, list[str]]:
    mapping: dict[str, set[str]] = {}
    for ingress in ingresses:
        for service in ingress.services:
            mapping.setdefault(service, set()).update(ingress.hosts or [ingress.name])
    return {key: sorted(values) for key, values in mapping.items()}


def _is_exposed(service: KubernetesServiceRef) -> bool:
    return bool(
        service.service_type in {"LoadBalancer", "NodePort"}
        or service.external_ips
        or service.load_balancer_ips
        or service.ingress_hosts
    )


def _build_findings(services: list[KubernetesServiceRef]) -> list[str]:
    findings = []
    for service in services:
        label = f"{service.namespace}/{service.name}"
        if service.selector and not service.matched_workloads:
            findings.append(f"{label}: selector does not match any scanned workload")
        if service.kind != "ExternalName" and service.endpoint_count == 0:
            findings.append(f"{label}: no ready endpoints reported")
        if service.service_type in {"LoadBalancer", "NodePort"} and not service.ingress_hosts:
            findings.append(f"{label}: directly exposed as {service.service_type}")
    return findings


def _node_id(prefix: str, namespace: str, name: str) -> str:
    safe = "_".join(part.replace("-", "_").replace(".", "_") for part in (prefix, namespace, name))
    return safe.replace("/", "_")
