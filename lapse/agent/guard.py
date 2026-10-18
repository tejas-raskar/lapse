from __future__ import annotations

import re
from html import escape, unescape
from pathlib import Path

from pydantic import ValidationError

from lapse.bot.cards import account_name, format_date, join_names, plan_card, root_name, scenario_card
from lapse.contracts import Plan, Root, ScenarioResult

ENTITY_FIELDS = {
    "id",
    "node_id",
    "account_id",
    "root_id",
    "service_id",
    "from_id",
    "to_id",
    "old_root",
    "new_root",
    "from_root",
    "to_root",
    "label",
    "display",
    "name",
    "account_label",
    "from_label",
    "to_label",
    "identifier",
    "address",
    "carrier",
    "last4",
}
ENTITY_LISTS = {"root_ids", "direct_accounts", "unknown_accounts", "replacement_ids"}
PROSE = set(Path(__file__).with_name("prompts").joinpath("grounded_words.txt").read_text().split())


def entities(results):
    values = set()

    def walk(value, key=""):
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, child_key)
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, str) and (key in ENTITY_FIELDS or key in ENTITY_LISTS) and value:
            values.add(value)

    for result in results:
        walk(result)
    return values


def grounded(text: str, results: list) -> bool:
    if not text:
        return False
    remainder = unescape(re.sub(r"</?b>", "", text))
    names = entities(results)
    names |= {part for name in names for part in name.split(" · ")}
    for name in tuple(names):
        phone = re.fullmatch(r"(?:••)?(\d{4})(?: \(([^()]+)\))?", name)
        if phone:
            names.add(f"••{phone[1]}")
            if phone[2]:
                names.add(phone[2])
    for value in sorted(names, key=len, reverse=True):
        remainder = re.sub(r"(?<!\w)" + re.escape(value) + r"(?!\w)", " ", remainder, flags=re.IGNORECASE)
    words = re.findall(r"[\w@.+•-]+", remainder.casefold())
    return all(
        word.strip(".-") in PROSE or re.fullmatch(r"\d{1,3}(?:[.,]\d{1,2})?|t[1-4]", word.strip(".-"))
        for word in words
        if word.strip(".-")
    )


CLAIM_WORDS = re.compile(
    r"\b(?:safe|unsafe|secure|secured|risk|risky|exposed|protected|compromised|hacked|locked|"
    r"affected|vulnerable|fine|okay|ok|password|passwords|access|lose|lost|reach|stranger|someone|"
    r"attacker|get\s+into|take\s+over|takeover|depends?|tied)\b",
    re.IGNORECASE,
)


def small_talk(text: str | None, catalog: dict) -> str | None:
    """Accept a tool-free model reply only when it states nothing about the user's graph."""
    text = (text or "").strip()
    if not text or len(text) > 400 or re.search(r"\d|<|>|`", text) or CLAIM_WORDS.search(text):
        return None
    # Service names are capitalised; only sentence starts, "I" and "Lapse" may be.
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        for word in re.findall(r"[A-Za-z][\w'’]*", sentence)[1:]:
            if word[0].isupper() and word not in {"I", "I'm", "I’m", "I'll", "I’ll", "Lapse"}:
                return None
    lowered = text.casefold()
    for item in [*catalog.get("roots", []), *catalog.get("accounts", [])]:
        for value in (item.get("id"), item.get("label"), item.get("service")):
            for part in str(value or "").split(" · "):
                if len(part) >= 3 and part.casefold() in lowered:
                    return None
    return escape(text)


def focused_results(results: list) -> list:
    for result in results:
        if not isinstance(result, dict):
            continue
        candidates = result.get("candidates", [])
        if len(candidates) == 1 and candidates[0].get("kind") == "root":
            root_id = candidates[0]["id"]
            return [
                item
                for item in results
                if not isinstance(item, dict)
                or "affected_accounts" not in item
                or root_id in item["root_ids"]
            ]
    return results


def _roots(results):
    roots = []
    for result in results:
        items = (
            result
            if isinstance(result, list)
            else result.get("roots", [])
            if isinstance(result, dict)
            else []
        )
        for item in items:
            try:
                roots.append(Root.model_validate(item))
            except ValidationError:
                continue
    return roots


def _scenario(result, results):
    root = next((root for root in _roots(results) if root.id in result["root_ids"]), None)
    if root is not None:
        try:
            return scenario_card(root, ScenarioResult.model_validate(result))
        except ValidationError:
            pass
    affected = result["affected_accounts"]
    direct_ids = set(result["direct_accounts"])
    direct = [account_name(a["label"]) for a in affected if a["node_id"] in direct_ids]
    indirect = [account_name(a["label"]) for a in affected if a["node_id"] not in direct_ids]
    lines = []
    if result["kind"] == "lockout":
        lost = [account_name(a["label"]) for a in result.get("lost_now_accounts", [])]
        recovery = [account_name(a["label"]) for a in result.get("recovery_lost_accounts", [])]
        if lost:
            lines.append(
                f"You could lose access to {join_names(lost)}. Save anything you need from it first."
                if len(lost) == 1
                else f"You could lose access to {join_names(lost)}. Save anything you need first."
            )
        if recovery:
            lines.append(
                f"You could also lose your way back into {join_names(recovery)} if you forget your password."
            )
        if not lost and not recovery:
            if affected and "lost_now_accounts" not in result:
                lines.append(
                    f"Losing this could affect your access to {join_names([account_name(a['label']) for a in affected])}."
                )
            else:
                lines.append(
                    "You can still sign in to your accounts and get back in if you forget your password."
                )
    else:
        if direct:
            lines.append(f"Someone who gets this could get into your {join_names(direct)}.")
        if indirect:
            lines.append(
                f"Through {'that account' if len(direct) == 1 else 'those accounts'}, "
                f"they could get into {join_names(indirect)} too."
                if direct
                else f"They could get into {join_names(indirect)} through other accounts."
            )
        if not affected:
            lines.append("I didn't find any accounts someone could get into this way.")
    if result.get("unknown_accounts"):
        lines.append(
            "I don't know how some of your other sites let people back in, so I can't promise they're safe."
        )
    if affected:
        lines.append("Want me to fix it?")
    return "\n\n".join(lines)


def _node_name(label: str) -> str:
    phone = re.fullmatch(r"••(\d{4})(?: \(([^()]+)\))?", label)
    if phone:
        return f"your {escape(phone[2]) + ' ' if phone[2] else ''}number (••{phone[1]})"
    if " · " not in label:
        return f"<b>{escape(label)}</b>"
    return account_name(label)


def templated(results: list) -> str:
    scenarios = {r["kind"]: r for r in results if isinstance(r, dict) and "affected_accounts" in r}
    if len(scenarios) > 1 and isinstance(results[-1], dict) and "affected_accounts" in results[-1]:
        answers = [
            _scenario(result, results).removesuffix("\n\nWant me to fix it?") for result in scenarios.values()
        ]
        return "\n\n".join(answers) + "\n\nWant me to fix it?"
    for result in reversed(results):
        if isinstance(result, list) and result:
            roots = _roots([result])
            if roots:
                lines = []
                for root in roots:
                    date = root.drop_at or root.closes_at or root.expires_at
                    ending = " is staying with you" if root.status == "active" else " is changing"
                    if date:
                        ending += f" on {format_date(date, bold=True)}"
                    lines.append(root_name(root) + ending + ".")
                return "\n\n".join(lines) + "\n\nWhich one would you like to check?"
            labels = [
                escape(item["label"]) for item in result if isinstance(item, dict) and item.get("label")
            ]
            if labels:
                return f"Which one would you like to check: {join_names(labels)}?"
            continue
        if not isinstance(result, dict):
            continue
        if result.get("outcome") == "keystones":
            items = result["keystones"]
            if not items:
                return "I didn't find one account that many others depend on in the accounts I've checked.\n\nWhich account would you like to check?"
            lines = []
            for index, item in enumerate(items):
                count = item["downstream"]
                lead = "Start with" if index == 0 else "Then check"
                lines.append(
                    f"{lead} {_node_name(item['label'])}. "
                    f"{count} {'account depends' if count == 1 else 'accounts depend'} on it."
                )
            lines.append("Some accounts need several things to get in. Want me to plan the fixes?")
            return "\n\n".join(lines)
        if result.get("outcome") == "route":
            names = " → ".join(_node_name(step["label"]) for step in result["route"])
            hops = result["hops"]
            return (
                f"The shortest way in takes {hops} {'step' if hops == 1 else 'steps'}:\n\n"
                f"{names}\n\nThat's the chain to break. Want me to plan the fix?"
            )
        if result.get("outcome") == "no_single_route":
            return (
                "There's no way in that needs only one thing. Someone with several of your accounts "
                "at once could still get in, so I can't promise it's safe."
            )
        if result.get("outcome") == "kept":
            return f"{escape(result['label'])} is the number you're keeping.\n\nWhich changing email or number should we check?"
        if result.get("outcome") in {"error", "not_in_graph", "unknown_root"}:
            account = result.get("account") or {}
            service = result.get("service") or {}
            label = service.get("name") or account.get("display")
            if label:
                return f"I don't know how {account_name(label)} lets people back in, so I can't promise it's safe."
            if result.get("outcome") == "error":
                return "I couldn't check that right now. Please try again."
            return "I haven't found that account or contact yet, so I can't tell you whether it's safe. Which email, number, or service do you mean?"
        if "affected_accounts" in result:
            return _scenario(result, results)
        if "exposure_before" in result:
            try:
                return plan_card(Plan.model_validate(result), roots=_roots(results))
            except ValidationError:
                steps = result["steps"]
                if not steps:
                    return "There are no changes to make right now."
                lines = ["Here's how we fix it:"]
                for step in steps:
                    status = step.get("status")
                    name = account_name(step["account_label"])
                    action = f"{name} is done." if status == "done" else f"Update your {name}."
                    lines.append(action)
                if not result["feasible"]:
                    lines.append("Some changes may not be possible before your deadline.")
                return "\n\n".join(lines)
        if result.get("outcome") == "not_affected":
            return "This change doesn't put that account at risk based on how it lets you sign in and get back in."
        if result.get("outcome") in {"path", "lost_now", "recovery_lost"}:
            grouped = {}
            for step in result["steps"]:
                key = (step["to_id"], step.get("method_id", step.get("method_label")))
                grouped.setdefault(key, {"to": step["to_label"], "from": []})["from"].append(
                    step["from_label"]
                )
            lines = []
            for group in grouped.values():
                sources = join_names([account_name(label) for label in group["from"]])
                target = account_name(group["to"])
                if result["outcome"] == "recovery_lost":
                    lines.append(f"You use {sources} to get back into {target} if you forget your password.")
                elif result["outcome"] == "lost_now":
                    lines.append(f"You need {sources} to sign in to {target} this way.")
                else:
                    lines.append(f"Someone needs {sources} to get into {target} this way.")
            return "\n\n".join(lines) or "I couldn't trace how that account is affected."
        if "candidates" in result:
            labels = [escape(c["label"]) for c in result["candidates"] if c.get("label")]
            if not labels:
                return "Which email, number, or service do you mean?"
            choices = ", ".join(labels[:-1]) + " or " + labels[-1] if len(labels) > 1 else labels[0]
            return f"Do you mean {choices}?"
        if "account" in result:
            account = result["account"]
            name = account_name(account["display"]) if account.get("display") else "that account"
            return f"I found {name}. Which email or number are you changing?"
    return "Which email, number, or service would you like me to check?"


def approval_summary(plan: dict) -> str:
    if isinstance(plan, dict) and plan.get("outcome") == "memory":
        return "There's no plan waiting for approval. Ask me to plan the fix first."
    if not isinstance(plan, dict) or "steps" not in plan:
        return templated([plan])
    steps = [step for step in plan["steps"] if step["tier"] <= 3 and step["status"] == "proposed"]
    if not steps:
        return "There's nothing left to approve. You can check your plan for anything still to do."
    names = list(dict.fromkeys(account_name(step["account_label"]) for step in steps))
    return f"Ready to update {join_names(names)}.\n\nTap Approve all to start."
