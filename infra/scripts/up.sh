#!/usr/bin/env bash
# Phase 1-4: cluster, application, Datadog Agent, OTel Collector, Chaos Mesh. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/../.."

# Pinned versions: reproducibility of every experiment depends on them.
DATADOG_CHART_VERSION=${DATADOG_CHART_VERSION:-3.70.4}
CHAOS_MESH_VERSION=${CHAOS_MESH_VERSION:-2.6.3}
DEPLOY_JAEGER=${DEPLOY_JAEGER:-0}

[[ -f .env ]] && set -a && . ./.env && set +a
: "${DD_API_KEY:?set DD_API_KEY in .env}"
: "${DD_SITE:=datadoghq.eu}"

if ! kind get clusters 2>/dev/null | grep -qx rca-sim; then
  echo "== creating kind cluster"
  kind create cluster --config infra/kind-config.yaml --wait 120s
fi

echo "== Online Boutique (Phase 1)"
kubectl apply -k infra/online-boutique

echo "== Datadog Agent (Phase 2)"
kubectl create namespace datadog --dry-run=client -o yaml | kubectl apply -f -
kubectl -n datadog create secret generic datadog-secret \
  --from-literal=api-key="$DD_API_KEY" --dry-run=client -o yaml | kubectl apply -f -
helm repo add datadog https://helm.datadoghq.com >/dev/null
helm repo update datadog >/dev/null
helm upgrade --install datadog datadog/datadog \
  --version "$DATADOG_CHART_VERSION" \
  --namespace datadog \
  --values infra/datadog/values.yaml \
  --set datadog.site="$DD_SITE"

echo "== OTel Collector (Phase 3)"
kubectl apply -f infra/otel/deployment.yaml
kubectl -n observability create configmap otel-collector-config \
  --from-file=collector.yaml=infra/otel/collector.yaml \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n observability rollout restart deployment/otel-collector
[[ "$DEPLOY_JAEGER" == "1" ]] && kubectl apply -f infra/jaeger/jaeger.yaml

echo "== Chaos Mesh (Phase 4)"
helm repo add chaos-mesh https://charts.chaos-mesh.org >/dev/null
helm repo update chaos-mesh >/dev/null
kubectl create namespace chaos-mesh --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
  --version "$CHAOS_MESH_VERSION" \
  --namespace chaos-mesh \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/containerd/containerd.sock \
  --set dashboard.create=false

echo "== waiting for the application"
kubectl -n shop wait --for=condition=available --timeout=600s deployment --all
echo "up. Give the baseline ~15 minutes to settle before injecting anything (Phase 1 acceptance)."
