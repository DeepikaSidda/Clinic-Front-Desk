"""Shared pytest fixtures for the Clinic Front-Desk Voice Agent test suite.

This is a skeleton established in task 1.1. Concrete fixtures are added as the
implementation lands:

- In-memory fake stores (task 3.2) — the default, fast, deterministic backend
  for property tests.
- A fault-injection store wrapper (task 3.3) for atomicity/failure-path tests.
- A DynamoDB-local / moto-backed table fixture (used by the storage-swap
  equivalence integration test, Property 27 / task 4.2).
- A mocked ``VoiceStreamManager`` boundary (task 9.1) for orchestration,
  barge-in, and interpretation-failure tests.

Test tiers are marked via the markers registered in ``pytest.ini``
(``property``, ``integration``, ``smoke``, ``latency``); unit tests are
unmarked.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def dynamodb_local_available() -> bool:
    """Placeholder capability flag for the DynamoDB-local fixture.

    Replaced in task 4.2 with a real moto/DynamoDB-local table fixture. Kept
    here so the conftest imports cleanly with zero tests collected.
    """
    return False
