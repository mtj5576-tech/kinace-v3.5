from __future__ import annotations

from pathlib import Path

import pytest

from kinace_v3.holdout_access import (
    FORMAL_TRACK,
    SANDBOX_TRACK,
    HoldoutAccessError,
    HoldoutConsumedError,
    HoldoutNotEligibleError,
    assert_registry_separation,
    bind_holdout_extension_access,
    holdout_access_count,
    load_holdout_access_registry,
    open_holdout_access,
)


WINDOW = {
    "start": "2026-01-01",
    "end": "2026-03-01",
    "sessions": 60,
}
PROTOCOL = "protocol-001"
CANDIDATE = "candidate-001"


def _open(path: Path, *, candidate: str = CANDIDATE, track: str = FORMAL_TRACK):
    return open_holdout_access(
        candidate,
        PROTOCOL,
        WINDOW,
        eligible=True,
        registry_path=path,
        track=track,
    )


def test_preeligible_access_does_not_create_registry(tmp_path):
    registry_path = tmp_path / "holdout-access.json"

    with pytest.raises(HoldoutNotEligibleError, match="non-holdout"):
        open_holdout_access(
            CANDIDATE,
            PROTOCOL,
            WINDOW,
            eligible=False,
            registry_path=registry_path,
            track=FORMAL_TRACK,
        )

    assert not registry_path.exists()
    assert holdout_access_count(registry_path, track=FORMAL_TRACK) == 0
    registry = load_holdout_access_registry(registry_path, track=FORMAL_TRACK)
    assert registry["access_count"] == 0
    assert registry["pre_eligible_count"] == 0


def test_eligible_access_is_append_only_and_one_shot(tmp_path):
    registry_path = tmp_path / "holdout-access.json"

    access = _open(registry_path)

    assert access["read_count"] == 1
    assert access["candidate"] == CANDIDATE
    assert access["protocol"] == PROTOCOL
    assert access["track"] == FORMAL_TRACK
    assert access["authorized_at_utc"]
    registry = load_holdout_access_registry(registry_path, track=FORMAL_TRACK)
    assert registry["access_count"] == 1
    assert len(registry["entries"]) == 1

    with pytest.raises(HoldoutConsumedError, match="holdout_consumed") as exc_info:
        _open(registry_path, candidate="candidate-002")

    assert exc_info.value.reason_code == "holdout_consumed"
    assert holdout_access_count(registry_path, track=FORMAL_TRACK) == 1
    assert len(load_holdout_access_registry(registry_path, track=FORMAL_TRACK)["entries"]) == 1


def test_extension_reuses_original_authorization_without_another_read(tmp_path):
    registry_path = tmp_path / "holdout-access.json"
    access = _open(registry_path)
    extension_window = {
        "start": WINDOW["start"],
        "end": "2026-04-01",
        "sessions": 90,
    }

    with pytest.raises(HoldoutConsumedError, match="holdout_consumed"):
        open_holdout_access(
            "candidate-002",
            PROTOCOL,
            extension_window,
            eligible=True,
            registry_path=registry_path,
            track=FORMAL_TRACK,
        )

    binding = bind_holdout_extension_access(
        holdout_access_id=access["holdout_access_id"],
        candidate=CANDIDATE,
        protocol=PROTOCOL,
        extension_window=extension_window,
        freeze_id="freeze-001",
        extension_policy_id="policy-001",
        registry_path=registry_path,
        track=FORMAL_TRACK,
    )

    registry = load_holdout_access_registry(registry_path, track=FORMAL_TRACK)
    assert binding["read_count"] == 1
    assert binding["read_count_delta"] == 0
    assert registry["access_count"] == 1
    assert len(registry["entries"]) == 1
    assert registry["extension_count"] == 1

    with pytest.raises(HoldoutConsumedError, match="holdout_consumed"):
        bind_holdout_extension_access(
            holdout_access_id=access["holdout_access_id"],
            candidate=CANDIDATE,
            protocol=PROTOCOL,
            extension_window=extension_window,
            freeze_id="freeze-002",
            extension_policy_id="policy-001",
            registry_path=registry_path,
            track=FORMAL_TRACK,
        )


def test_formal_and_sandbox_registries_are_physically_separate(tmp_path):
    formal_path = tmp_path / "formal" / "holdout-access.json"
    sandbox_path = tmp_path / "sandbox" / "holdout-access.json"

    formal = _open(formal_path, track=FORMAL_TRACK)
    sandbox = _open(sandbox_path, track=SANDBOX_TRACK)

    assert formal["holdout_access_id"] != sandbox["holdout_access_id"]
    assert formal["registry_id"] != sandbox["registry_id"]
    assert_registry_separation(formal_path, sandbox_path)

    with pytest.raises(HoldoutAccessError, match="separate"):
        assert_registry_separation(formal_path, formal_path)
