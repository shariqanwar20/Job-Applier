import pytest
import yaml

from jobapplier.apply.profile import ProfileError, load_profile, profile_values
from tests.conftest import ROOT

EXAMPLE = ROOT / "profile.example.yaml"


def test_the_example_has_every_key_the_filler_reads():
    p = load_profile(EXAMPLE)
    assert set(p) >= {
        "name",
        "email",
        "phone",
        "location",
        "links",
        "work_authorization",
        "start_date",
        "relocation",
        "salary_expectation",
        "salary_default",
        "voluntary_self_id",
    }
    assert set(p["name"]) == {"first", "last"} and {"city", "province", "country"} <= set(p["location"])
    assert set(p["work_authorization"]) == {"canada", "sponsorship_now", "sponsorship_future"}
    assert set(p["relocation"]) == {"willing", "cities"} and {"linkedin", "github"} <= set(p["links"])


def test_a_missing_profile_names_the_example_file(tmp_path):
    with pytest.raises(ProfileError, match=r"profile\.example\.yaml"):
        load_profile(tmp_path / "profile.yaml")


def test_a_profile_that_is_not_a_mapping_is_an_error(tmp_path):
    (tmp_path / "profile.yaml").write_text("- just a list\n")
    with pytest.raises(ProfileError, match="mapping"):
        load_profile(tmp_path / "profile.yaml")


def test_values_flatten_to_dotted_keys_with_yes_no_and_derived_names(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": {"first": "Alex", "last": "Example"},
                "location": {"city": "Toronto", "province": "ON", "country": "Canada"},
                "work_authorization": {"canada": True, "sponsorship_now": False},
                "relocation": {"willing": True, "cities": ["Vancouver", "Montreal"]},
                "salary_default": 85000,
                "email": None,
            }
        )
    )
    v = profile_values(load_profile(path))
    assert v["name.first"] == "Alex" and v["name.full"] == "Alex Example"
    assert v["location.full"] == "Toronto, ON"
    assert v["work_authorization.canada"] == "Yes" and v["work_authorization.sponsorship_now"] == "No"
    assert v["relocation.cities"] == "Vancouver, Montreal" and v["salary_default"] == "85000"
    assert v["email"] == "" and "phone" not in v
