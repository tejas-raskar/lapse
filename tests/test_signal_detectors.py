import sys
from datetime import UTC, date, datetime, timedelta
from types import ModuleType

import httpx
import pytest

from lapse.contracts import ChangeProcedure, MethodTemplate, Service, ServicePolicy
from lapse.graph import db, planner, scenarios
from lapse.signals import detectors

NOW = datetime(2026, 10, 17, tzinfo=UTC)


@pytest.fixture
def detector_graph(isolated_graph, monkeypatch):
    monkeypatch.setattr(db, "user_graph", lambda uid: isolated_graph)
    monkeypatch.setattr(scenarios, "user_graph", lambda uid: isolated_graph)
    module = ModuleType("lapse.notify")
    calls = []
    module.notify = lambda uid, text, buttons: calls.append((uid, text, buttons))
    monkeypatch.setitem(sys.modules, "lapse.notify", module)
    isolated_graph.query("CREATE (:User {uid: 'detector-test', synthetic: true})")
    return isolated_graph, calls


def root(g, *, id="phone", kind="phone", **props):
    props = {"id": id, "kind": kind, "label": id, "status": "active", **props}
    g.query(f"CREATE (r:Root:{kind.title()}) SET r = $props", {"props": props})


def method(g, id, target, need=None, password=False):
    g.query(
        "MATCH (a {id: $target}) CREATE (m:Method {id: $id, label: $id, needs_password: $password}) "
        "CREATE (m)-[:GRANTS]->(a)",
        {"id": id, "target": target, "password": password},
    )
    if need:
        g.query(
            "MATCH (m:Method {id: $id}), (r {id: $need}) CREATE (m)-[:NEEDS]->(r)",
            {"id": id, "need": need},
        )


@pytest.mark.db
def test_reissue_uses_full_scenario_and_notifies_once(detector_graph):
    g, calls = detector_graph
    root(g, status="retiring", last4="4321", reissue_after="2027-01-01")
    g.query(
        "CREATE (:Account {id: 'direct', display: 'Direct'}), (:Account {id: 'indirect', display: 'Indirect'})"
    )
    method(g, "direct_method", "direct", "phone")
    method(g, "indirect_method", "indirect", "direct")
    records = []
    first = detectors.run_detectors("test", now=NOW, trace=lambda *args: records.append(args))
    second = detectors.run_detectors("test", now=NOW + timedelta(days=1))
    assert len(first) == len(second) == len(calls) == 1
    assert first[0].id == second[0].id
    assert first[0].at == second[0].at == NOW
    assert first[0].detail == (
        "Your old number (••4321) could go to someone new from <b>Jan 1</b> (in 76 days).\n\n"
        "They could use it to get into <b>Direct</b> and <b>Indirect</b>.\n\n"
        "Want me to check what needs changing?"
    )
    assert [button["action"] for button in calls[0][2]] == ["open_web", "plan_root", "dismiss_signal"]
    assert g.ro_query("MATCH (:Signal)-[:ABOUT]->(:Root) RETURN count(*)").result_set == [[1]]
    assert {entry[0] for entry in records} >= {"closure_round", "signals_detector_merge"}
    g.query("MATCH (s:Signal) SET s.status = 'dismissed'")
    assert detectors.run_detectors("test", now=NOW)[0].status == "dismissed"
    assert len(calls) == 1
    g.query("MATCH (r:Root) SET r.reissue_after = '2027-02-01'")
    assert detectors.run_detectors("test", now=NOW)[0].deadline == date(2027, 2, 1)
    assert len(calls) == 2


@pytest.mark.db
def test_closing_email_adopts_ingested_signal_and_uses_lockout(detector_graph):
    g, calls = detector_graph
    root(
        g, id="email", kind="email", address="student@example.test", status="closing", closes_at="2026-12-01"
    )
    g.query("CREATE (:Account {id: 'host', display: 'Host'}), (:Account {id: 'app', display: 'App'})")
    method(g, "host_login", "host", password=True)
    method(g, "mailbox", "email", "host")
    g.query("MATCH (m:Method {id: 'mailbox'}) SET m.kind = 'mailbox'")
    method(g, "app_login", "app", "email")
    g.query("CREATE (:Account {id: 'password-app', display: 'Password app'})")
    method(g, "password_app_login", "password-app", password=True)
    method(g, "password_app_recovery", "password-app", "email")
    g.query(
        "CREATE (:Signal {id: 'ingest-existing', kind: 'closing_email', root_id: 'email', "
        "deadline: '2026-12-01', at: $at, status: 'open', title: 'Closure', detail: 'ingest'})",
        {"at": NOW.isoformat()},
    )
    result = detectors.run_detectors("test", now=NOW)
    assert result[0].id == "ingest-existing"
    assert "Your email (student@example.test) shuts down on <b>Dec 1</b> (in 45 days)." in result[0].detail
    assert "You'll lose access to <b>Host</b> and <b>App</b>" in result[0].detail
    assert "if you forget your password" in result[0].detail
    assert "<b>Password app</b>" in result[0].detail
    assert len(calls) == 1
    assert g.ro_query("MATCH (s:Signal) RETURN count(s)").result_set == [[1]]


@pytest.mark.db
@pytest.mark.parametrize(
    ("status", "age", "expected"),
    [
        ("inactive", 30, True),
        ("inactive", 31, False),
        ("active", 180, True),
        ("active", 179, False),
        ("active", -1, False),
    ],
)
def test_stale_root_time_boundaries(detector_graph, status, age, expected):
    g, calls = detector_graph
    root(g, status=status, label="••4321")
    g.query(
        "MATCH (r:Root) CREATE (e:Evidence {id: 'observed', date: $date})-[:MENTIONS]->(r)",
        {"date": (NOW - timedelta(days=age)).isoformat()},
    )
    result = detectors.run_detectors("test", now=NOW)
    assert bool(result) == expected
    assert bool(calls) == expected
    if expected:
        assert result[0].kind == "stale_root"
        assert "Do you still have ••4321?" in result[0].detail


def rdap_client(document, *, status=200):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json=document)

    return httpx.Client(transport=httpx.MockTransport(handle)), calls


def test_rdap_parses_expiry_with_timeout_and_ignores_other_events():
    client, calls = rdap_client(
        {
            "events": [
                {"eventAction": "registration", "eventDate": "2020-01-01T00:00:00Z"},
                {"eventAction": "expiration", "eventDate": "invalid"},
                {"eventAction": "expiration", "eventDate": "2027-05-01T00:00:00Z"},
                {"eventAction": "expiration", "eventDate": "2027-04-01T00:00:00Z"},
            ]
        }
    )
    with client:
        assert detectors.rdap_expiry("example.com", client=client) == date(2027, 4, 1)
    assert str(calls[0].url) == "https://rdap.org/domain/example.com"
    assert calls[0].extensions["timeout"]["read"] == 5.0


@pytest.mark.parametrize(
    "document", [[], {}, {"events": {}}, {"events": [None, {"eventAction": "expiration"}]}]
)
def test_rdap_missing_or_malformed(document):
    client, _ = rdap_client(document)
    with client:
        assert detectors.rdap_expiry("example.com", client=client) is None


def test_rdap_network_failure():
    def fail(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        assert detectors.rdap_expiry("example.com", client=client) is None


@pytest.mark.db
def test_domain_expiry_updates_email_and_deduplicates(detector_graph):
    g, calls = detector_graph
    root(g, id="email", kind="email", email_kind="custom", address="user@mail.example.co.uk")
    client, requests = rdap_client(
        {"events": [{"eventAction": "expiration", "eventDate": "2027-05-01T00:00:00Z"}]}
    )
    with client:
        result = detectors.run_detectors("test", now=NOW, rdap_client=client)
        detectors.run_detectors("test", now=NOW, rdap_client=client)
    assert result[0].kind == "domain_expiry"
    assert result[0].deadline == date(2027, 5, 1)
    assert str(requests[0].url) == "https://rdap.org/domain/example.co.uk"
    assert g.ro_query("MATCH (r:Root) RETURN r.expires_at").result_set == [["2027-05-01"]]
    assert len(calls) == 1


@pytest.mark.db
def test_new_device_offers_old_authenticator_lockout(detector_graph):
    g, calls = detector_graph
    root(g, id="old", kind="device", label="Old phone")
    root(g, id="new", kind="device", label="New phone")
    g.query(
        "MATCH (old:Root {id: 'old'}), (new:Root {id: 'new'}) "
        "CREATE (a:Account {id: 'app', display: 'App'})-[:USES {role: 'totp_device'}]->(old), "
        "(e:Evidence {id: 'device', event: 'new_device', date: $date})-[:SUPPORTS]->(a), "
        "(e)-[:MENTIONS]->(new)",
        {"date": NOW.isoformat()},
    )
    method(g, "login", "app", "old", password=True)
    result = detectors.run_detectors("test", now=NOW)
    assert len(result) == len(calls) == 1
    assert result[0].kind == "new_device"
    assert result[0].root_id == "old"
    assert "Did you move your authenticator app from Old phone?" in result[0].detail
    assert "lose access to <b>App</b>" in result[0].detail
    scenario_id = calls[0][2][0]["payload"]["path"].split("=")[1]
    scenario = scenarios.get_scenario("test", scenario_id)
    assert scenario.kind == "lockout"
    assert scenario.root_ids == ["old"]


@pytest.mark.db
@pytest.mark.parametrize("delivery", ["available", "missing", "failed"])
def test_missed_account_creates_followup_once_and_includes_later_accounts(
    detector_graph, monkeypatch, delivery
):
    g, calls = detector_graph
    root(g, id="old", status="inactive")
    root(g, id="new")
    policy = ServicePolicy(
        service=Service(id="test", name="Test"),
        methods=[MethodTemplate(id="phone", kind="login", label="Phone", needs_roles=["login_phone"])],
        change=[ChangeProcedure(root_kind="phone", tier=3)],
    )
    monkeypatch.setattr(planner, "_policies", lambda: [policy])
    monkeypatch.setattr(detectors.loader, "default_policy_paths", lambda: ["unused"])
    monkeypatch.setattr(detectors.loader, "load_policy_file", lambda _: ([policy], []))
    g.query(
        "CREATE (p:Plan {id: 'original', old_root: 'old', new_root: 'new', created_at: '2026-01-01T00:00:00+00:00'}) "
        "CREATE (done:Change {id: 'completed', from_root: 'old', status: 'done'}) "
        "CREATE (p)-[:HAS_STEP]->(done)"
    )

    def missed(account):
        g.query(
            "MATCH (r:Root {id: 'old'}) "
            "CREATE (a:Account {id: $id, service_id: 'test', display: $id})-[:USES {role: 'login_phone'}]->(r), "
            "(e:Evidence {id: $id, date: $date})-[:MENTIONS]->(r), (e)-[:SUPPORTS]->(a)",
            {"id": account, "date": NOW.isoformat()},
        )
        method(g, "method:" + account, account, "old")

    missed("one")
    module = sys.modules["lapse.notify"]
    notify = module.notify
    if delivery == "missing":
        monkeypatch.setitem(sys.modules, "lapse.notify", None)
    elif delivery == "failed":

        def fail_missed(uid, text, buttons):
            if "your list of fixes" in text:
                raise RuntimeError("missed notification failed")
            notify(uid, text, buttons)

        monkeypatch.setattr(module, "notify", fail_missed)
    if delivery == "failed":
        with pytest.raises(RuntimeError, match="missed notification failed"):
            detectors.run_detectors("test", now=NOW)
    else:
        result = detectors.run_detectors("test", now=NOW)
        signal = next(item for item in result if item.kind == "missed_account")
        assert "I added 1 account to your list of fixes." in signal.detail
    monkeypatch.setitem(sys.modules, "lapse.notify", module)
    monkeypatch.setattr(module, "notify", notify)
    assert g.ro_query("MATCH (p:Plan) RETURN count(p)").result_set == [[2]]
    assert g.ro_query("MATCH (c:Change {account_id: 'one'}) RETURN c.to_root").result_set == [["new"]]
    detectors.run_detectors("test", now=NOW)
    assert g.ro_query("MATCH (p:Plan) RETURN count(p)").result_set == [[2]]
    assert len([text for _, text, _ in calls if "your list of fixes" in text]) == 1
    missed("two")
    detectors.run_detectors("test", now=NOW)
    assert g.ro_query("MATCH (p:Plan) RETURN count(p)").result_set == [[3]]
    assert g.ro_query("MATCH (c:Change {account_id: 'two'}) RETURN c.to_root").result_set == [["new"]]
    assert len([text for _, text, _ in calls if "your list of fixes" in text]) == 1


@pytest.mark.db
def test_unavailable_notification_is_retried(detector_graph, monkeypatch, caplog):
    g, calls = detector_graph
    root(g, status="retiring", last4="4321", reissue_after="2027-01-01")
    module = sys.modules["lapse.notify"]
    monkeypatch.setitem(sys.modules, "lapse.notify", None)
    signals = detectors.run_detectors("test", now=NOW)
    assert len(signals) == 1
    assert not calls
    assert "signal delivery remains pending" in caplog.text
    monkeypatch.setitem(sys.modules, "lapse.notify", module)
    retried = detectors.run_detectors("test", now=NOW)
    assert retried[0].id == signals[0].id
    assert len(calls) == 1


@pytest.mark.db
def test_failed_notification_does_not_mark_delivered(detector_graph, monkeypatch):
    g, calls = detector_graph
    root(g, status="retiring", last4="4321", reissue_after="2027-01-01")
    module = sys.modules["lapse.notify"]
    notify = module.notify

    def fail(*args):
        raise RuntimeError("notification unavailable")

    monkeypatch.setattr(module, "notify", fail)
    with pytest.raises(RuntimeError, match="notification unavailable"):
        detectors.run_detectors("test", now=NOW)
    monkeypatch.setattr(module, "notify", notify)
    detectors.run_detectors("test", now=NOW)
    assert len(calls) == 1
    assert g.ro_query("MATCH (s:Signal) RETURN count(s)").result_set == [[1]]


@pytest.mark.db
@pytest.mark.parametrize("has_use", [False, True])
def test_missed_account_without_modelled_change_does_not_create_empty_plans(detector_graph, has_use):
    g, calls = detector_graph
    root(g, id="old", status="inactive")
    g.query(
        "MATCH (r:Root {id: 'old'}) "
        "CREATE (p:Plan {id: 'completed', old_root: 'old'})-[:HAS_STEP]->"
        "(:Change {id: 'done', from_root: 'old', status: 'done'}), "
        "(e:Evidence {id: 'new', date: $at})-[:MENTIONS]->(r), "
        "(e)-[:SUPPORTS]->(:Account {id: 'unknown', display: 'Unknown', service_id: 'unknown', modelled: false})",
        {"at": NOW.isoformat()},
    )
    if has_use:
        g.query(
            "MATCH (r:Root {id: 'old'}), (a:Account {id: 'unknown'}) "
            "CREATE (a)-[:USES {role: 'login_phone'}]->(r)"
        )
    first = detectors.run_detectors("test", now=NOW)
    second = detectors.run_detectors("test", now=NOW)
    assert [item.id for item in first] == [item.id for item in second]
    assert g.ro_query("MATCH (p:Plan) RETURN count(p)").result_set == [[1]]
    missed = next(item for item in first if item.kind == "missed_account")
    assert "check 1 account yourself" in missed.detail
    assert "your list of fixes" not in missed.detail
    assert len([text for _, text, _ in calls if "check 1 account yourself" in text]) == 1


@pytest.mark.db
@pytest.mark.parametrize("dismissed_node", [None, "managed", "backfill"])
def test_older_ingest_backfill_consolidates_signal_tuple(detector_graph, dismissed_node):
    g, calls = detector_graph
    root(
        g, id="email", kind="email", address="student@example.test", status="closing", closes_at="2026-12-01"
    )
    first = detectors.run_detectors("test", now=NOW)[0]
    g.query(
        "MATCH (r:Root {id: 'email'}) "
        "CREATE (s:Signal {id: 'older-ingest', kind: 'closing_email', root_id: 'email', "
        "deadline: '2026-12-01', at: '2026-01-01T00:00:00+00:00', status: 'open', "
        "title: 'Ingested closure', detail: 'service'})-[:ABOUT]->(r)"
    )
    if dismissed_node:
        signal_id = first.id if dismissed_node == "managed" else "older-ingest"
        g.query("MATCH (s:Signal {id: $id}) SET s.status = 'dismissed'", {"id": signal_id})
    result = detectors.run_detectors("test", now=NOW + timedelta(days=1))
    assert len(result) == 1
    assert result[0].id == first.id
    assert result[0].status == ("dismissed" if dismissed_node else "open")
    assert len(calls) == 1
    assert g.ro_query("MATCH (s:Signal) RETURN count(s)").result_set == [[1]]
    assert g.ro_query("MATCH (:Signal)-[:ABOUT]->(:Root) RETURN count(*)").result_set == [[1]]
    assert g.ro_query("MATCH (s:Signal) RETURN s.detector_notified").result_set == [[True]]


@pytest.mark.db
def test_pending_signal_consolidates_backfill_state_before_delivery(detector_graph, monkeypatch):
    g, calls = detector_graph
    root(g, status="retiring", reissue_after="2027-01-01")
    module = sys.modules["lapse.notify"]
    monkeypatch.setitem(sys.modules, "lapse.notify", None)
    first = detectors.run_detectors("test", now=NOW)[0]
    monkeypatch.setitem(sys.modules, "lapse.notify", module)
    g.query(
        "CREATE (:Signal {id: 'older-ingest', kind: 'reissue_countdown', root_id: 'phone', "
        "deadline: '2027-01-01', at: '2026-01-01T00:00:00+00:00', status: 'acted', "
        "title: 'Older', detail: 'Older', detector_notified: true})"
    )
    result = detectors.run_detectors("test", now=NOW)
    assert result[0].id == first.id
    assert result[0].status == "acted"
    assert not calls
    assert g.ro_query("MATCH (s:Signal) RETURN count(s), s.detector_notified").result_set == [[1, True]]


@pytest.mark.db
@pytest.mark.parametrize("count", [1, 2])
def test_unknown_alerts_are_honest_and_pluralized(detector_graph, count):
    g, _ = detector_graph
    root(g, status="retiring", last4="4321", reissue_after="2026-10-18")
    for index in range(count):
        g.query(
            "MATCH (r:Root) CREATE (:Account {id: $id, display: 'Unknown site', modelled: false})"
            "-[:USES {role: 'login_phone'}]->(r)",
            {"id": f"unknown-{index}"},
        )
    text = detectors.run_detectors("test", now=NOW)[0].detail
    assert "<b>Oct 18</b> (in 1 day)" in text
    expected = (
        "I don't know how another site lets you back in, so I can't promise it's safe."
        if count == 1
        else "I don't know how 2 other sites let you back in, so I can't promise they're safe."
    )
    assert expected in text
    assert "unknown-" not in text
    assert "Under the methods" not in text
