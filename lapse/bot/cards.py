from __future__ import annotations

import re
from datetime import date
from html import escape

from lapse.contracts import ChangeStatus, Plan, Root, ScenarioResult, Signal

FINISHED = {ChangeStatus.done, ChangeStatus.skipped}
ROLE_NAMES = {
    "recovery_phone": "recovery phone",
    "sms_2fa": "2-Step phone",
    "login_phone": "sign-in phone",
    "login_email": "sign-in email",
    "recovery_email": "recovery email",
    "totp_device": "authenticator",
}


def join_names(names) -> str:
    names = list(names)
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def format_date(value, *, today=None, bold=False) -> str:
    if not value:
        return ""
    value = date.fromisoformat(value) if isinstance(value, str) else value
    days = (value - (today or date.today())).days
    label = f"{value:%b} {value.day}"
    if bold:
        label = f"<b>{label}</b>"
    relative = (
        "today"
        if days == 0
        else (
            f"in {days} {'day' if days == 1 else 'days'}"
            if days > 0
            else f"{abs(days)} {'day' if days == -1 else 'days'} ago"
        )
    )
    return f"{label} ({relative})"


def short_root(root: Root) -> str:
    """Plain-text button label for a root; Telegram buttons do not render HTML."""
    if root.kind == "phone":
        name = f"{'new' if root.status == 'active' else 'old'} number"
        return f"{name} ••{root.last4}" if root.last4 else name
    if root.kind == "email":
        kind = {"edu": "college ", "work": "work "}.get(root.email_kind, "")
        return f"{kind}email"
    if root.kind == "domain":
        return f"domain {root.name or root.label}"
    return root.label


def root_name(root: Root) -> str:
    if root.kind == "phone" and root.last4:
        if root.status == "active":
            return f"your new number (••{escape(root.last4)})"
        carrier = re.search(r"\(([^()]+)\)", root.label)
        name = f"{escape(carrier[1])} " if carrier else ""
        return f"your old {name}number (••{escape(root.last4)})"
    if root.kind == "email":
        kind = {"edu": "college ", "work": "work "}.get(root.email_kind, "")
        return f"your {kind}email ({escape(root.address or root.label)})"
    if root.kind == "domain":
        return f"your domain ({escape(root.name or root.label)})"
    return escape(root.label) if root.label != root.id else f"your {root.kind.value}"


def account_name(label: str, identifier=None) -> str:
    service, _, address = label.partition(" · ")
    identifier = identifier or address
    name = "Google account" if service == "Google" else service
    return f"<b>{escape(name)}</b>" + (f" ({escape(identifier)})" if identifier else "")


def kept_card(root: Root) -> str:
    name = f"••{escape(root.last4)}" if root.kind == "phone" and root.last4 else root_name(root)
    return f"{name[0].upper() + name[1:]} is {'the number' if root.kind == 'phone' else 'something'} you're keeping.\n\nWhich changing email or number should we check?"


def unknown_line(count: int) -> str:
    noun = "another account" if count == 1 else f"{count} other accounts"
    return f"I don't know how {noun} {'lets' if count == 1 else 'let'} people back in, so I can't promise {'it’s' if count == 1 else 'they’re'} safe."


def scenario_card(root: Root, result: ScenarioResult, *, today=None, accounts=()) -> str:
    name = root_name(root)
    when = root.drop_at or root.closes_at or root.expires_at
    dated = f" on {format_date(when, today=today, bold=True)}" if when else ""
    direct = [a for a in result.affected_accounts if a.node_id in result.direct_accounts]
    indirect = [a for a in result.affected_accounts if a.node_id not in result.direct_accounts]
    if result.kind == "takeover":
        if root.kind == "phone":
            lines = [f"Heads up 👀 You're giving up {name}{dated}."]
            if root.reissue_after and root.drop_at:
                days = (root.reissue_after - root.drop_at).days
                lines.append(
                    f"Carriers can hand it to someone new after about {days} {'day' if days == 1 else 'days'}."
                )
        else:
            lines = [f"{name[0].upper() + name[1:]} expires{dated}."]
        if direct:
            lines.append(
                "Whoever gets it could get into your "
                + join_names(account_name(a.label) for a in direct)
                + "."
            )
        if indirect:
            lines.append(
                f"Through {'that account' if len(direct) == 1 else 'those accounts'}, they could get into "
                + join_names(account_name(a.label.split(" · ")[0]) for a in indirect)
                + " too."
            )
        if not result.affected_accounts:
            lines.append("None of the accounts I've checked can be accessed through it.")
    else:
        if root.status == "active":
            lines = [f"If you lost {name}:"]
        else:
            verb = "shuts down" if root.kind == "email" else "will no longer be available"
            lines = [f"{name[0].upper() + name[1:]} {verb}{dated}."]
        hosted = [
            a
            for a in result.lost_now_accounts
            if root.kind == "email"
            and root.address
            and a.label.endswith(" · " + root.address)
            and a.label.split(" · ")[0].casefold() == root.hosted_by_service
        ]
        lost = [a for a in result.lost_now_accounts if a not in hosted]
        if hosted:
            lines.append(
                "The "
                + join_names(account_name(a.label.split(" · ")[0]) for a in hosted)
                + " that comes with it goes too, so save anything you need from it."
            )
        if lost:
            lines.append("You'd lose access to " + join_names(account_name(a.label) for a in lost) + ".")
        lost_ids = {a.node_id for a in result.lost_now_accounts}
        recovery = [a for a in result.recovery_lost_accounts if a.node_id not in lost_ids]
        if recovery:
            lines.append(
                "If you forget your password, you could lose access to "
                + join_names(account_name(a.label) for a in recovery)
                + "."
            )
        if not lost and not recovery:
            if not result.unknown_accounts:
                moved = [
                    a
                    for a in accounts
                    if root.address in a.get("previous_identifiers", [])
                    and a.get("identifier") != root.address
                ]
                reason = (
                    ", because you moved "
                    + join_names(account_name(a["display"].split(" · ")[0]) for a in moved)
                    + " already"
                    if moved
                    else ""
                )
                lines.append(f"Good news: nothing else I've checked depends on it{reason} ✅")
            else:
                lines.append("None of the other accounts I've checked lose access.")
    if result.unknown_accounts:
        lines.append(unknown_line(len(result.unknown_accounts)))
    if result.affected_accounts and (result.kind == "takeover" or lost or recovery):
        lines.append("Want me to fix it?")
    question = lines.pop() if lines[-1] == "Want me to fix it?" else None
    paragraphs = []
    for index in range(0, len(lines), 2):
        paragraphs.append(" ".join(lines[index : index + 2]))
    if question:
        paragraphs.append(question)
    return "\n\n".join(paragraphs)


def risk_summary(items, *, today=None) -> str:
    if not items:
        return "You're keeping everything I know about. Nothing needs changing right now."
    lines = ["Here's what's changing:"]
    for root, result in sorted(
        items, key=lambda item: item[0].drop_at or item[0].closes_at or item[0].expires_at or date.max
    ):
        when = root.drop_at or root.closes_at or root.expires_at
        dated = f" on {format_date(when, today=today, bold=True)}" if when else ""
        name = root_name(root)
        count = len(result.affected_accounts)
        if result.kind == "takeover":
            detail = (
                f"Someone who gets it could get into {count} of your {'account' if count == 1 else 'accounts'}"
                if count
                else "None of the accounts I've checked can be accessed through it"
            )
            lines.append(f"You're giving up {name}{dated}. {detail}.")
        else:
            detail = (
                f"You could lose access to {count} {'account' if count == 1 else 'accounts'}"
                if count
                else "None of the accounts I've checked lose access"
            )
            lines.append(f"{name[0].upper() + name[1:]} closes{dated}. {detail}.")
        if result.unknown_accounts:
            lines.append(unknown_line(len(result.unknown_accounts)))
    lines.append("Which one would you like to sort out?")
    return "\n\n".join(lines)


def account_risk_card(account: dict, items, *, today=None, incomplete=()) -> str:
    name = account_name(account["display"], account.get("identifier"))
    lines = []
    for root, result in items:
        if account["id"] not in {item.node_id for item in result.affected_accounts}:
            continue
        when = root.drop_at or root.closes_at or root.expires_at
        dated = f" on {format_date(when, today=today, bold=True)}" if when else ""
        if result.kind == "takeover":
            lines.append(
                f"You're giving up {root_name(root)}{dated}. Someone who gets it could get into your {name}."
            )
        else:
            lost_now = account["id"] in {item.node_id for item in result.lost_now_accounts}
            condition = "" if lost_now else " if you forget your password"
            lines.append(
                f"When you lose {root_name(root)}{dated}, you could lose access to {name}{condition}."
            )
    if incomplete:
        contacts = join_names(root_name(root) if root else "a changing contact" for root in incomplete)
        warning = (
            f"I couldn't complete the checks for {contacts}. "
            "I still need to check those before I can tell you the full risk."
        )
        if not lines:
            return f"I couldn't finish checking your {name}.\n\n{warning}\n\nTry again in a moment."
        return "\n\n".join(lines) + "\n\n" + warning + "\n\nWant me to fix what I found so far?"
    if not lines:
        return f"The changes you've told me about don't put your {name} at risk based on the sign-in and recovery options I've checked."
    return "\n\n".join(lines) + "\n\nWant me to fix it?"


def plan_card(plan: Plan, result: ScenarioResult | None = None, *, roots=(), today=None) -> str:
    by_id = {root.id: root for root in roots}
    root = by_id.get(plan.old_root)
    name = root_name(root) if root else "this change"
    steps = sorted(plan.steps, key=lambda step: step.order)
    count = len(steps)
    lines = [
        f"Here's how we fix {name}. "
        + ("Just one step:" if count == 1 else f"{count} steps:" if count else "No account changes to make.")
    ]
    for step in steps:
        target = by_id.get(step.to_root)
        destination = (
            f"••{escape(target.last4)}"
            if target and target.last4
            else root_name(target)
            if target
            else "your replacement contact"
        )
        roles = join_names(ROLE_NAMES[role] for role in (step.roles or [step.role]))
        action = (
            f"Change the {roles} for your {account_name(step.account_label)} to {destination}."
            if step.to_root
            else f"Remove the old {roles} from your {account_name(step.account_label)}."
        )
        if step.status in FINISHED:
            action += " Done." if step.status == ChangeStatus.done else " You chose to skip this."
        elif step.status == ChangeStatus.failed:
            action += " That didn't work. Use the link to do it yourself."
        elif step.status in {ChangeStatus.running, ChangeStatus.approved}:
            action += " I'm on it."
        elif step.status == ChangeStatus.awaiting_otp:
            action += " I'm waiting for your code."
        elif step.tier in {1, 2}:
            action += " I can do this for you once you approve."
        elif step.tier == 3:
            action += (
                " You'll need to tap the link and make this change."
                if step.settings_url
                else " You'll need to do this in your account settings."
            )
        else:
            action += " You'll need to handle this with them directly."
        if step.due and step.status not in FINISHED:
            action += f" Do it before {format_date(step.due, today=today, bold=True)}."
        lines.append(action)
    if steps and any(step.status not in FINISHED for step in steps):
        lines.append("I'll watch for a confirmation email and tick it off when I can confirm the change.")
    if not plan.feasible or plan.exposure_after:
        lines.append("These steps won't fix everything. Some accounts still need your attention.")
    if plan.unknown_accounts:
        lines.append(unknown_line(len(plan.unknown_accounts)))
    if not steps and root and root.kind == "email" and plan.remaining_accounts:
        lines.append("The account that comes with this email closes too. Save anything you need from it.")
    if any(step.status == ChangeStatus.proposed and step.tier <= 3 for step in steps):
        lines.append("Ready? Tap Approve all to start.")
    return "\n\n".join(lines)


def status_card(roots: list[Root], plans: list[Plan], signals: list[Signal]) -> str:
    lines = []
    for root in roots:
        name = root_name(root)
        when = root.drop_at or root.closes_at or root.expires_at
        status = (
            "You're keeping"
            if root.status == "active"
            else "You're giving up"
            if root.status == "retiring"
            else "You're losing"
            if root.status == "closing"
            else "You're no longer using"
        )
        lines.append(f"{status} {name}" + (f" on {format_date(when)}" if when else "") + ".")
    if not roots:
        lines.append("Add a phone number or email in the app to get started.")
    for plan in plans:
        count = sum(s.status not in FINISHED for s in plan.steps)
        if count:
            lines.append(
                f"{count} {'step' if count == 1 else 'steps'} left"
                + (f" before {format_date(plan.deadline)}" if plan.deadline else "")
                + "."
            )
    lines.extend(
        escape(signal.title) + (f" by {format_date(signal.deadline)}" if signal.deadline else "")
        for signal in signals
    )
    if roots:
        lines.append("Use /risk to see what needs attention next.")
    return "\n\n".join(lines)
