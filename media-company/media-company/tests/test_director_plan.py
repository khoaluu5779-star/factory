from __future__ import annotations

import copy
import json

import jsonschema
import pytest

from factory.contracts.director_plan import (
    SUPPORTED_SCHEMA_VERSIONS,
    check_plan_against_channel,
    director_plan_json_schema,
    parse_director_plan,
)
from factory.contracts.errors import ContractViolation, UnsupportedSchemaVersion
from factory.core.workspace import JobWorkspace
from factory.contracts.stage import StageContext
from factory.stages.fakes import FakeDirectorStage
from tests.conftest import REPO_ROOT

JOB_ID = "JOB-20260927-drama-1234567890"


@pytest.fixture
def plan_dict(channel, tmp_path):
    ctx = StageContext(
        job_id=JOB_ID,
        channel=channel,
        workspace=JobWorkspace(tmp_path, JOB_ID),
        attempt=1,
        max_attempts=1,
        publish_authorization=None,
        _load_document=lambda n: None,
    )
    return FakeDirectorStage().build_plan(ctx)


def test_valid_plan_parses(plan_dict):
    plan = parse_director_plan(plan_dict)
    assert plan.schema_version == "1.0"
    assert plan.job_id == JOB_ID
    assert len(plan.scenes) == 1


def test_schema_version_recognized():
    assert "1.0" in SUPPORTED_SCHEMA_VERSIONS


@pytest.mark.parametrize("version", ["2.0", "0.9", None, 1.0])
def test_unsupported_or_missing_schema_version_rejected(plan_dict, version):
    plan_dict["schema_version"] = version
    with pytest.raises(UnsupportedSchemaVersion):
        parse_director_plan(plan_dict)


def test_missing_schema_version_key_rejected(plan_dict):
    del plan_dict["schema_version"]
    with pytest.raises(UnsupportedSchemaVersion):
        parse_director_plan(plan_dict)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(job_id="not-a-job-id"),
        lambda p: p.update(unexpected="field"),
        lambda p: p["scenes"][0].update(index=2),
        lambda p: p["scenes"][0].update(character_ids=["ghost"]),
        lambda p: p["scenes"][0].update(image_prompt="x" * 601),
        lambda p: p["scenes"].clear(),
        lambda p: p["scenes"][0].update(lines=[]),
        lambda p: p["experiment"].update(format="long"),
        lambda p: p.update(target_duration_s=-5),
    ],
)
def test_invalid_plan_rejected(plan_dict, mutate):
    mutate(plan_dict)
    with pytest.raises(ContractViolation):
        parse_director_plan(plan_dict)


def test_non_object_rejected():
    with pytest.raises(ContractViolation):
        parse_director_plan(["not", "a", "dict"])


def test_duplicate_characters_rejected(plan_dict):
    char = {"id": "hero", "name": "Hero", "description": "d", "voice_role": "protagonist"}
    plan_dict["characters"] = [char, dict(char)]
    with pytest.raises(ContractViolation):
        parse_director_plan(plan_dict)


def test_character_referenced_by_scene_ok(plan_dict):
    plan_dict["characters"] = [{"id": "hero", "name": "Hero", "description": "tall", "voice_role": "protagonist"}]
    plan_dict["scenes"][0]["character_ids"] = ["hero"]
    assert parse_director_plan(plan_dict).voice_roles_used() >= {"protagonist", "narrator"}


def test_channel_cross_checks(plan_dict, channel):
    check_plan_against_channel(parse_director_plan(plan_dict), channel)

    bad = copy.deepcopy(plan_dict)
    bad["scenes"][0]["lines"][0]["voice_role"] = "villain"  # role the channel does not define
    with pytest.raises(ContractViolation, match="voice roles"):
        check_plan_against_channel(parse_director_plan(bad), channel)

    bad = copy.deepcopy(plan_dict)
    bad["target_duration_s"] = bad["experiment"]["duration_target_s"] = 500
    with pytest.raises(ContractViolation, match="target_duration_s"):
        check_plan_against_channel(parse_director_plan(bad), channel)

    bad = copy.deepcopy(plan_dict)
    bad["experiment"]["experiment_id"] = "XX-0001"
    with pytest.raises(ContractViolation, match="experiment_id"):
        check_plan_against_channel(parse_director_plan(bad), channel)

    bad = copy.deepcopy(plan_dict)
    bad["channel"] = "finance"
    with pytest.raises(ContractViolation, match="channel"):
        check_plan_against_channel(parse_director_plan(bad), channel)


def test_published_json_schema_matches_code_and_accepts_valid_plan(plan_dict):
    committed = json.loads((REPO_ROOT / "schemas" / "director_plan.schema.json").read_text())
    assert committed == director_plan_json_schema(), "run: python -m factory schema --write"
    jsonschema.validate(plan_dict, committed)
    plan_dict["schema_version"] = "2.0"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(plan_dict, committed)
