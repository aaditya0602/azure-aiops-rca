# Brainstem — reproducible entry points.
#
# On Windows use the .venv python directly, e.g.
#   .\.venv\Scripts\python.exe harness\synth.py --seed 1337
# The targets below are what CI runs and what a Linux/macOS clone uses.

PY ?= .venv/bin/python
SEED ?= 1337
TOPOLOGY ?= topology/topology.yaml
THRESHOLD ?= 3.0

.PHONY: help venv synth eval sweep detection-curve test verify \
        up down logs real-scenario real-eval clean

help:
	@echo "venv            create .venv and install deps"
	@echo "verify          full offline check: unit tests + seeded eval gate (CI target)"
	@echo "synth           generate synthetic spans + ground-truth labels"
	@echo "eval            score detection + localization against labels"
	@echo "sweep           accuracy vs fault severity, multi-seed"
	@echo "detection-curve recall / false-positive / MTTD vs threshold"
	@echo "up / down       start / stop the real 7-node OTel stack"
	@echo "real-scenario   run load + fault injection against the live stack"
	@echo "real-eval       convert collector output and score it"

venv:
	python -m venv .venv
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -r requirements.txt -r requirements-dev.txt

# --- synthetic track (fast, deterministic, no Docker) ---

synth:
	$(PY) harness/synth.py --topology $(TOPOLOGY) --seed $(SEED) \
		--duration 7200 --rps 10 --faults 110 --p-concurrent 0.35

eval:
	$(PY) analyzer/eval.py --topology $(TOPOLOGY) --threshold $(THRESHOLD) \
		--out eval/results/synth_seed$(SEED).json

sweep:
	$(PY) analyzer/sweep.py --seeds 1337,7,99 --scales 1.0,0.5,0.3,0.18,0.1 \
		--threshold $(THRESHOLD) --out eval/results/sweep.json

detection-curve:
	$(PY) analyzer/threshold_sweep.py --out eval/results/detection_curve.json

test:
	$(PY) -m pytest -q tests/

# Triage the detected incidents. Offline by default (cassette replay).
# Live: make triage PROVIDER=azure_foundry  — needs AZURE_AI_* env vars.
PROVIDER ?= cassette
triage:
	$(PY) agent/run.py --provider $(PROVIDER) --threshold $(THRESHOLD) --limit 5

# Deterministic fixture the committed cassettes were recorded against. CI
# regenerates it byte-identically (fixed seed) so replay hits every cassette.
agent-fixture:
	$(PY) harness/synth.py --topology topology/small.yaml --seed 4242 \
		--duration 900 --rps 10 --faults 12 --quiet 15 \
		--fault-min 25 --fault-max 35 --p-concurrent 0.25 \
		--out-spans data/agentfix/spans.jsonl \
		--out-labels data/agentfix/injections.jsonl

# Replay the recorded cassettes offline — no key, no network.
agent-replay: agent-fixture
	$(PY) agent/run.py --provider cassette --topology topology/small.yaml \
		--spans data/agentfix/spans.jsonl --limit 4 --require-triaged 4

# The CI gate: unit tests, then a seeded end-to-end run that must clear both an
# absolute accuracy floor AND a margin over the baseline. Fully offline — no
# Docker, no Azure, no LLM key.
#
# The margin check is the one that matters: if error attribution silently breaks,
# `attributed` collapses onto `self_time` and absolute accuracy alone might still
# look acceptable. Fixed seed, so both numbers are exactly reproducible.
verify: test
	$(PY) harness/synth.py --topology $(TOPOLOGY) --seed $(SEED) \
		--duration 1800 --rps 8 --faults 40 --p-concurrent 0.35 \
		--out-spans data/ci/spans.jsonl --out-labels data/ci/injections.jsonl
	$(PY) analyzer/eval.py --topology $(TOPOLOGY) \
		--spans data/ci/spans.jsonl --labels data/ci/injections.jsonl \
		--threshold $(THRESHOLD) --gate-method attributed \
		--assert-top1 90 --assert-beats self_time:15

# --- real track (Docker + OTel Collector) ---

up:
	docker compose up -d --build
	@echo "gateway on http://localhost:8080/work"

down:
	docker compose down -v

logs:
	docker compose logs -f --tail=50

REAL_PORTS = gateway=http://localhost:8080,orders=http://localhost:8081,\
payments=http://localhost:8082,inventory=http://localhost:8083,\
ledger=http://localhost:8084,cache=http://localhost:8085,\
recommender=http://localhost:8086

# Load and fault injection run concurrently: the injector creates the incidents
# that the load makes visible.
#
# The collector is RESTARTED rather than the output file deleted. Deleting it
# while the collector holds the handle leaves the collector writing to an unlinked
# inode, so the run silently produces no spans at all.
real-scenario:
	docker compose restart otel-collector
	sleep 5
	$(PY) -u harness/loadgen.py --rps 25 --duration 430 --seed $(SEED) & \
	$(PY) -u harness/injector.py --topology topology/small.yaml \
		--ports "$(REAL_PORTS)" --groups 8 --fault-len 30 --quiet 15 \
		--p-concurrent 0.3 --seed $(SEED); \
	wait

real-eval:
	$(PY) harness/otlp_to_spans.py --in data/otlp/otlp-spans.jsonl \
		--out data/real/spans.jsonl
	$(PY) analyzer/eval.py --topology topology/small.yaml \
		--spans data/real/spans.jsonl --labels data/real/injections.jsonl \
		--threshold $(THRESHOLD) --out eval/results/real_stack.json

clean:
	rm -rf data/ __pycache__ analyzer/__pycache__ harness/__pycache__
