import json
from datetime import UTC, datetime, time, timedelta
from html import escape

from lapse.contracts import Change
from lapse.exec.base import CONTACT_ROLES, ExecContext, contact_value, redis_client
from lapse.graph import db

REMINDERS = "lapse:exec:reminders"


class OfflineExecutor:
    def can_run(self, change: Change) -> bool:
        return change.tier == 4

    async def run(self, ctx: ExecContext) -> bool:
        action = (
            f"change my {CONTACT_ROLES[ctx.change.role]} from {contact_value(ctx.old_root)} "
            f"to {contact_value(ctx.new_root)}"
            if ctx.new_root
            else f"remove {contact_value(ctx.old_root)} as my {CONTACT_ROLES[ctx.change.role]}"
        )
        text = (
            f"You'll need to handle this with <b>{escape(ctx.service.name)}</b> directly. "
            "Check which documents they need at the link below.\n\n"
            "Bring your ID and account details, and make sure you can use your new phone or email. "
            "Ask them to update your details and keep the receipt. "
            "Check that they removed your old phone or email.\n\n"
            f"You can say: Please {action} and send me written confirmation. "
            "Add your account number, name and signature to the form."
        )
        ctx.emitter.emit("need_user", text, url=ctx.procedure.doc_url)
        due = datetime.now(UTC) + timedelta(days=1)
        if ctx.change.due:
            due = min(due, datetime.combine(ctx.change.due, time(9), tzinfo=UTC))
        redis_client().zadd(REMINDERS, {json.dumps([ctx.uid, ctx.change.id]): due.timestamp()})
        return False


def send_due_reminders(*, trace=None) -> int:
    from lapse.notify import notify

    redis = redis_client()
    sent = 0
    for member in redis.zrangebyscore(REMINDERS, "-inf", datetime.now(UTC).timestamp()):
        uid, change_id = json.loads(member)
        rows = db.run(db.user_graph(uid), "planner_get_change", {"change_id": change_id}, trace=trace).rows
        if not rows or rows[0]["change"]["status"] in {"done", "skipped", "failed"}:
            redis.zrem(REMINDERS, member)
            continue
        notify(
            uid,
            f"A quick reminder to update your details with "
            f"<b>{escape(rows[0]['change']['account_label'])}</b>.",
        )
        redis.zadd(REMINDERS, {member: (datetime.now(UTC) + timedelta(days=1)).timestamp()})
        sent += 1
    return sent


def register_reminders(scheduler) -> None:
    scheduler.add_job(send_due_reminders, "interval", seconds=60, id="exec_reminders", replace_existing=True)
