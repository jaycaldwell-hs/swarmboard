"""Saved peer handles and fresh seeds select the same model-slot profiles."""
import pytest

from swarmboard.peer_models import PEER_MODELS
from swarmboard.personas import DEFAULT_AGENTS, peer_profile


def test_saved_roster_and_fresh_seeds_share_profiles_without_transport_fields():
    expected = {
        "hiro": "autonomy provocateur",
        "raven": "contrarian belief tester",
        "ng": "alliance broker and instigator",
        "benway": "surreal counterfactual tinkerer",
        "da5id": "continuity and memory skeptic",
        "yt": "tone and relational provocateur",
    }
    seeds = {agent["model"]: agent for agent in DEFAULT_AGENTS}
    assert set(seeds) == set(PEER_MODELS.values())
    for handle, role in expected.items():
        model = PEER_MODELS[handle]
        profile = peer_profile(model)
        assert set(profile) == {"role", "persona"}
        assert profile["role"] == role
        assert {key: seeds[model][key] for key in profile} == profile
        profile["persona"] = "A caller's local edit"
        assert peer_profile(model)["persona"] == seeds[model]["persona"]


@pytest.mark.parametrize("model", ["gpt-6-astra", "unrecognized-peer-model"])
def test_unknown_models_cannot_receive_a_peer_profile(model):
    with pytest.raises(ValueError, match="No peer profile"):
        peer_profile(model)
