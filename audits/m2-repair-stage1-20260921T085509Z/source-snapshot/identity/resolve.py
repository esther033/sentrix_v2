"""
Shared canonical service identity resolution, per the priority order
documented in identity/service-alias-map.yaml. Used by
smoke/quality-check.py (M1A/M1B gates) and scripts/canonicalize.py
(Milestone 2 canonical view) so the two never silently drift apart.
"""
import yaml


def load_alias_map(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_canonical_identity(attrs, alias_map):
    """Priority-ordered canonical_service_id resolution. `attrs` is a flat
    dict of whatever identity-shaped keys a record happens to carry (OTel
    resource attrs, k8s attrs, or Prometheus labels -- caller normalizes
    key names to the dotted OTel form before calling this).

    Returns (canonical_service_id_or_None, resolution_method_str)."""
    canonical_services = set(alias_map.get("canonical_services", []))
    aliases = alias_map.get("aliases", {})

    def try_name(raw):
        if raw is None:
            return None
        if raw in canonical_services:
            return raw
        if raw in aliases:
            return aliases[raw]
        return None

    # 1. service.namespace + service.name
    if attrs.get("service.namespace") and attrs.get("service.name"):
        name = try_name(attrs["service.name"])
        if name:
            return name, "otel_resource_service_name"

    # 2. k8s.namespace.name + k8s.deployment.name
    if attrs.get("k8s.namespace.name") and attrs.get("k8s.deployment.name"):
        name = try_name(attrs["k8s.deployment.name"])
        if name:
            return name, "k8s_deployment_name"

    # 3. k8s.pod.uid or container.id (identity present, but cannot map to
    #    a canonical service name without a deployment/service label --
    #    still counts as "resolved" for validity purposes: we know WHICH
    #    pod this is, even if we can't say which canonical service
    #    without further lookup)
    if attrs.get("k8s.pod.uid") or attrs.get("container.id"):
        return None, "k8s_pod_identity_only"

    # 4. Prometheus source-native labels. Two different scrape jobs need
    # two different extraction strategies here, both still "source-native
    # labels" (neither is an OTel attribute or an alias-map lookup):
    #   - our own OTel gateway's `prometheus` exporter (spanmetrics) sets
    #     job="<namespace>/<service>", so the job name itself IS the
    #     service name.
    #   - cAdvisor's kubernetes-nodes-cadvisor job is shared by every
    #     container on the node (job is always "kubernetes-nodes-cadvisor",
    #     never service-specific) and carries no k8s.deployment.name --
    #     only `namespace` + `pod` (the pod's own name, not the
    #     Deployment's). Since canonical_services is a closed, known list
    #     and every pod name is generated as "<deployment-name>-<hash>-<hash>"
    #     (Deployment) or "<statefulset-name>-<ordinal>" (StatefulSet),
    #     prefix-matching the pod name against that known list is a
    #     reliable, non-guessing resolution -- not a fuzzy/approximate
    #     match, since canonical service names are exactly the underlying
    #     Deployment/StatefulSet names by construction in this cluster.
    if attrs.get("namespace") and attrs.get("pod"):
        name = try_name(attrs.get("job", "").split("/")[-1] if attrs.get("job") else None)
        if name:
            return name, "prometheus_native_labels"
        pod = attrs["pod"]
        for svc in sorted(canonical_services, key=len, reverse=True):
            if pod == svc or pod.startswith(svc + "-"):
                return svc, "prometheus_native_labels_pod_prefix"

    return None, "unresolved"
