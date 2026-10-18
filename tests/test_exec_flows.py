import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from lapse.contracts import (
    Change,
    ChangeProcedure,
    EventKind,
    Evidence,
    Root,
    Service,
    ServicePolicy,
    UseRole,
)
from lapse.exec import base, jobs, t4_offline, verify
from lapse.exec.base import ExecContext, StepEmitter
from lapse.exec.recipes.dsl import BrowserExecutor
from lapse.exec.t3_assisted import AssistedExecutor
from lapse.exec.t4_offline import OfflineExecutor
from lapse.ingest.imap import parse_email

START = datetime(2026, 10, 17, 11, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "emails"


def context(*, tier=3, status="approved", role="login_email", change_id="change-1"):
    old = Root(id="email_college", kind="email", label="demo@example.edu", address="demo@example.edu")
    new = Root(id="email_primary", kind="email", label="demo@example.com", address="demo@example.com")
    change = Change(
        id=change_id,
        plan_id="plan-1",
        order=0,
        account_id="account-notion",
        account_label="Notion",
        role=role,
        from_root=old.id,
        to_root=new.id,
        tier=tier,
        requires_old_root=False,
        status=status,
    )
    procedure = ChangeProcedure(
        root_kind="email",
        tier=tier,
        settings_url="https://www.notion.so/settings",
        doc_url="https://www.notion.com/help/account-settings",
        recipe="notion_change_email",
        confirmation_patterns=[r"(?i)email address changed"],
    )
    return ExecContext(
        "demo",
        change,
        procedure,
        Service(id="notion", name="Notion", domains=["notion.so"]),
        old,
        new,
        START,
        StepEmitter("demo", change.id),
    )


class FakeRedis:
    def __init__(self):
        self.members = {}

    def zadd(self, key, mapping):
        assert key == t4_offline.REMINDERS
        self.members.update(mapping)

    def zrangebyscore(self, key, low, high):
        assert key == t4_offline.REMINDERS and low == "-inf"
        return [member for member, score in self.members.items() if score <= high]

    def zrem(self, key, member):
        assert key == t4_offline.REMINDERS
        self.members.pop(member)


@pytest.fixture
def execution(monkeypatch):
    state = SimpleNamespace(
        rows={},
        policies={},
        pending=[],
        queries=[],
        updates=[],
        notifications=[],
        events=[],
        applied=[],
        claimable=True,
        redis=FakeRedis(),
    )
    events = ModuleType("lapse.events")
    events.publish = lambda uid, kind, data: state.events.append((uid, kind, data))
    notifications = ModuleType("lapse.notify")
    notifications.notify = lambda uid, text, buttons=None: state.notifications.append((uid, text, buttons))
    monkeypatch.setitem(sys.modules, "lapse.events", events)
    monkeypatch.setitem(sys.modules, "lapse.notify", notifications)
    monkeypatch.setattr(base.db, "user_graph", lambda uid: f"test-graph:{uid}")

    def add(ctx):
        state.rows[ctx.change.id] = {
            "change": ctx.change.model_dump(mode="json"),
            "account": {"id": ctx.change.account_id, "service_id": ctx.service.id},
            "old_root": ctx.old_root.model_dump(mode="json"),
            "new_root": ctx.new_root.model_dump(mode="json") if ctx.new_root else None,
        }
        state.policies[ctx.service.id] = ServicePolicy(service=ctx.service, change=[ctx.procedure])
        return state.rows[ctx.change.id]

    def run(graph, query, params=None, *, trace=None, write=False):
        assert graph == "test-graph:demo"
        state.queries.append((query, params, write, trace))
        params = params or {}
        row = state.rows.get(params.get("change_id"))
        if query == "exec_pending":
            return SimpleNamespace(rows=state.pending)
        if query in {"exec_context", "planner_get_change"}:
            return SimpleNamespace(rows=[row] if row else [])
        if query == "exec_start":
            assert write
            if not state.claimable or row["change"]["status"] != "approved":
                return SimpleNamespace(rows=[])
            row["change"].update(status="running", started_at=params["started_at"], exec_active=True)
            return SimpleNamespace(rows=[row])
        if query == "exec_release":
            assert write
            row["change"]["exec_active"] = False
            return SimpleNamespace(rows=[])
        pytest.fail(f"Unexpected graph query: {query}")

    def update(uid, change_id, *, status, note=None, trace=None):
        assert uid == "demo"
        state.updates.append((change_id, status, note))
        state.rows[change_id]["change"].update(status=status.value, note=note)

    def apply(uid, change_id, evidence_id=None, *, trace=None):
        state.applied.append((uid, change_id, evidence_id, trace))
        state.rows[change_id]["change"]["status"] = "done"

    state.add = add
    monkeypatch.setattr(base.db, "run", run)
    monkeypatch.setattr(base, "update_change", update)
    monkeypatch.setattr(jobs, "apply_change_done", apply)
    monkeypatch.setattr(verify, "apply_change_done", apply)
    monkeypatch.setattr(jobs, "load_policies", lambda: state.policies)
    monkeypatch.setattr(verify, "load_policies", lambda: state.policies)
    monkeypatch.setattr(t4_offline, "redis_client", lambda: state.redis)
    return state


async def test_assisted_waits_for_user_with_exact_settings_link(execution):
    ctx = context()
    row = execution.add(ctx)
    assert await AssistedExecutor().run(ctx) is False
    assert row["change"]["status"] == "awaiting_user"
    uid, message, buttons = execution.notifications[0]
    assert uid == "demo"
    assert "demo@example.edu" in message and "demo@example.com" in message
    assert buttons == [
        {
            "label": "Open the link",
            "action": "open_url",
            "payload": {"url": "https://www.notion.so/settings"},
        },
        {"label": "I did it", "action": "check_change", "payload": {"change_id": ctx.change.id}},
    ]
    assert execution.events[0][2]["kind"] == "need_user"
    assert execution.applied == []


@pytest.mark.parametrize("status", ["done", "skipped", "failed"])
def test_step_emitter_does_not_reopen_terminal_change(execution, status):
    ctx = context(status=status)
    row = execution.add(ctx)
    ctx.emitter.emit("progress", "Late browser progress")
    ctx.emitter.emit("need_user", "Late manual fallback", url="https://www.notion.so/settings")
    assert row["change"]["status"] == status
    assert execution.updates == execution.events == execution.notifications == []


def test_step_emitter_requests_otp_without_a_code(execution):
    ctx = context()
    row = execution.add(ctx)
    ctx.emitter.emit("need_otp", "Enter the SMS code in Telegram.", channel="sms:••7788")
    assert row["change"]["status"] == "awaiting_otp"
    assert execution.events[0][2]["channel"] == "sms:••7788"
    assert execution.notifications == [("demo", "Enter the SMS code in Telegram.", [])]


async def test_offline_drafts_checklist_and_schedules_deadline_reminder(execution, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return START

    monkeypatch.setattr(t4_offline, "datetime", Clock)
    ctx = context(tier=4)
    ctx.change.due = START.date() + timedelta(days=1)
    row = execution.add(ctx)
    assert await OfflineExecutor().run(ctx) is False
    assert row["change"]["status"] == "awaiting_user"
    message = execution.notifications[0][1]
    assert "handle this with <b>Notion</b> directly" in message
    assert "You can say: Please change my email" in message
    assert "from demo@example.edu to demo@example.com" in message
    assert "Add your account number, name and signature" in message
    due = datetime(2026, 10, 18, 9, tzinfo=UTC).timestamp()
    assert execution.redis.members == {json.dumps(["demo", "change-1"]): due}
    assert execution.applied == []


def test_reminders_send_only_for_pending_changes(execution):
    for status in ["awaiting_user", "done", "failed", "skipped"]:
        execution.add(context(tier=4, status=status, change_id=status))
        execution.redis.members[json.dumps(["demo", status])] = 0
    execution.redis.members[json.dumps(["demo", "missing"])] = 0
    before = datetime.now(UTC)
    assert t4_offline.send_due_reminders() == 1
    assert execution.notifications == [
        ("demo", "A quick reminder to update your details with <b>Notion</b>.", None)
    ]
    assert list(execution.redis.members) == [json.dumps(["demo", "awaiting_user"])]
    assert next(iter(execution.redis.members.values())) >= (before + timedelta(days=1)).timestamp()


@pytest.mark.parametrize("tier", [1, 2, 3, 4])
async def test_job_dispatches_by_tier(execution, monkeypatch, tier):
    ctx = context(tier=tier)
    row = execution.add(ctx)
    browser_calls = []

    async def browser(self, ctx):
        browser_calls.append((self.recipe.name, ctx.change.id))
        return False

    monkeypatch.setattr(BrowserExecutor, "run", browser)
    result = await jobs.handle_execute_change("demo", {"change_id": "change-1"})
    assert result == {"change_id": "change-1", "status": "running" if tier < 3 else "awaiting_user"}
    assert browser_calls == ([("notion_change_email", "change-1")] if tier < 3 else [])
    assert bool(execution.redis.members) == (tier == 4)
    assert row["change"]["exec_active"] is False
    assert [query for query, *_ in execution.queries].count("exec_start") == 1
    assert [query for query, *_ in execution.queries].count("exec_release") == 1


@pytest.mark.parametrize(
    "status", ["proposed", "running", "awaiting_otp", "awaiting_user", "done", "failed", "skipped"]
)
async def test_job_requires_approval(execution, status):
    execution.add(context(tier=1, status=status))
    assert await jobs.handle_execute_change("demo", {"change_id": "change-1"}) == {
        "change_id": "change-1",
        "status": status,
    }
    assert [query for query, *_ in execution.queries] == ["exec_context"]
    assert execution.events == execution.notifications == execution.applied == []


async def test_job_does_not_execute_an_already_claimed_change(execution):
    execution.add(context(tier=1))
    execution.claimable = False
    assert await jobs.handle_execute_change("demo", {"change_id": "change-1"}) == {
        "change_id": "change-1",
        "status": "already_claimed",
    }
    assert execution.updates == execution.events == execution.notifications == []


@pytest.mark.parametrize("fallback", ["role", "recipe", "procedure_tier", "wrong_service"])
async def test_job_falls_back_to_assisted_for_unsupported_recipe(execution, monkeypatch, fallback):
    ctx = context(tier=1, role="recovery_email" if fallback == "role" else "login_email")
    if fallback == "recipe":
        ctx.procedure.recipe = "missing"
    if fallback == "procedure_tier":
        ctx.procedure.tier = 3
    if fallback == "wrong_service":
        ctx.procedure.recipe = "canva_change_email"
    execution.add(ctx)

    async def forbidden_browser(*args):
        pytest.fail("Unsupported recipe must not open a browser")

    monkeypatch.setattr(BrowserExecutor, "run", forbidden_browser)
    assert await jobs.handle_execute_change("demo", {"change_id": "change-1"}) == {
        "change_id": "change-1",
        "status": "awaiting_user",
    }
    assert "Open <b>Notion</b> settings" in execution.notifications[0][1]


async def test_job_confirmed_recipe_applies_change_and_notifies(execution, monkeypatch):
    execution.add(context(tier=1))

    async def confirmed(*args):
        return True

    monkeypatch.setattr(BrowserExecutor, "run", confirmed)
    assert await jobs.handle_execute_change("demo", {"change_id": "change-1"}, trace="trace") == {
        "change_id": "change-1",
        "status": "done",
    }
    assert execution.applied == [("demo", "change-1", None, "trace")]
    assert execution.notifications == [("demo", "Done ✅ <b>Notion</b> now uses demo@example.com.", None)]


@pytest.mark.parametrize("error", [RuntimeError, ValueError, OSError, TimeoutError])
async def test_job_failure_marks_failed_and_releases_claim(execution, monkeypatch, error):
    row = execution.add(context(tier=1))

    async def failing(*args):
        raise error("private browser data 123456")

    monkeypatch.setattr(BrowserExecutor, "run", failing)
    assert await jobs.handle_execute_change("demo", {"change_id": "change-1"}) == {
        "change_id": "change-1",
        "status": "failed",
    }
    assert row["change"]["status"] == "failed" and row["change"]["exec_active"] is False
    assert "123456" not in json.dumps(execution.events)
    assert execution.applied == []


def corpus_evidence(filename):
    parsed = parse_email((FIXTURES / filename).read_bytes(), "email_primary")
    return Evidence(
        id=filename,
        msg_id=parsed.msg_id,
        inbox=parsed.inbox,
        date=parsed.date,
        sender=parsed.sender,
        subject=parsed.subject,
        snippet=parsed.body_text[:300],
        event=EventKind.contact_changed,
    )


@pytest.mark.parametrize(
    ("filename", "domain", "pattern", "root"),
    [
        (
            "05_amazon_change.eml",
            "amazon.in",
            r"(?i)(?:mobile|phone).*(?:changed|updated)",
            Root(id="phone_new", kind="phone", label="••7788", last4="7788"),
        ),
        (
            "23_email_change.eml",
            "figma.com",
            r"(?i)email address changed",
            Root(id="email_primary", kind="email", label="demo@example.com", address="demo@example.com"),
        ),
    ],
)
def test_confirmation_matches_synthetic_corpus(filename, domain, pattern, root):
    assert verify.matches_confirmation(
        corpus_evidence(filename),
        started_at=START,
        domains=[domain],
        patterns=[pattern],
        new_root=root,
    )


@pytest.mark.parametrize(
    ("filename", "domain", "root"),
    [
        (
            "05_amazon_change.eml",
            "amazon.in",
            Root(id="phone_target", kind="phone", label="••4321", last4="4321"),
        ),
        (
            "23_email_change.eml",
            "figma.com",
            Root(id="email_target", kind="email", label="demo@example.edu", address="demo@example.edu"),
        ),
    ],
)
def test_confirmation_rejects_target_named_as_old_contact(filename, domain, root):
    assert not verify.matches_confirmation(
        corpus_evidence(filename),
        started_at=START,
        domains=[domain],
        patterns=[r"(?i)changed"],
        new_root=root,
    )


@pytest.mark.parametrize(
    "mismatch", ["stale", "same_time", "domain", "spoofed_display", "target", "otp", "pattern"]
)
def test_confirmation_rejects_unrelated_evidence(mismatch):
    evidence = corpus_evidence("23_email_change.eml")
    if mismatch in {"stale", "same_time"}:
        evidence.date = START - timedelta(seconds=1 if mismatch == "stale" else 0)
    if mismatch == "domain":
        evidence.sender = "notice@figma.com.evil.test"
    if mismatch == "spoofed_display":
        evidence.sender = '"notice@figma.com" <sender@evil.test>'
    if mismatch == "target":
        evidence.snippet = evidence.snippet.replace("demo@example.com", "other@example.com")
    if mismatch == "otp":
        evidence.event = EventKind.otp_email
    patterns = [r"(?i)phone number changed"] if mismatch == "pattern" else [r"(?i)email address changed"]
    assert not verify.matches_confirmation(
        evidence,
        started_at=START,
        domains=["figma.com"],
        patterns=patterns,
        new_root=Root(id="email_primary", kind="email", label="demo@example.com", address="demo@example.com"),
    )


def test_verify_pending_applies_matching_evidence_to_exact_change(execution):
    evidence = corpus_evidence("23_email_change.eml")
    for change_id, address in [
        ("change-match", "demo@example.com"),
        ("change-other", "other@example.com"),
        ("change-no-evidence", "demo@example.com"),
    ]:
        ctx = context(status="awaiting_user", change_id=change_id)
        ctx.change.account_id = f"account-{change_id}"
        ctx.service = Service(id="figma", name="Figma", domains=["figma.com"])
        ctx.new_root.address = address
        ctx.new_root.label = address
        row = execution.add(ctx)
        row["change"]["started_at"] = START.isoformat()
        row["evidence"] = (
            [None] if change_id == "change-no-evidence" else [None, evidence.model_dump(mode="json")]
        )
        execution.pending.append(row)
    assert verify.verify_pending("demo", trace="verify-trace") == ["change-match"]
    assert execution.applied == [("demo", "change-match", "23_email_change.eml", "verify-trace")]
    assert execution.rows["change-other"]["change"]["status"] == "awaiting_user"
    assert execution.rows["change-no-evidence"]["change"]["status"] == "awaiting_user"
    assert execution.notifications == [("demo", "Done ✅ <b>Figma</b> now uses demo@example.com.", None)]


def test_verify_pending_requires_resolved_replacement_root(execution):
    row = execution.add(context(status="running"))
    row.update(new_root=None, evidence=[corpus_evidence("23_email_change.eml").model_dump(mode="json")])
    row["change"]["started_at"] = START.isoformat()
    execution.pending = [row]
    assert verify.verify_pending("demo") == []
    assert execution.applied == execution.notifications == []


def test_confirmation_requires_positive_replacement_not_previous_contact():
    evidence = corpus_evidence("23_email_change.eml")
    evidence.snippet = "Your previous email was demo@example.com."
    assert not verify.matches_confirmation(
        evidence,
        started_at=START,
        domains=["figma.com"],
        patterns=[r"(?i)email address changed"],
        new_root=Root(id="new", kind="email", label="new", address="demo@example.com"),
    )


@pytest.mark.parametrize(
    ("role", "expected"), [("recovery_phone", True), ("sms_2fa", False), ("login_phone", False)]
)
def test_recovery_confirmation_cannot_verify_other_phone_roles(role, expected):
    evidence = corpus_evidence("05_amazon_change.eml")
    evidence.sender = "notice@accounts.google.com"
    evidence.subject = "Your recovery phone has been changed"
    evidence.snippet = "Your recovery phone has been changed from xxx-xxx-4321 to xxx-xxx-7788."
    assert (
        verify.matches_confirmation(
            evidence,
            started_at=START,
            domains=["google.com"],
            patterns=[r"(?i)recovery phone.{0,60}(?:was|has been) (?:changed|updated)"],
            new_root=Root(id="new", kind="phone", label="••7788", last4="7788"),
            role=role,
        )
        is expected
    )


async def test_assisted_dispatch_uses_recipe_link_when_policy_link_is_absent(execution):
    ctx = context(tier=3)
    ctx.procedure.settings_url = None
    execution.add(ctx)
    assert (await jobs.handle_execute_change("demo", {"change_id": "change-1"}))["status"] == "awaiting_user"
    assert execution.notifications[0][2][0]["payload"]["url"] == "https://www.notion.so/home"


async def test_assisted_phone_copy_masks_number_and_explains_role(execution):
    ctx = context(role="sms_2fa")
    ctx.old_root = Root(id="phone_old", kind="phone", label="phone_old", last4="9810")
    ctx.new_root = Root(id="phone_new", kind="phone", label="phone_new", e164="+919999006029")
    ctx.service.name = "A&B"
    execution.add(ctx)
    await AssistedExecutor().run(ctx)
    assert execution.notifications[0][1] == (
        "Open <b>A&amp;B</b> settings and change your 2-Step phone from ••9810 to ••6029.\n\n"
        "I'll check your email for confirmation and tick it off once it's changed."
    )


async def test_assisted_removal_copy_does_not_offer_a_replacement(execution):
    ctx = context()
    ctx.new_root = None
    execution.add(ctx)
    await AssistedExecutor().run(ctx)
    assert execution.notifications[0][1] == (
        "Open <b>Notion</b> settings and remove demo@example.edu as your email.\n\n"
        "I'll check your email for confirmation and tick it off once it's changed."
    )


async def test_assisted_instructions_include_both_phone_roles(execution):
    ctx = context()
    ctx.change.role = UseRole.recovery_phone
    ctx.change.roles = [UseRole.recovery_phone, UseRole.sms_2fa]
    execution.add(ctx)
    await AssistedExecutor().run(ctx)
    assert "recovery phone and 2-Step phone" in execution.notifications[0][1]


def test_web_execution_text_is_plain_while_notification_keeps_bold(execution):
    ctx = context()
    execution.add(ctx)
    ctx.emitter.emit("need_user", "Open <b>Notion</b> settings &amp; change your email.")
    assert execution.events[0][2]["message"] == "Open Notion settings & change your email."
    assert execution.notifications[0][1] == "Open <b>Notion</b> settings &amp; change your email."
