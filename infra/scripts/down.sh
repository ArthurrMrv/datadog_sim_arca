#!/usr/bin/env bash
# Deletes the whole cluster. Ground truth and incident reports live in results/ and survive.
set -euo pipefail
kind delete cluster --name rca-sim
