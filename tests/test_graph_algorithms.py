from uuid import uuid4

import pytest

from lapse.agent.guard import templated
from lapse.graph import algorithms
from lapse.graph.db import graph
from lapse.policy.loader import load_policy_file
from scripts.load_fixture import load_persona


@pytest.fixture
def algo_graph(monkeypatch):
    g = graph(f"lapse:test:algo:{uuid4().hex}")
    policies, carriers = load_policy_file("fixtures/policy.yaml")
    load_persona(g, policies, carriers)
    monkeypatch.setattr(algorithms, "user_graph", lambda uid: g)
    try:
        yield g
    finally:
        g.delete()


@pytest.mark.db
def test_keystones_rank_mailbox_and_google_first(algo_graph):
    traced = []
    result = algorithms.keystones("test", 3, trace=lambda name, *_: traced.append(name))
    items = {item["id"]: item for item in result["keystones"]}
    assert [item["id"] for item in result["keystones"]] == ["email_primary", "google", "email_college"]
    assert items["email_primary"]["upstream"] == 4
    assert items["email_primary"]["chains"] == 20
    assert items["email_primary"]["downstream"] == 5
    assert items["google"]["downstream"] == 5
    assert "Notion · demo@example.com" in items["google"]["downstream_labels"]
    assert "algo_keystones" in traced


@pytest.mark.db
def test_shortest_path_crosses_three_hops(algo_graph):
    result = algorithms.shortest_path("test", "phone_old", "notion")
    assert result["outcome"] == "route"
    assert result["hops"] == 3
    assert [step["id"] for step in result["route"]] == ["phone_old", "google", "email_primary", "notion"]


@pytest.mark.db
def test_and_gated_account_has_no_single_route(algo_graph):
    assert algorithms.shortest_path("test", "phone_old", "dropbox") == {
        "outcome": "no_single_route",
        "from_id": "phone_old",
        "to_id": "dropbox",
    }


@pytest.mark.db
def test_unknown_node_is_rejected(algo_graph):
    with pytest.raises(KeyError):
        algorithms.shortest_path("test", "phone_old", "missing")


def test_algorithm_replies_never_call_a_missing_route_safe():
    route = {
        "outcome": "route",
        "algorithm": "SPpaths",
        "hops": 2,
        "route": [
            {"id": "phone_old", "label": "••4321 (Vi)"},
            {"id": "google", "label": "Google · demo@example.com"},
            {"id": "email_primary", "label": "demo@example.com"},
        ],
    }
    assert templated([route]) == (
        "The shortest way in takes 2 steps:\n\nyour Vi number (••4321) → "
        "<b>Google account</b> (demo@example.com) → <b>demo@example.com</b>\n\n"
        "That's the chain to break. Want me to plan the fix?"
    )
    reply = templated([{"outcome": "no_single_route", "from_id": "a", "to_id": "b"}])
    assert "can't promise it's safe" in reply
    keystones = {
        "outcome": "keystones",
        "algorithm": "betweenness",
        "keystones": [
            {
                "id": "google",
                "label": "Google · demo@example.com",
                "node_type": "Account",
                "score": 4.0,
                "chains": 1,
                "upstream": 1,
                "downstream": 1,
                "downstream_labels": ["Notion · demo@example.com"],
            },
        ],
    }
    assert "Start with <b>Google account</b> (demo@example.com). 1 account depends on it." in templated([keystones])
