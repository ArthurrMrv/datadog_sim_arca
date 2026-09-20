#!/usr/bin/env bash
# Phase 1-4: cluster, application, Datadog Agent, OTel Collector, Chaos Mesh. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/../.."

. infra/versions.env

[[ -f .env ]] && set -a && . ./.env && set +a
: "${DD_API_KEY:?set DD_API_KEY in .env}"
: "${DD_SITE:=datadoghq.eu}"

if ! kind get clusters 2>/dev/null | grep -qx rca-sim; then
  echo "== creating kind cluster"
  kind create cluster --config infra/kind-config.yaml --wait 120s
fi

# kind copies the host's resolver into the node. In a devcontainer, a Codespace or behind a corporate
# resolver that address is often unreachable from the node's own network, and every image pull fails
# with a DNS error that looks like a registry outage. Checked on every run, because a container
# restart resets the file. `getent` is used because the node image ships no nslookup or dig.
if ! docker exec rca-sim-control-plane getent hosts registry-1.docker.io >/dev/null 2>&1; then
  echo "== node cannot resolve registries, pointing it at public DNS"
  docker exec rca-sim-control-plane \
    sh -c 'printf "nameserver 8.8.8.8\nnameserver 1.1.1.1\n" > /etc/resolv.conf'
fi

# PREPULL=1 pulls every image on the host and loads it into the node first: needed where the node
# cannot reach a registry at all, and a large time saver on any re-created cluster.
[[ "${PREPULL:-0}" == "1" ]] && infra/scripts/prepull.sh

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

# The Collector must be (re)created *after* the Agent is serving, or its conntrack entry for the
# Agent's ClusterIP pins to a pod that is still terminating -- and every later dial is refused
# instantly, even though the Service, endpoints and listener are all correct. That is the failure
# that looked like a broken OTLP receiver for an hour.
echo "== waiting for the Agent to be ready before touching the Collector"
kubectl -n datadog rollout status ds/datadog --timeout=300s

echo "== OTel Collector (Phase 3)"
# ConfigMap before Deployment: the other order starts a pod that cannot mount its config, so the
# first thing `make status` shows is a CreateContainerConfigError that fixes itself.
kubectl create namespace observability --dry-run=client -o yaml | kubectl apply -f -
kubectl -n observability create configmap otel-collector-config \
  --from-file=collector.yaml=infra/otel/collector.yaml \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f infra/otel/deployment.yaml
# Picks up an edited collector.yaml on a re-run, and gives the pod a fresh network identity.
kubectl -n observability rollout restart deployment/otel-collector
kubectl -n observability rollout status deployment/otel-collector --timeout=180s
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
