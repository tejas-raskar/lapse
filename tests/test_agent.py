import copy
import json
import shutil
import sys
from contextlib import asynccontextmanager, contextmanager
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import anyio
import pytest
from mcp.shared.memory import create_client_server_memory_streams
from openai.types.chat import ChatCompletionMessage
from redis.exceptions import ResponseError

from lapse import trace
from lapse.agent import agent, guard, mcp_server, memory
from lapse.agent import tools as registry_tools
from lapse.graph import db
from lapse.trace import current_turn


def message(text=None, calls=()):
    return ChatCompletionMessage.model_validate(
        {
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {
                    "id": f"call-{index}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": args if isinstance(args, str) else json.dumps(args),
                    },
                }
                for index, (name, args) in enumerate(calls)
            ]
            or None,
        }
    )


class FakeLLM:
    def __init__(self, *script):
        self.script = list(script)
        self.requests = []
        self.closed = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        response = self.script.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(choices=[SimpleNamespace(message=response)])

    async def close(self):
        self.closed = True


@pytest.fixture
def world(monkeypatch):
    monkeypatch.delenv("AGENT_TOOL_TRANSPORT", raising=False)
    state = SimpleNamespace(calls=[], minted=[], saved=[], linked=[], prior=[], llm=None, traced=[])
    state.root = {"id": "phone-old", "label": "••4321", "kind": "root"}
    state.scenario = {
        "id": "scenario-1",
        "kind": "takeover",
        "root_ids": ["phone-old"],
        "affected_accounts": [
            {"node_id": "gmail", "label": "Gmail"},
            {"node_id": "dropbox", "label": "Dropbox"},
        ],
        "direct_accounts": ["gmail"],
        "unknown_accounts": [],
    }
    state.plan = {
        "id": "plan-1",
        "old_root": "phone-old",
        "exposure_before": 2,
        "exposure_after": 0,
        "feasible": True,
        "steps": [
            {
                "id": f"change-{index}",
                "order": index,
                "account_id": "dropbox",
                "account_label": "Dropbox",
                "tier": tier,
                "status": status,
            }
            for index, (tier, status) in enumerate(
                [(1, "proposed"), (2, "proposed"), (3, "proposed"), (4, "proposed"), (1, "done")], 1
            )
        ],
    }

    def dispatch(uid, name, args):
        state.calls.append((uid, name, copy.deepcopy(args), current_turn.get()))
        if name == "resolve":
            candidate = (
                {"id": "dropbox", "label": "Dropbox", "kind": "account"}
                if "dropbox" in args["hint"].lower()
                else state.root
            )
            return {"candidates": [candidate], "ambiguous": False}
        if name == "list_entities":
            return {
                "roots": [state.root],
                "accounts": [{"id": "dropbox", "label": "Dropbox", "modelled": True}],
            }
        if name == "list_roots":
            return [state.root]
        if name in {"takeover_if_reassigned", "lockout_if_lost"}:
            return {**state.scenario, "kind": "lockout" if name == "lockout_if_lost" else "takeover"}
        if name in {"plan_retirement", "plan_status"}:
            return copy.deepcopy(state.plan)
        if name == "explain":
            return {
                "outcome": "path",
                "account_id": "dropbox",
                "steps": [
                    {
                        "from_id": "gmail",
                        "from_label": "Gmail",
                        "to_id": "dropbox",
                        "to_label": "Dropbox",
                        "method_label": "email recovery",
                    },
                ],
            }
        if name == "account_info":
            return {"account": {"id": "dropbox", "display": "Dropbox"}}
        if name == "path_between":
            return {"outcome": "route", "hops": 1, "route": [state.root, {"id": "gmail", "label": "Gmail"}]}
        raise AssertionError(f"Unexpected tool: {name}")

    def mint(uid, changes):
        state.minted.append((uid, list(changes)))
        return "private-approval-token"

    @contextmanager
    def trace_call(uid, name, args):
        record = SimpleNamespace(uid=uid, name=name, args=trace.redact(args), outcome="ok")
        state.traced.append(record)
        yield record

    state.trace_call = trace_call

    approvals = ModuleType("lapse.approvals")
    approvals.mint = mint
    monkeypatch.setitem(sys.modules, "lapse.approvals", approvals)
    monkeypatch.setattr(agent, "dispatch", dispatch)
    monkeypatch.setattr(mcp_server, "dispatch", dispatch)
    monkeypatch.setattr(agent, "create_llm", lambda: state.llm)
    monkeypatch.setattr(agent, "tool_call", trace_call)
    return state


@pytest.fixture
def offline(world, monkeypatch):
    def save(uid, role, text, channel, entity_ids=(), context=None):
        turn_id = f"turn-{len(world.saved)}"
        world.saved.append(
            {
                "id": turn_id,
                "uid": uid,
                "role": role,
                "text": text,
                "channel": channel,
                "entities": set(entity_ids),
                "context": context or {},
            }
        )
        return turn_id

    monkeypatch.setattr(memory, "history", lambda uid: world.prior)
    monkeypatch.setattr(memory, "save_turn", save)
    monkeypatch.setattr(
        memory, "link_turn", lambda uid, turn_id, ids: world.linked.append((uid, turn_id, set(ids)))
    )
    return world


async def test_scripted_model_resolves_runs_scenario_and_returns_buttons(offline):
    text = "Under the methods we model, Gmail and Dropbox are at risk. Plan the fix."
    offline.llm = FakeLLM(
        message(calls=[("resolve", {"hint": "my old number"})]),
        message(calls=[("takeover_if_reassigned", {"root_id": "phone-old"})]),
        message(text),
    )
    reply = await agent.respond("alice", "risk of my old number", channel="web")
    assert "Someone who gets this could get into your" in reply["text"]
    assert "Through that account, they could get into" in reply["text"]
    assert len(offline.llm.requests) == 2
    assert reply["tool_calls"] == ["list_entities", "resolve", "takeover_if_reassigned"]
    assert reply["buttons"] == [
        {"label": "Fix it for me", "action": "plan_root", "payload": {"root_id": "phone-old"}},
        {"label": "Show me", "action": "open_web", "payload": {"path": "/graph?scenario=scenario-1"}},
    ]
    assert all(call[0] == "alice" and call[3] == "turn-0" for call in offline.calls)
    assert offline.saved[-1]["context"] == {
        "scenario_id": "scenario-1",
        "root_id": "phone-old",
        "scenario_kind": "takeover",
    }
    assert offline.saved[-1]["channel"] == "web"
    assert offline.saved[-1]["entities"] >= {"phone-old", "gmail", "dropbox"}
    assert offline.llm.closed
    assert current_turn.get() is None


@pytest.mark.parametrize(
    "prompt, expected, context",
    [
        ("risk of my old number", ["resolve", "takeover_if_reassigned"], {}),
        ("what if I lose my old number", ["resolve", "lockout_if_lost"], {}),
        ("plan the fix for my old number", ["resolve", "plan_retirement"], {}),
        ("why is Dropbox at risk", ["resolve", "explain"], {"scenario_id": "scenario-1"}),
        ("why is Dropbox at risk", ["resolve", "account_info"], {}),
        ("status", ["plan_status"], {"plan_id": "plan-1"}),
    ],
)
@pytest.mark.parametrize("failure", ["unavailable", "malformed"])
async def test_broken_model_falls_back_to_intents(offline, prompt, expected, context, failure):
    offline.prior = [{"role": "assistant", "text": "Previous result", "context": context}]
    offline.llm = FakeLLM(
        RuntimeError("rate limited")
        if failure == "unavailable"
        else message(calls=[("resolve", "{broken-json")])
    )
    reply = await agent.respond("alice", prompt)
    assert (
        reply["tool_calls"] == ["list_entities"] + (["resolve"] if failure == "malformed" else []) + expected
    )
    if failure == "malformed":
        (rejected,) = offline.traced
        assert rejected.name == "resolve"
        assert rejected.args == {"malformed_arguments": True}
        assert rejected.outcome == "error"
    assert "under the methods we model" not in reply["text"].lower()
    assert "phone-old" not in reply["text"]
    assert "execute_change" not in reply["tool_calls"]
    assert offline.llm.closed


async def test_no_model_key_uses_fallback_and_mints_only_eligible_steps(offline):
    reply = await agent.respond("alice", "plan the fix for my old number")
    assert reply["tool_calls"] == ["resolve", "plan_retirement"]
    assert offline.minted == [("alice", ["change-1", "change-2", "change-3"])]
    assert reply["buttons"] == [
        {
            "label": "Approve all",
            "action": "approve_changes",
            "payload": {
                "change_ids": ["change-1", "change-2", "change-3"],
                "approval_token": "private-approval-token",
            },
        },
        {"label": "Show me", "action": "open_web", "payload": {"path": "/plans/plan-1"}},
    ]
    assert "private-approval-token" not in reply["text"]
    assert all("private-approval-token" not in turn["text"] for turn in offline.saved)


async def test_plan_token_never_enters_model_context(offline):
    offline.llm = FakeLLM(
        message(calls=[("plan_retirement", {"old_root_id": "phone-old"})]),
        message("Under the methods we model, review the plan before approval."),
    )
    reply = await agent.respond("alice", "plan the fix")
    assert offline.minted == [("alice", ["change-1", "change-2", "change-3"])]
    assert "private-approval-token" not in json.dumps(offline.llm.requests)
    assert reply["buttons"][0]["payload"]["approval_token"] == "private-approval-token"


async def test_six_tool_calls_is_total_limit(offline):
    offline.llm = FakeLLM(*[message(calls=[("resolve", {"hint": "old number"})]) for _ in range(6)])
    reply = await agent.respond("alice", "risk of my old number")
    assert reply["tool_calls"] == ["list_entities"] + ["resolve"] * 5
    assert len(offline.calls) == 6
    assert len(offline.llm.requests) == 5
    assert "••4321" in reply["text"]


async def test_fallback_respects_tool_budget_already_spent(offline):
    offline.llm = FakeLLM(
        *[message(calls=[("resolve", {"hint": "old number"})]) for _ in range(4)], RuntimeError("offline")
    )
    reply = await agent.respond("alice", "risk of my old number")
    assert reply["tool_calls"] == ["list_entities"] + ["resolve"] * 5
    assert len(offline.calls) == 6


async def test_last_eight_history_turns_are_in_model_context(offline):
    offline.prior = [
        {"role": "user" if index % 2 == 0 else "assistant", "text": f"history-{index}"} for index in range(8)
    ]
    offline.llm = FakeLLM(RuntimeError("offline"))
    await agent.respond("alice", "risk of my old number")
    messages = offline.llm.requests[0]["messages"]
    assert messages[0]["role"] == "system"
    assert [item["content"] for item in messages[1:9]] == [f"history-{index}" for index in range(8)]
    assert [item["role"] for item in messages[1:9]] == ["user", "assistant"] * 4
    assert messages[-1] == {"role": "user", "content": "risk of my old number"}


async def test_model_prose_cannot_override_grounded_template(offline):
    offline.llm = FakeLLM(
        message("Spotify is safe", calls=[("takeover_if_reassigned", {"root_id": "phone-old"})]),
    )
    reply = await agent.respond("alice", "risk of my old number")
    assert len(offline.llm.requests) == 1
    assert "Spotify" not in reply["text"]
    assert "get into your <b>Gmail</b>" in reply["text"]
    assert "<b>Gmail</b>" in reply["text"] and "<b>Dropbox</b>" in reply["text"]
    assert reply["tool_calls"] == ["list_entities", "takeover_if_reassigned"]
    assert reply["diagnostics"]["guard_regenerated"] is False


async def test_approval_execution_is_never_an_llm_tool(offline):
    offline.llm = FakeLLM(
        message(calls=[("execute_change", {"change_id": "change-1", "approval_token": "secret"})])
    )
    reply = await agent.respond("alice", "perform this change")
    names = {tool["function"]["name"] for tool in offline.llm.requests[0]["tools"]}
    assert "execute_change" not in names
    assert reply["tool_calls"] == ["list_entities", "execute_change", "list_roots"]
    assert [call[1] for call in offline.calls] == ["list_entities", "list_roots"]
    (rejected,) = offline.traced
    assert rejected.outcome == "error"
    assert rejected.args["approval_token"] == "[redacted]"


async def test_unknown_model_tool_is_traced(offline, monkeypatch):
    monkeypatch.setattr(agent, "dispatch", registry_tools.dispatch)
    monkeypatch.setattr(trace, "tool_call", offline.trace_call)
    offline.llm = FakeLLM(message(calls=[("invented_tool", {})]))
    tools = agent.TurnTools("alice", "turn-0")
    with pytest.raises(ValueError, match="Invalid or unavailable"):
        await agent.model_answer(offline.llm, [], tools, {})
    (rejected,) = offline.traced
    assert rejected.name == "invented_tool"
    assert rejected.outcome == "error"


async def test_college_risk_and_followup_preserve_lockout_kind(offline):
    offline.root = {"id": "email-college", "label": "College email", "kind": "root", "root_kind": "email"}
    offline.scenario["root_ids"] = ["email-college"]
    reply = await agent.respond("alice", "risk of college email")
    assert reply["tool_calls"] == ["resolve", "lockout_if_lost"]
    assert offline.saved[-1]["context"] == {
        "scenario_id": "scenario-1",
        "root_id": "email-college",
        "scenario_kind": "lockout",
    }
    offline.prior = [offline.saved[-1]]
    reply = await agent.respond("alice", "show risk")
    assert reply["tool_calls"] == ["lockout_if_lost"]


@pytest.mark.parametrize(
    "sensitive",
    [
        "The OTP for this login is 123456.",
        "My OTP code is 123456",
        "My password is a long private phrase",
    ],
)
async def test_sensitive_user_input_never_reaches_memory_or_model(offline, sensitive):
    assert memory.safe_text(sensitive) == "[redacted sensitive input]"
    offline.llm = FakeLLM(RuntimeError("offline"))
    await agent.respond("alice", sensitive)
    assert offline.saved[0]["text"] == "[redacted sensitive input]"
    assert offline.llm.requests[0]["messages"][-1]["content"] == "[redacted sensitive input]"


@pytest.mark.parametrize("field", ["text", "hint"])
def test_trace_redacts_credentials_in_free_text_arguments(monkeypatch, field):
    writes = []
    monkeypatch.setattr(db, "user_graph", lambda uid: None)
    monkeypatch.setattr(db, "run", lambda graph, name, params, **kwargs: writes.append(params))
    monkeypatch.setitem(sys.modules, "lapse.events", None)
    with trace.tool_call("alice", "resolve", {field: "The OTP for this login is 123456."}):
        pass
    assert json.loads(writes[0]["args"]) == {field: "[redacted sensitive input]"}
    assert "123456" not in json.dumps(writes)


@pytest.mark.parametrize(
    "text",
    [
        "Under the methods we model, Spotify is at risk.",
        "Under the methods we model, spotify is at risk.",
        "Under the methods we model, ••4321 is at risk.",
        "Under the methods we model, 4321 is at risk.",
    ],
)
def test_guard_rejects_unseen_names_and_numbers(world, text):
    assert guard.grounded(text, [world.scenario]) is False


def test_guard_accepts_grounded_names_without_stock_disclaimer(world):
    assert guard.grounded("Your <b>Gmail</b> and <b>Dropbox</b> accounts are at risk.", [world.scenario])
    assert guard.grounded("Dropbox is at risk.", [world.scenario])
    result = {"outcome": "not_in_graph", "message": "Account absent."}
    assert not guard.grounded("Spotify is safe.", [result])
    assert guard.grounded("I can't tell you whether it's safe.", [result])


@pytest.mark.parametrize("prompt", ["risk of my old number", "approve all"])
async def test_mcp_transport_uses_real_client_session(offline, monkeypatch, prompt):
    server = mcp_server.create_server()._mcp_server

    @asynccontextmanager
    async def in_memory_http(url):
        assert url == "http://127.0.0.1:8766/mcp"
        async with (
            create_client_server_memory_streams() as (client_streams, server_streams),
            anyio.create_task_group() as group,
        ):
            group.start_soon(server.run, *server_streams, server.create_initialization_options())
            try:
                yield (*client_streams, lambda: None)
            finally:
                group.cancel_scope.cancel()

    monkeypatch.setenv("AGENT_TOOL_TRANSPORT", "mcp")
    monkeypatch.setenv("MCP_URL", "http://127.0.0.1:8766/mcp")
    monkeypatch.setattr(agent, "streamable_http_client", in_memory_http)
    offline.llm = FakeLLM(message(calls=[("takeover_if_reassigned", {"root_id": "phone-old"})]))
    reply = await agent.respond("alice", prompt)
    assert all(call[0] == "alice" and call[3] == "turn-0" for call in offline.calls)
    if prompt == "approve all":
        assert reply["tool_calls"] == ["plan_status"]
        assert reply["buttons"][0]["payload"]["change_ids"] == ["change-1", "change-2", "change-3"]
        assert offline.llm.requests == []
    else:
        assert reply["tool_calls"] == ["list_entities", "takeover_if_reassigned"]
        assert "<b>Gmail</b>" in reply["text"] and "<b>Dropbox</b>" in reply["text"]
        assert len(offline.llm.requests) == 1


@pytest.mark.db
async def test_turns_persist_with_about_links_and_last_eight_history(world, monkeypatch, tmp_path):
    graph = db.graph(f"lapse:test:wp4:agent:{uuid4().hex}")
    monkeypatch.setattr(db, "user_graph", lambda uid: graph)
    for path in db.QUERY_DIR.glob("wp4_turn_*.cypher"):
        shutil.copy(path, tmp_path / path.name)
    (tmp_path / "seed.cypher").write_text(
        "CREATE (:Root {id: 'phone-old'}), (:Account {id: 'gmail'}), (:Account {id: 'dropbox'})"
    )
    (tmp_path / "inspect.cypher").write_text(
        "MATCH (t:Turn) OPTIONAL MATCH (t)-[:ABOUT]->(n) "
        "RETURN t AS turn, collect(n.id) AS entities ORDER BY t.at"
    )
    monkeypatch.setattr(db, "QUERY_DIR", tmp_path)
    try:
        db.run(graph, "seed", write=True)
        for index in range(10):
            memory.save_turn("alice", "user", f"earlier {index}", "telegram")
        assert [turn["text"] for turn in memory.history("alice")] == [
            f"earlier {index}" for index in range(2, 10)
        ]
        reply = await agent.respond("alice", "risk of my old number")
        rows = db.run(graph, "inspect").rows
        assert len(rows) == 12
        user, assistant = rows[-2:]
        assert user["turn"]["role"] == "user"
        assert user["turn"]["text"] == "risk of my old number"
        assert assistant["turn"]["role"] == "assistant"
        assert assistant["turn"]["text"] == reply["text"]
        assert set(user["entities"]) == set(assistant["entities"]) == {"phone-old", "gmail", "dropbox"}
        assert memory.history("alice")[-1]["context"] == {
            "scenario_id": "scenario-1",
            "root_id": "phone-old",
            "scenario_kind": "takeover",
        }
    finally:
        try:
            graph.delete()
        except ResponseError as exc:
            if "Invalid graph operation on empty key" not in str(exc):
                raise


@pytest.mark.parametrize(
    "prompt, expected",
    [
        ("what happens if someone gets my old number?", ["resolve", "takeover_if_reassigned"]),
        ("what do I lose when my college email closes?", ["resolve", "lockout_if_lost"]),
        ("is dropbox safe?", ["resolve", "account_info"]),
    ],
)
async def test_demo_wording_routes_when_model_is_unavailable(offline, prompt, expected):
    offline.llm = FakeLLM(RuntimeError("provider unavailable"))
    reply = await agent.respond("alice", prompt)
    assert reply["tool_calls"] == ["list_entities"] + expected
    assert reply["diagnostics"]["fallback_used"] is True
    assert reply["diagnostics"]["llm_tool_calls"] == []
    assert reply["diagnostics"]["guard_regenerated"] is False


def test_template_keeps_takeover_when_model_also_checks_lockout(world):
    takeover = world.scenario
    lockout = {**takeover, "kind": "lockout", "lost_now_accounts": [], "recovery_lost_accounts": []}
    answer = guard.templated([takeover, lockout])
    assert "get into your <b>Gmail</b>" in answer
    assert "get into <b>Dropbox</b> too" in answer
    assert "You can still sign in to your accounts" in answer
    assert answer.count("Want me to fix it?") == 1


def test_focus_drops_replacement_root_scenarios_before_answer_and_memory(world):
    selected = {"candidates": [{"id": "email-college", "kind": "root", "replacement_ids": ["email-primary"]}]}
    college = {**world.scenario, "kind": "lockout", "root_ids": ["email-college"]}
    replacement = {**world.scenario, "kind": "lockout", "root_ids": ["email-primary"]}
    assert guard.focused_results([selected, college, replacement]) == [selected, college]


@pytest.mark.parametrize("prompt", ["approve", "approve all", "go ahead", "Go ahead!"])
async def test_typed_approval_rereads_steps_and_mints_fresh_button_without_model(
    offline, monkeypatch, prompt
):
    offline.prior = [{"role": "assistant", "text": "Stale plan", "context": {"plan_id": "older-plan"}}]
    original = agent.dispatch

    def dispatch(uid, name, args):
        if name == "plan_status" and args.get("plan_id") == "older-plan":
            return {**offline.plan, "id": "older-plan", "steps": []}
        return original(uid, name, args)

    monkeypatch.setattr(agent, "dispatch", dispatch)
    offline.llm = FakeLLM()
    first = await agent.respond("alice", prompt)
    assert first["text"] == "Ready to update <b>Dropbox</b>.\n\nTap Approve all to start."
    assert first["buttons"][0]["payload"]["change_ids"] == ["change-1", "change-2", "change-3"]
    offline.plan["steps"][0]["status"] = "done"
    second = await agent.respond("alice", prompt)
    assert second["tool_calls"] == ["plan_status"]
    assert second["buttons"][0]["label"] == "Approve all"
    assert second["buttons"][0]["payload"]["change_ids"] == ["change-2", "change-3"]
    assert "Dropbox (T1)" not in second["text"]
    assert "(T4)" not in second["text"]
    assert offline.minted == [
        ("alice", ["change-1", "change-2", "change-3"]),
        ("alice", ["change-2", "change-3"]),
    ]
    assert offline.llm.requests == []
    assert second["diagnostics"]["llm_requests"] == 0
    assert second["diagnostics"]["llm_tool_calls"] == []
    assert [call[1:3] for call in offline.calls] == [
        ("plan_status", {}),
        ("plan_status", {}),
    ]
    assert "private-approval-token" not in json.dumps(offline.saved, default=list)


async def test_approve_finds_plan_without_history_and_handles_no_proposed_steps(offline):
    reply = await agent.respond("alice", "approve")
    assert offline.calls[0][1:3] == ("plan_status", {})
    assert reply["buttons"][0]["payload"]["change_ids"] == ["change-1", "change-2", "change-3"]
    for step in offline.plan["steps"]:
        step["status"] = "done"
    reply = await agent.respond("alice", "approve")
    assert (
        reply["text"] == "There's nothing left to approve. You can check your plan for anything still to do."
    )
    assert [button["action"] for button in reply["buttons"]] == ["open_web"]


async def test_approve_without_any_plan_explains_next_action(offline, monkeypatch):
    monkeypatch.setattr(
        agent,
        "dispatch",
        lambda *args: {
            "outcome": "memory",
            "message": "No stored plan has proposed steps. No changes were executed.",
        },
    )
    reply = await agent.respond("alice", "go ahead")
    assert reply["text"] == "There's no plan waiting for approval. Ask me to plan the fix first."
    assert reply["buttons"] == []
    assert offline.minted == []


async def test_one_model_selection_uses_catalog_and_reports_stage_timings(offline):
    offline.llm = FakeLLM(message(calls=[("takeover_if_reassigned", {"root_id": "phone-old"})]))
    reply = await agent.respond("alice", "someone takes my old number")
    assert "Someone who gets this could get into your" in reply["text"]
    assert "Through that account, they could get into" in reply["text"]
    assert reply["diagnostics"]["llm_requests"] == 1
    assert reply["diagnostics"]["llm_attempts"] == 1
    assert reply["diagnostics"]["fallback_used"] is False
    assert reply["diagnostics"]["llm_tool_calls"] == ["takeover_if_reassigned"]
    assert all(reply["diagnostics"][stage] >= 0 for stage in ("llm_ms", "tools_ms", "guard_ms"))
    request = offline.llm.requests[0]
    assert request["max_tokens"] == 256
    assert request["tool_choice"] == "auto"
    assert '"id":"phone-old"' in request["messages"][0]["content"]
    assert '"modelled":true' in request["messages"][0]["content"]
    assert reply["tool_calls"] == ["list_entities", "takeover_if_reassigned"]


async def test_slow_model_is_cancelled_and_falls_back_to_grounded_tools(offline, monkeypatch):
    offline.llm = FakeLLM()
    cancelled = []

    async def slow_model(**kwargs):
        try:
            await anyio.sleep_forever()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(offline.llm.chat.completions, "create", slow_model)
    monkeypatch.setattr(agent, "LLM_BUDGET_SECONDS", 0.01)
    reply = await agent.respond("alice", "what happens if someone gets my old number?")
    assert cancelled == [True]
    assert offline.llm.closed
    assert reply["diagnostics"]["fallback_reason"] == "TimeoutError"
    assert reply["diagnostics"]["llm_requests"] == 1
    assert reply["diagnostics"]["llm_ms"] > 0
    assert reply["tool_calls"] == ["list_entities", "resolve", "takeover_if_reassigned"]
    assert "Someone who gets this could get into your" in reply["text"]
    assert "Through that account, they could get into" in reply["text"]
    assert "under the methods we model" not in reply["text"].lower()
    assert "phone-old" not in reply["text"]


async def test_ambiguous_resolution_stops_before_scenario(offline, monkeypatch):
    original = agent.dispatch

    def dispatch(uid, name, args):
        if name == "resolve":
            return {
                "candidates": [offline.root, {"id": "phone-other", "label": "Other phone", "kind": "root"}],
                "ambiguous": True,
            }
        return original(uid, name, args)

    monkeypatch.setattr(agent, "dispatch", dispatch)
    offline.llm = FakeLLM(message(calls=[("resolve", {"hint": "phone"})]))
    reply = await agent.respond("alice", "what if someone gets my phone")
    assert "Do you mean ••4321 or Other phone?" in reply["text"]
    assert reply["tool_calls"] == ["list_entities", "resolve"]
    assert reply["buttons"] == []


async def test_explanation_checks_lockout_without_another_model_request(offline, monkeypatch):
    offline.prior = [
        {
            "role": "assistant",
            "text": "Prior takeover",
            "context": {
                "scenario_id": "takeover-1",
                "root_id": "phone-old",
                "scenario_kind": "takeover",
            },
        }
    ]
    original = agent.dispatch

    def dispatch(uid, name, args):
        if name == "explain" and args["scenario_id"] == "takeover-1":
            return {"outcome": "not_affected", "account_id": "dropbox"}
        result = original(uid, name, args)
        if name == "explain":
            result["outcome"] = "recovery_lost"
        return result

    monkeypatch.setattr(agent, "dispatch", dispatch)
    offline.llm = FakeLLM(
        message(calls=[("explain", {"account_id": "dropbox", "scenario_id": "takeover-1"})])
    )
    reply = await agent.respond("alice", "why is Dropbox at risk?")
    assert (
        reply["text"] == "You use <b>Gmail</b> to get back into <b>Dropbox</b> if you forget your password."
    )
    assert reply["tool_calls"] == ["list_entities", "explain", "lockout_if_lost", "explain"]
    assert reply["diagnostics"]["llm_requests"] == 1


@pytest.mark.parametrize("use_model", [False, True])
async def test_kept_phone_risk_never_runs_scenario(offline, monkeypatch, use_model):
    root = {"id": "phone-new", "label": "••6029", "kind": "phone", "status": "active", "last4": "6029"}
    original = agent.dispatch

    def dispatch(uid, name, args):
        if name == "list_entities":
            return {"roots": [root], "accounts": []}
        if name == "resolve":
            return {"candidates": [{**root, "kind": "root", "root_kind": "phone"}]}
        if name == "list_roots":
            return [root]
        return original(uid, name, args)

    monkeypatch.setattr(agent, "dispatch", dispatch)
    if use_model:
        offline.llm = FakeLLM(message(calls=[("takeover_if_reassigned", {"root_id": "phone-new"})]))
    reply = await agent.respond("alice", "risk of 6029")
    assert (
        reply["text"]
        == "••6029 is the number you're keeping.\n\nWhich changing email or number should we check?"
    )
    assert reply["buttons"] == []
    assert "takeover_if_reassigned" not in reply["tool_calls"]
    assert reply["diagnostics"]["llm_requests"] == int(use_model)


async def test_full_scenario_uses_dates_and_familiar_names_without_second_model_request(offline, monkeypatch):
    root = {
        "id": "phone-old",
        "label": "••4321 (Vi)",
        "kind": "phone",
        "status": "retiring",
        "last4": "4321",
        "drop_at": "2026-10-31",
        "reissue_after": "2027-01-29",
    }
    original = agent.dispatch

    def dispatch(uid, name, args):
        if name == "list_roots":
            return [root]
        result = original(uid, name, args)
        if name == "takeover_if_reassigned":
            return {
                **result,
                "actor": "stranger",
                "rounds": 2,
                "total_accounts": 2,
                "ms": 1.0,
                "affected_accounts": [
                    {**item, "node_type": "Account", "round": index}
                    for index, item in enumerate(result["affected_accounts"], 1)
                ],
            }
        return result

    monkeypatch.setattr(agent, "dispatch", dispatch)
    offline.llm = FakeLLM(message(calls=[("takeover_if_reassigned", {"root_id": "phone-old"})]))
    reply = await agent.respond("alice", "someone gets my old number")
    assert "your old Vi number (••4321)" in reply["text"]
    assert "<b>Oct 31</b>" in reply["text"]
    assert "90 days" in reply["text"]
    assert "phone-old" not in reply["text"]
    assert reply["tool_calls"] == ["list_entities", "takeover_if_reassigned", "list_roots"]
    assert reply["diagnostics"]["llm_requests"] == 1


async def test_web_reply_has_plain_text_and_telegram_has_bold_names(offline):
    web = await agent.respond("alice", "risk of my old number", channel="web")
    telegram = await agent.respond("alice", "risk of my old number")
    assert "Gmail" in web["text"] and "<b>" not in web["text"]
    assert "<b>Gmail</b>" in telegram["text"]


async def test_small_talk_reply_without_tools(offline):
    offline.llm = FakeLLM(message("Hey! Happy to help you stay on top of things."))
    reply = await agent.respond("alice", "hey whats up")
    assert reply["text"].startswith("Hey! Happy to help")
    assert "what happens if someone gets my old number" in reply["text"]
    assert reply["diagnostics"]["fallback_used"] is False
    assert reply["tool_calls"] == ["list_entities"]


@pytest.mark.parametrize(
    "prose",
    ["Your Gmail is safe.", "You have 3 accounts at risk.", "All good with ••4321!", "<b>hi</b>"],
)
async def test_small_talk_cannot_state_graph_facts(offline, prose):
    offline.llm = FakeLLM(message(prose), message(prose))
    reply = await agent.respond("alice", "hey")
    assert reply["diagnostics"]["fallback_used"] is True
    assert prose not in reply["text"]
    assert [request["tool_choice"] for request in offline.llm.requests] == ["auto", "required"]


async def test_prose_instead_of_tool_retries_with_required_tool(offline):
    offline.llm = FakeLLM(
        message("Someone could get into Gmail."),
        message(calls=[("takeover_if_reassigned", {"root_id": "phone-old"})]),
    )
    reply = await agent.respond("alice", "what happens if someone gets my old number?")
    assert reply["diagnostics"]["fallback_used"] is False
    assert reply["tool_calls"] == ["list_entities", "takeover_if_reassigned"]
    assert "get into your <b>Gmail</b>" in reply["text"]


def test_small_talk_allows_capability_answers():
    catalog = {"roots": [{"id": "phone-old", "label": "••4321"}], "accounts": [{"id": "a", "label": "Gmail"}]}
    assert guard.small_talk("Happy to help! Ask about your accounts whenever.", catalog)
    assert guard.small_talk("Try asking what to fix first for your old number.", catalog)
    assert guard.small_talk("Your Gmail looks good.", catalog) is None
    assert guard.small_talk("Everything is fine.", catalog) is None


async def test_help_question_gets_capability_answer_offline(offline):
    offline.llm = FakeLLM(TimeoutError())
    reply = await agent.respond("alice", "What can you do?")
    assert "with your approval" in reply["text"]
    assert reply["tool_calls"] == []


@pytest.mark.parametrize(
    "question",
    ["what would i lose if lost the old number?", "what if I lose my old number", "lost old number"],
)
async def test_fallback_loss_phrasings_resolve_the_root(offline, question):
    offline.llm = FakeLLM(TimeoutError())
    await agent.respond("alice", question)
    resolves = [call[2] for call in offline.calls if call[1] == "resolve"]
    assert resolves and "if" not in resolves[0]["hint"].split()


def test_small_talk_rejects_service_names_and_access_claims():
    catalog = {"roots": [], "accounts": []}
    assert guard.small_talk("Someone could get into Gmail.", catalog) is None
    assert guard.small_talk("Hey! Your Notion looks good.", catalog) is None
    assert guard.small_talk("Hey! Not much. How can I help?", catalog)


async def test_fallback_route_question_uses_path_between(offline, monkeypatch):
    offline.llm = FakeLLM(TimeoutError())
    await agent.respond("alice", "how does my old number lead to gmail?")
    names = [call[1] for call in offline.calls]
    assert names == ["list_entities", "resolve", "resolve", "path_between"]


async def test_help_question_skips_the_model(offline):
    offline.llm = FakeLLM()
    reply = await agent.respond("alice", "what can you do?")
    assert "with your approval" in reply["text"]
    assert offline.llm.requests == []


async def test_is_account_safe_checks_every_changing_root(offline):
    offline.llm = FakeLLM(message(calls=[("account_info", {"account_id": "gmail"})]))
    reply = await agent.respond("alice", "is gmail safe?")
    assert reply["text"]


@pytest.mark.parametrize(
    "outcomes, affected, incomplete",
    [
        (["error", "error"], False, True),
        (["ok", "error"], True, True),
        (["ok", "error"], False, True),
        (["malformed", "ok"], False, True),
        (["wrong_root", "ok"], False, True),
        (["ok", "ok"], False, False),
    ],
)
async def test_account_risk_never_reassures_on_incomplete_checks(
    world, monkeypatch, outcomes, affected, incomplete
):
    roots = [
        {"id": "old", "kind": "phone", "label": "Old phone", "status": "retiring", "last4": "4321"},
        {
            "id": "college",
            "kind": "email",
            "label": "College",
            "status": "closing",
            "address": "demo@example.edu",
        },
    ]

    def dispatch(uid, name, arguments):
        if name == "list_roots":
            return roots
        outcome = outcomes[0 if arguments["root_id"] == "old" else 1]
        if outcome == "error":
            return {"outcome": "error", "message": "Unavailable. No safety conclusion can be drawn."}
        if outcome == "malformed":
            return {"affected_accounts": []}
        kind = "takeover" if name == "takeover_if_reassigned" else "lockout"
        return {
            "id": arguments["root_id"],
            "kind": kind,
            "root_ids": ["other"] if outcome == "wrong_root" else [arguments["root_id"]],
            "actor": "stranger" if kind == "takeover" else "user",
            "rounds": 1,
            "affected_accounts": [
                {"node_id": "account", "label": "Canva", "node_type": "Account", "round": 1}
            ]
            if affected and kind == "takeover"
            else [],
            "direct_accounts": [],
            "unknown_accounts": [],
            "total_accounts": 1,
            "ms": 0,
        }

    monkeypatch.setattr(agent, "dispatch", dispatch)
    tools = agent.TurnTools(
        uid="test",
        turn_id="turn",
        catalog={"roots": roots},
        results=[{"account": {"id": "account", "display": "Canva", "modelled": True}, "methods": []}],
    )
    text = await tools.account_risk()
    assert ("I still need to check those" in text) == incomplete
    assert ("don't put" in text) == (not incomplete)
    if affected:
        assert "could get into" in text
        assert tools.results[-1]["root_ids"] == ["old"]
        assert "fix what I found so far" in text
    if not incomplete:
        assert "sign-in and recovery options I've checked" in text


@pytest.mark.parametrize(
    "roots_response",
    [
        {"outcome": "error"},
        [],
        [{"id": "old", "kind": "invalid"}],
    ],
)
async def test_account_risk_requires_valid_root_metadata(world, monkeypatch, roots_response):
    monkeypatch.setattr(agent, "dispatch", lambda uid, name, args: roots_response)
    tools = agent.TurnTools(
        uid="test",
        turn_id="turn",
        catalog={"roots": [{"id": "old", "kind": "phone", "status": "retiring"}]},
        results=[{"account": {"id": "account", "display": "Canva", "modelled": True}, "methods": []}],
    )
    text = await tools.account_risk()
    assert "I still need to check those" in text
    assert "don't put" not in text
    assert tools.calls == ["list_roots"]
