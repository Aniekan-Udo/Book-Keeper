"""Durable background work: a transactional-outbox job queue on Postgres (queue.py) and
the workers that run it (worker.py). Used for graph sync after ledger changes and memory
curation after replies; see Documentation.MD §9 (D46)."""
