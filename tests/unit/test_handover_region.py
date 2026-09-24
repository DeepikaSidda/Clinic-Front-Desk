"""The clinic's voice must resolve a region without a developer's AWS config.

This is the bug that hid best. The Polly client was built with ``region_name=None``
and nothing ever passed a region, so botocore fell back to ``AWS_DEFAULT_REGION`` —
which the deployment does not set; its systemd unit sets ``AWS_REGION``. Every
synthesis on the instance failed with "You must specify a region", and because
synthesis errors are deliberately caught so a dead voice can never drop a call, the
only symptom was a caller hearing silence where the clinic should have spoken.

It passed every local test, because a developer machine has a region in
``~/.aws/config`` and the instance does not. So these tests clear the environment
first: that absence is the deployment, and it is the case that was never covered.
"""

from __future__ import annotations

from typing import Any

import pytest

from clinic_front_desk.handover.live import (
    LiveCallRegistry,
    LiveHandoverService,
    default_region,
)

_ALL_REGION_VARS = ("CLINIC_REGION", "AWS_REGION", "AWS_DEFAULT_REGION")


@pytest.fixture
def no_ambient_region(monkeypatch: Any) -> None:
    """A machine with no region configured anywhere — i.e. the instance."""
    for name in _ALL_REGION_VARS:
        monkeypatch.delenv(name, raising=False)


def test_aws_region_is_honoured(monkeypatch: Any, no_ambient_region: None) -> None:
    """What the deployment actually sets. This is the case that was broken."""
    monkeypatch.setenv("AWS_REGION", "ap-south-1")

    assert default_region() == "ap-south-1"


def test_aws_default_region_is_honoured(
    monkeypatch: Any, no_ambient_region: None
) -> None:
    """What botocore reads on its own, kept working for local shells."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-2")

    assert default_region() == "eu-west-2"


def test_clinic_region_wins(monkeypatch: Any, no_ambient_region: None) -> None:
    """Same precedence as the data layer, so one deploy cannot straddle regions."""
    monkeypatch.setenv("CLINIC_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")

    assert default_region() == "us-west-2"


def test_blank_values_are_ignored(monkeypatch: Any, no_ambient_region: None) -> None:
    """An empty variable is not a region, and passing it along fails at the API."""
    monkeypatch.setenv("CLINIC_REGION", "   ")
    monkeypatch.setenv("AWS_REGION", "us-east-2")

    assert default_region() == "us-east-2"


def test_there_is_always_a_region(no_ambient_region: None) -> None:
    """Never None. An unresolved region is a silent loss of the clinic's voice."""
    assert default_region() == "us-east-1"


def test_the_polly_client_is_built_with_a_region(
    monkeypatch: Any, no_ambient_region: None
) -> None:
    """The actual defect: the client was constructed with ``region_name=None``.

    Asserted on the call rather than on the helper, because the helper being right
    while the client ignores it is exactly the shape of the original bug.
    """
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    captured: dict[str, Any] = {}

    class _FakeBoto3:
        @staticmethod
        def client(service: str, **kwargs: Any) -> object:
            captured["service"] = service
            captured.update(kwargs)
            return object()

    import sys

    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3)

    service = LiveHandoverService(LiveCallRegistry())
    service._client()

    assert captured["service"] == "polly"
    assert captured["region_name"] == "ap-south-1", captured


def test_an_explicit_region_still_wins(monkeypatch: Any, no_ambient_region: None) -> None:
    """Constructor argument beats the environment, for tests and for multi-region."""
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    captured: dict[str, Any] = {}

    class _FakeBoto3:
        @staticmethod
        def client(service: str, **kwargs: Any) -> object:
            captured.update(kwargs)
            return object()

    import sys

    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3)

    LiveHandoverService(LiveCallRegistry(), region="eu-central-1")._client()

    assert captured["region_name"] == "eu-central-1"
