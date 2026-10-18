from html import escape

from lapse.bot.cards import join_names
from lapse.contracts import Change
from lapse.exec.base import CONTACT_ROLES, ExecContext, contact_value


class AssistedExecutor:
    def can_run(self, change: Change) -> bool:
        return True

    async def run(self, ctx: ExecContext, *, url: str | None = None) -> bool:
        contact = join_names(CONTACT_ROLES[role] for role in (ctx.change.roles or [ctx.change.role]))
        action = (
            f"change your {contact} from {contact_value(ctx.old_root)} to {contact_value(ctx.new_root)}"
            if ctx.new_root
            else f"remove {contact_value(ctx.old_root)} as your {contact}"
        )
        message = (
            f"Open <b>{escape(ctx.service.name)}</b> settings and {action}.\n\n"
            "I'll check your email for confirmation and tick it off once it's changed."
        )
        ctx.emitter.emit(
            "need_user", message, url=url or ctx.procedure.settings_url or ctx.change.settings_url
        )
        return False
