from datetime import UTC, date, datetime

import pytest

from lapse.agent import guard
from lapse.bot.cards import account_risk_card, format_date, plan_card, scenario_card
from lapse.contracts import Change, Plan, Reached, Root, ScenarioResult


def test_incomplete_risk_card_escapes_root_labels():
    root = Root(id="closing", kind="email", label="<inbox>", address="<demo@example.edu>")
    text = account_risk_card({"id": "account", "display": "Canva"}, [], incomplete=[root])
    assert "I still need to check those" in text
    assert "&lt;demo@example.edu&gt;" in text
    assert "<demo@example.edu>" not in text
    assert "don't put" not in text


@pytest.mark.parametrize(
    "day, expected",
    [
        (17, "Oct 17 (1 day ago)"),
        (18, "Oct 18 (today)"),
        (19, "Oct 19 (in 1 day)"),
        (31, "Oct 31 (in 13 days)"),
    ],
)
def test_dates_use_short_months_and_correct_plural(day, expected):
    assert format_date(date(2026, 10, day), today=date(2026, 10, 18)) == expected


def test_old_number_card_names_direct_and_indirect_accounts():
    root = Root(
        id="old",
        kind="phone",
        label="••9810 (Vi)",
        last4="9810",
        status="retiring",
        drop_at=date(2026, 10, 31),
        reissue_after=date(2027, 1, 29),
    )
    result = ScenarioResult(
        id="scenario",
        kind="takeover",
        root_ids=["old"],
        actor="stranger",
        rounds=3,
        affected_accounts=[
            Reached(node_id="google", label="Google · demo@example.com", node_type="Account", round=1),
            Reached(node_id="figma", label="Figma", node_type="Account", round=3),
        ],
        direct_accounts=["google"],
        unknown_accounts=[],
        total_accounts=2,
        ms=0,
    )
    text = scenario_card(root, result, today=date(2026, 10, 18))
    assert text == (
        "Heads up 👀 You're giving up your old Vi number (••9810) on <b>Oct 31</b> (in 13 days). "
        "Carriers can hand it to someone new after about 90 days.\n\n"
        "Whoever gets it could get into your <b>Google account</b> (demo@example.com). "
        "Through that account, they could get into <b>Figma</b> too.\n\nWant me to fix it?"
    )


def test_college_only_host_closes_and_completed_move_is_grounded():
    root = Root(
        id="college",
        kind="email",
        label="demo@example.edu",
        address="demo@example.edu",
        email_kind="edu",
        hosted_by_service="google",
        status="closing",
        closes_at=date(2026, 11, 15),
    )
    host = Reached(node_id="college_google", label="Google · demo@example.edu", node_type="Account", round=1)
    result = ScenarioResult(
        id="scenario",
        kind="lockout",
        root_ids=["college"],
        actor="user",
        rounds=1,
        affected_accounts=[host],
        lost_now_accounts=[host],
        recovery_lost_accounts=[host.model_copy(update={"via_method": "reset_method"})],
        direct_accounts=["college_google"],
        unknown_accounts=[],
        total_accounts=2,
        ms=0,
    )
    moved = {
        "display": "Notion",
        "identifier": "demo@example.com",
        "previous_identifiers": ["demo@example.edu"],
    }
    text = scenario_card(root, result, accounts=[moved], today=date(2026, 10, 18))
    assert text == (
        "Your college email (demo@example.edu) shuts down on <b>Nov 15</b> (in 28 days). "
        "The <b>Google account</b> that comes with it goes too, so save anything you need from it.\n\n"
        "Good news: nothing else I've checked depends on it, because you moved <b>Notion</b> already ✅"
    )
    assert "Notion" not in scenario_card(root, result)


def test_plan_names_roles_target_deadline_without_internal_ids():
    roots = [
        Root(id="old", kind="phone", label="••9810 (Vi)", last4="9810", status="retiring"),
        Root(id="new", kind="phone", label="New", last4="6029"),
    ]
    plan = Plan(
        id="plan:private",
        kind="takeover",
        old_root="old",
        new_root="new",
        deadline=date(2026, 10, 31),
        feasible=True,
        exposure_before=3,
        exposure_after=0,
        created_at=datetime.now(UTC),
        steps=[
            Change(
                id="change:private",
                plan_id="plan:private",
                order=1,
                account_id="google",
                account_label="Google",
                role="recovery_phone",
                roles=["recovery_phone", "sms_2fa"],
                from_root="old",
                to_root="new",
                tier=3,
                requires_old_root=False,
                due=date(2026, 10, 30),
                settings_url="https://example.com/settings",
            )
        ],
    )
    text = plan_card(plan, roots=roots, today=date(2026, 10, 18))
    assert "Just one step:" in text
    assert "Change the recovery phone and 2-Step phone for your <b>Google account</b> to ••6029." in text
    assert "Do it before <b>Oct 30</b> (in 12 days)." in text
    assert not any(internal in text for internal in ["private", "proposed", "T3", "takeover", "lockout"])
    assert guard.grounded(
        text, [plan.model_dump(mode="json"), [root.model_dump(mode="json") for root in roots]]
    )


def test_service_using_college_address_is_not_described_as_shutting_down():
    root = Root(
        id="college",
        kind="email",
        label="demo@example.edu",
        address="demo@example.edu",
        email_kind="edu",
        hosted_by_service="google",
        status="closing",
    )
    notion = Reached(node_id="notion", label="Notion · demo@example.edu", node_type="Account", round=1)
    result = ScenarioResult(
        id="scenario",
        kind="lockout",
        root_ids=["college"],
        actor="user",
        rounds=1,
        affected_accounts=[notion],
        lost_now_accounts=[notion],
        recovery_lost_accounts=[],
        direct_accounts=["notion"],
        unknown_accounts=[],
        total_accounts=1,
        ms=0,
    )
    text = scenario_card(root, result)
    assert "You'd lose access to <b>Notion</b> (demo@example.edu)." in text
    assert "that comes with it goes too" not in text


def test_lockout_of_kept_email_is_hypothetical():
    root = Root(id="primary", kind="email", label="me@example.com", address="me@example.com")
    result = ScenarioResult(
        id="s",
        kind="lockout",
        root_ids=["primary"],
        actor="user",
        rounds=0,
        affected_accounts=[],
        lost_now_accounts=[],
        recovery_lost_accounts=[],
        direct_accounts=[],
        unknown_accounts=[],
        total_accounts=0,
        ms=0,
    )
    text = scenario_card(root, result)
    assert text.startswith("If you lost your email (me@example.com):")
    assert "shuts down" not in text
