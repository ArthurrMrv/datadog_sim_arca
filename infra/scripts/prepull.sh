#!/usr/bin/env bash
# Pull every image the cluster needs on the host, then load them into the kind node.
#
# Solves two things at once: the node never needs registry DNS (which is unreachable from a kind node
# in many devcontainers and Codespaces), and a recreated cluster reuses the host's image cache instead
# of re-downloading ~2 GB.
set -euo pipefail
cd "$(dirname "$0")/../.."
. infra/versions.env

helm repo add datadog https://helm.datadoghq.com >/dev/null 2>&1 || true
helm repo add chaos-mesh https://charts.chaos-mesh.org >/dev/null 2>&1 || true
helm repo update >/dev/null 2>&1 || true

# Rendered manifests, from the same sources up.sh applies. A chart that fails to render is skipped
# rather than fatal: its images still pull on demand if the node can reach a registry.
manifests() {
  kubectl kustomize infra/online-boutique
  cat infra/otel/deployment.yaml
  [[ "$DEPLOY_JAEGER" == "1" ]] && cat infra/jaeger/jaeger.yaml
  helm template datadog datadog/datadog --version "$DATADOG_CHART_VERSION" \
    --values infra/datadog/values.yaml 2>/dev/null || echo "WARN: datadog chart did not render" >&2
  helm template chaos-mesh chaos-mesh/chaos-mesh --version "$CHAOS_MESH_VERSION" \
    --set chaosDaemon.runtime=containerd --set dashboard.create=false 2>/dev/null \
    || echo "WARN: chaos-mesh chart did not render" >&2
}

# Unquoted on the kind load line on purpose: image refs never contain spaces, and one load of the
# whole set moves a single tarball instead of one per image.
images=$(manifests | grep -hoE 'image:[[:space:]]*"?[^"[:space:]]+' \
  | sed -E 's/image:[[:space:]]*"?//' | sort -u)

for image in $images; do
  echo "== $image"
  docker pull -q "$image"
done

if kind get clusters 2>/dev/null | grep -qx rca-sim; then
  kind load docker-image --name rca-sim $images
else
  echo "cluster not up yet: images are cached on the host, re-run this after 'make up'"
fi
