from datetime import UTC, datetime
from html import escape

from lapse.contracts import Change, ChangeStatus, Root
from lapse.exec.base import CONTACT_ROLES, ExecContext, StepEmitter, contact_value
from lapse.exec.recipes import RECIPES
from lapse.exec.recipes.dsl import BrowserExecutor
from lapse.exec.t3_assisted import AssistedExecutor
from lapse.exec.t4_offline import OfflineExecutor
from lapse.graph import db
from lapse.graph.planner import apply_change_done
from lapse.policy import loader


def load_policies():
    return {
        p.service.id: p for path in loader.default_policy_paths() for p in loader.load_policy_file(path)[0]
    }


async def handle_execute_change(uid: str, payload: dict, *, trace=None) -> dict:
    change_id = payload["change_id"]
    g = db.user_graph(uid)
    rows = db.run(g, "exec_context", {"change_id": change_id}, trace=trace).rows
    if not rows:
        raise ValueError("Unknown change or missing account/root")
    row = rows[0]
    change = Change.model_validate({"to_root": None, **row["change"]})
    if change.status != ChangeStatus.approved:
        return {"change_id": change.id, "status": change.status.value}
    old_root = Root.model_validate(row["old_root"])
    new_root = Root.model_validate(row["new_root"]) if row["new_root"] else None
    if change.to_root and new_root is None:
        raise ValueError("Missing replacement root")
    policy = load_policies()[row["account"]["service_id"]]
    procedure = next(p for p in policy.change if p.root_kind == old_root.kind)
    recipe = RECIPES.get(procedure.recipe)
    if recipe and recipe.service_id == policy.service.id and not procedure.settings_url:
        procedure = procedure.model_copy(update={"settings_url": recipe.settings_url})
    started_at = datetime.now(UTC)
    claimed = db.run(
        g,
        "exec_start",
        {"change_id": change.id, "started_at": started_at.isoformat()},
        write=True,
        trace=trace,
    ).rows
    if not claimed:
        return {"change_id": change.id, "status": "already_claimed"}
    change.status = ChangeStatus.running
    emitter = StepEmitter(uid, change.id, trace=trace)
    ctx = ExecContext(uid, change, procedure, policy.service, old_root, new_root, started_at, emitter, trace)
    executor = OfflineExecutor() if change.tier == 4 else AssistedExecutor()
    if recipe and recipe.service_id == policy.service.id:
        candidate = BrowserExecutor(recipe)
        if candidate.can_run(change) and procedure.tier in {1, 2}:
            executor = candidate
    try:
        emitter.emit(
            "progress",
            f"I'm checking your <b>{escape(policy.service.name)}</b> {CONTACT_ROLES[change.role]} settings now…",
        )
        confirmed = await executor.run(ctx)
        if confirmed:
            apply_change_done(uid, change.id, trace=trace)
            message = f"Done ✅ <b>{escape(policy.service.name)}</b> now uses {contact_value(new_root)}."
            emitter.emit("done", message)
            from lapse.notify import notify

            notify(uid, message)
    except (RuntimeError, ValueError, OSError, TimeoutError):
        emitter.emit(
            "failed",
            f"I couldn't finish that change on <b>{escape(policy.service.name)}</b>.\n\n"
            "Check your account settings before trying again.",
        )
        return {"change_id": change.id, "status": "failed"}
    finally:
        db.run(g, "exec_release", {"change_id": change.id}, write=True, trace=trace)
    current = db.run(g, "planner_get_change", {"change_id": change.id}, trace=trace).rows[0]["change"]
    return {"change_id": change.id, "status": current["status"]}


def register_handlers() -> None:
    from lapse.worker import handler

    handler("execute_change")(handle_execute_change)
