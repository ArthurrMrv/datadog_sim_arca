# One entry point for the whole pipeline. Every target is safe to re-run.
.DEFAULT_GOAL := help
SHELL := /bin/bash

VENV       ?= .venv
PY         := $(VENV)/bin/python
FAULT      ?= cpu
SERVICE    ?= cartservice
DURATION   ?= 300
CONFIG     ?= experiments/configs/base.yaml
MODE       ?= webhook

.PHONY: help venv test lint up down status clusters prepull pause resume monitors calibrate rca tunnel inject analyze replay eval sweep clean

help: ## show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'

venv: $(VENV)/.installed ## create the venv and install rca-service (PRISM is in app/prism/)
$(VENV)/.installed: rca-service/pyproject.toml
	python3 -m venv $(VENV)
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e "rca-service[dev]"
	@touch $@

test: venv ## unit tests: contract, windows, adapter, scoring. No cluster, no Datadog account
	$(VENV)/bin/pytest -q rca-service/tests

lint: venv ## ruff over the whole repo (one config: ruff.toml)
	$(VENV)/bin/ruff check .

up: ## create the cluster and deploy app, Agent, Collector, Chaos Mesh (Phases 1-4)
	infra/scripts/up.sh

down: ## delete the cluster (results/ survives)
	infra/scripts/down.sh

clusters: ## list kind clusters: any besides rca-sim is a container holding RAM for nothing
	@kind get clusters

pause: ## stop the cluster without deleting it (keeps images and deployments; resume is seconds)
	docker stop rca-sim-control-plane

resume: ## restart a paused cluster, then re-check that metrics are flowing
	docker start rca-sim-control-plane
	@sleep 20 && infra/scripts/status.sh

prepull: ## pull the app images on the host and load them into the node
	@# Two problems at once: the node needs no registry DNS for these, and a recreated cluster reuses
	@# the host's image cache instead of re-downloading ~1.5 GB.
	@kubectl kustomize infra/online-boutique | grep -oE 'image: .+' | cut -d' ' -f2 | sort -u \
	  | while read -r img; do \
	      echo "== $$img"; docker pull -q "$$img" && kind load docker-image --name rca-sim "$$img"; \
	    done

status: venv ## pods, Agent checks, Collector, freshness of the metrics the adapter reads
	infra/scripts/status.sh

monitors: venv ## apply the Datadog webhook, monitors and dashboard (URL= public /webhook URL)
	$(PY) datadog/monitors/apply.py apply $(if $(URL),--url $(URL),)

calibrate: venv ## suggest monitor thresholds from a quiet baseline (Phase 6.1)
	$(PY) datadog/monitors/apply.py calibrate --hours $(or $(HOURS),1)

rca: venv ## run the RCA service (MODE=webhook or MODE=poll)
ifeq ($(MODE),poll)
	$(PY) -m app.cli poll
else
	$(VENV)/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --app-dir rca-service
endif

tunnel: ## public HTTPS URL for the webhook; pass it to `make monitors URL=...`
	cloudflared tunnel --url http://localhost:8000

inject: venv ## one fault with ground-truth logging: FAULT= SERVICE= DURATION=
	$(PY) chaos/inject.py $(FAULT) $(SERVICE) --duration $(DURATION)

analyze: venv ## offline: pull the window around T= (unix seconds) and run PRISM
	PYTHONPATH=rca-service $(PY) -m app.cli analyze --at $(T)

replay: venv ## re-rank a stored incident with today's PRISM: ID=
	PYTHONPATH=rca-service $(PY) -m app.cli replay $(ID)

eval: venv ## full live evaluation campaign (Phase 7)
	$(PY) experiments/live_eval.py --config $(CONFIG)

sweep: venv ## metric-resolution experiment over stored incidents (Phase 8)
	$(PY) experiments/granularity_sweep.py

clean: ## remove the venv and caches
	rm -rf $(VENV) .pytest_cache rca-service/.pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
