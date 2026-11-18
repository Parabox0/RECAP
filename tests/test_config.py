import pytest
from helpers import profile, source

from recap.config import ConfigError, ModelEntry, ProfileManifest, RecapConfig, Settings, SourceConfig
from recap.timeutil import date_str, hour_bucket, iso, parse_time


@pytest.mark.parametrize(
    "over, message",
    [
        ({"url": "https://other.example.org/feed"}, "not in permitted_origins"),
        ({"url": "http://src.example.org/feed", "permitted_origins": ["http://src.example.org"]}, "allow_insecure_http"),
        ({"permitted_paths": ["/only/"]}, "not under permitted_paths"),
        ({"fact_types": ["weather"]}, "unknown fact type"),
        ({"authority": "blog"}, "authority"),
        ({"poll_interval_minutes": 1}, "at least 5"),
        ({"entities": ["no-namespace"]}, "bad entity"),
        ({"class": "magic"}, "class"),
    ],
)
def test_source_validation(over, message):
    with pytest.raises(ConfigError, match=message):
        SourceConfig.from_dict(source("src", **over))


def test_profile_validation():
    with pytest.raises(ConfigError, match="budget"):
        ProfileManifest.from_dict(profile(budget=300))
    with pytest.raises(ConfigError, match="weight"):
        ProfileManifest.from_dict(profile(topics={"a": 2}))
    with pytest.raises(ConfigError, match="max_editorial"):
        ProfileManifest.from_dict(profile(kind="editorial", max_editorial=3))
    p = ProfileManifest.from_dict(profile(topics={"b": 0.5, "a": 0.5, "c": 1.0}))
    assert p.top_topics(2) == ["c", "a"]


def test_prompt_bindings():
    profiles = [profile(prompt_versions=["sys@1"]), profile("geo", prompt_versions=["sys@1"])]
    with pytest.raises(ConfigError, match="bound to both"):
        RecapConfig.build("x.db", sources=[source("src")], profiles=profiles, models=[])
    cfg = RecapConfig.build(
        "x.db",
        sources=[source("src")],
        profiles=[profile(prompt_versions=["sys@1"]), profile(version=2, prompt_versions=["sys@1"])],
        models=[],
    )
    assert cfg.profile_for_prompt("sys@1").version == 2


def test_models_and_settings():
    with pytest.raises(ConfigError, match="calibration"):
        ModelEntry.from_dict({"id": "m", "counting": "calibrated"})
    with pytest.raises(ConfigError, match="unknown keys"):
        Settings.from_dict({"nonsense": 1})
    with pytest.raises(ConfigError, match="max_facts"):
        Settings.from_dict({"max_facts": 9})
    with pytest.raises(ConfigError, match="duplicate"):
        RecapConfig.build("x.db", sources=[source("a"), source("a")])


def test_time_parsing():
    t = parse_time("2026-11-18T10:00:00Z")
    assert parse_time("2026-11-18T11:00:00+01:00") == t
    assert parse_time("Wed, 18 Nov 2026 10:00:00 GMT") == t
    assert parse_time("2026-11-18") == t - 10 * 3600
    assert iso(t) == "2026-11-18T10:00:00Z" and date_str(t) == "2026-11-18"
    assert hour_bucket(t + 1799) == t
    assert parse_time(None) is None
    with pytest.raises(ValueError):
        parse_time("yesterday-ish")
