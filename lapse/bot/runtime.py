from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import logging
import re
from collections import defaultdict, deque
from contextlib import suppress
from dataclasses import dataclass
from html import escape, unescape
from importlib import import_module
from secrets import token_urlsafe
from time import monotonic
from urllib.parse import quote, urlsplit

from openai import OpenAIError
from redis.exceptions import RedisError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError

from lapse.bot import actions, cards
from lapse.contracts import ChangeStatus, GraphEvent, RootKind, RootStatus, ScenarioKind, ScenarioRequest
from lapse.graph import planner, scenarios, views

logger = logging.getLogger(__name__)


def html_chunks(text: str) -> list[str]:
    chunks, parts, size, bold = [], [], 0, False
    for token in re.split(r"(</?b>)", text or "I couldn't find an answer. Please try again."):
        if token in {"<b>", "</b>"}:
            opening = token == "<b>"
            if opening != bold:
                parts.append(token)
                bold = opening
            continue
        token = unescape(token)
        while token:
            piece, token = token[: 2000 - size], token[2000 - size :]
            parts.append(escape(piece))
            size += len(piece)
            if size == 2000:
                chunks.append("".join(parts) + ("</b>" if bold else ""))
                parts, size = (["<b>"] if bold else []), 0
    if size or not chunks:
        chunks.append("".join(parts) + ("</b>" if bold else ""))
    return chunks


async def invoke(function, *args, **kwargs):
    if inspect.iscoroutinefunction(function):
        return await function(*args, **kwargs)
    result = await asyncio.to_thread(function, *args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def dependency(name: str):
    return import_module(f"lapse.{name}")


@dataclass(frozen=True)
class PendingOTP:
    uid: str
    change_id: str
    service: str
    channel: str
    expires_at: float


class BotRuntime:
    def __init__(self, redis, *, web_url="http://localhost:8080", default_uid=None, slow_after=1.5):
        self.redis = redis
        self.web_url = web_url.rstrip("/")
        self.default_uid = default_uid
        self.slow_after = slow_after
        self.pending: dict[int, deque[PendingOTP]] = defaultdict(deque)
        self.conversations: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.otp_relays: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.delivery_task = None

    def web_link(self, path: str) -> str:
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or not path.startswith("/") or "\\" in path:
            raise ValueError("Web button must use a local path")
        return self.web_url + path

    def button_link(self, path: str) -> str:
        """Telegram rejects the name "localhost" in URL buttons but accepts 127.0.0.1."""
        parts = urlsplit(self.web_link(path))
        if (parts.hostname or "").casefold() == "localhost":
            netloc = "127.0.0.1" + (f":{parts.port}" if parts.port else "")
            parts = parts._replace(netloc=netloc)
        return parts.geturl()

    @property
    def web_is_public(self) -> bool:
        """Whether Telegram will accept URL buttons for the configured web host."""
        host = (urlsplit(self.web_url).hostname or "").casefold()
        if host == "localhost":
            return True
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return "." in host and not host.endswith((".local", ".localhost", ".internal"))
        return True

    async def keyboard(self, uid: str, chat_id: int, buttons: list[dict]):
        rows = []
        for button in buttons:
            action, payload = button["action"], button["payload"]
            if action == "open_web":
                link = self.button_link(payload["path"])
                if not self.web_is_public:
                    continue
                item = InlineKeyboardButton(button["label"], url=link)
            elif action == "open_url":
                url = payload["url"]
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or "\\" in url:
                    raise ValueError("External button must use an HTTPS URL")
                item = InlineKeyboardButton(button["label"], url=url)
            else:
                if action not in {
                    "approve_changes",
                    "plan_root",
                    "confirm_use",
                    "dismiss_signal",
                    "check_change",
                }:
                    raise ValueError("Unsupported button action")
                callback_id = "cb:" + token_urlsafe(12)
                await self.redis.set(
                    f"lapse:bot:callback:{uid}:{chat_id}:{callback_id}",
                    json.dumps({"action": action, "payload": payload}),
                    ex=900,
                )
                item = InlineKeyboardButton(button["label"], callback_data=callback_id)
            rows.append([item])
        return InlineKeyboardMarkup(rows) if rows else None

    async def send(self, bot, uid, chat_id, text, buttons=(), *, edit=None):
        keyboard = await self.keyboard(uid, chat_id, buttons)
        if not self.web_is_public:
            links = [
                f"{escape(button['label'])}: {escape(self.web_link(button['payload']['path']))}"
                for button in buttons
                if button["action"] == "open_web"
            ]
            if links:
                text = text + "\n\n" + "\n".join(links)
        # Telegram counts UTF-16 units after parsing HTML; 2000 code points fit even with emoji.
        chunks = html_chunks(text)
        for index, chunk in enumerate(chunks):
            kwargs = {
                "text": chunk,
                "parse_mode": ParseMode.HTML,
                "reply_markup": keyboard if index == len(chunks) - 1 else None,
            }
            if index == 0 and edit is not None:
                await edit.edit_text(**kwargs)
            else:
                await bot.send_message(chat_id=chat_id, **kwargs)

    async def uid(self, update, context):
        if update.effective_chat.type != "private":
            await update.effective_message.reply_text("Please message me in a private chat.")
            return None
        link = dependency("telegram_link")
        uid = await invoke(link.uid_for, update.effective_chat.id)
        if uid is None and self.default_uid:
            uid = await invoke(link.claim_default, self.default_uid, update.effective_chat.id)
        if uid is None:
            await self.send(context.bot, "", update.effective_chat.id, self.onboarding())
        return uid

    def onboarding(self):
        return (
            f"Let's connect this chat.\n\nOpen {escape(self.web_url)}/onboarding and tap Get link code, "
            "then send /start followed by your code here."
        )

    async def start(self, update, context):
        chat = update.effective_chat
        if chat.type != "private":
            await update.effective_message.reply_text("Please message me in a private chat.")
            return
        link = dependency("telegram_link")
        try:
            if context.args:
                if len(context.args) != 1:
                    raise ValueError("Expected one link code")
                uid = await invoke(link.bind, context.args[0], chat.id)
            else:
                uid = await invoke(link.uid_for, chat.id)
                if uid is None and self.default_uid:
                    uid = await invoke(link.claim_default, self.default_uid, chat.id)
        except ValueError:
            await self.send(
                context.bot, "", chat.id, "That link code is invalid or expired. " + self.onboarding()
            )
            return
        if uid is None:
            await self.send(context.bot, "", chat.id, self.onboarding())
            return
        self.pending.pop(chat.id, None)
        await self.send(
            context.bot,
            uid,
            chat.id,
            "You're connected ✅\n\nTell me which number or email you're giving up, or use /risk to see what needs attention.",
        )

    async def help(self, update, context):
        await update.effective_message.reply_text(
            "/start <code> · Connect from web onboarding\n"
            "/status · What still needs doing\n"
            "/risk [number or email] · What could go wrong when it changes\n"
            "/help · This help\n"
            "Or tell me what you’re changing. I’ll ask before changing an account."
        )

    async def status(self, update, context):
        if (uid := await self.uid(update, context)) is None:
            return
        roots = await invoke(views.list_roots, uid)
        plans = await invoke(planner.list_plans, uid)
        signals = await invoke(actions.list_signals, uid)
        await self.send(context.bot, uid, update.effective_chat.id, cards.status_card(roots, plans, signals))

    async def risk(self, update, context):
        if (uid := await self.uid(update, context)) is None:
            return
        from lapse.agent import guard, tools

        roots = await invoke(views.list_roots, uid)
        hint = " ".join(context.args).strip()
        buttons = []
        if hint:
            resolved = await invoke(tools.resolve, uid, hint)
            candidates = resolved.get("candidates", [])
            if len(candidates) != 1:
                choices = candidates or [
                    {"id": root.id, "label": root.label, "kind": "root"} for root in roots
                ]
                by_id = {root.id: root for root in roots}
                names = [
                    cards.root_name(by_id[c["id"]]) if c["id"] in by_id else cards.account_name(c["label"])
                    for c in choices
                ]
                text = (
                    "Which one do you mean: " + cards.join_names(names) + "?"
                    if names
                    else ("Which phone number, email, or service do you mean?")
                )
            else:
                candidate = candidates[0]
                if candidate["kind"] == "account":
                    detail = await invoke(
                        tools.dispatch, uid, "account_info", {"account_id": candidate["id"]}
                    )
                    if detail.get("outcome"):
                        text = guard.templated([detail])
                    else:
                        changing = [
                            root for root in roots if root.status in {RootStatus.retiring, RootStatus.closing}
                        ]
                        items = [(root, await self.root_risk(uid, root)) for root in changing]
                        text = cards.account_risk_card(detail["account"], items)
                else:
                    root = next(root for root in roots if root.id == candidate["id"])
                    if root.status == RootStatus.active:
                        text = cards.kept_card(root)
                    else:
                        result = await self.root_risk(uid, root)
                        accounts = (
                            await invoke(actions.list_accounts, uid) if root.kind == RootKind.email else []
                        )
                        text = cards.scenario_card(root, result, accounts=accounts)
                        buttons = self.risk_buttons(root, result)
        else:
            changing = [root for root in roots if root.status in {RootStatus.retiring, RootStatus.closing}]
            items = [(root, await self.root_risk(uid, root)) for root in changing]
            text = cards.risk_summary(items)
            buttons = [
                {
                    "label": "Fix " + cards.short_root(root),
                    "action": "plan_root",
                    "payload": {"root_id": root.id},
                }
                for root, _ in items
            ]
        await self.send(context.bot, uid, update.effective_chat.id, text, buttons)

    async def root_risk(self, uid, root):
        kind = (
            ScenarioKind.lockout if root.kind in {RootKind.email, RootKind.device} else ScenarioKind.takeover
        )
        return await invoke(scenarios.run_scenario, uid, ScenarioRequest(kind=kind, root_ids=[root.id]))

    def risk_buttons(self, root, result):
        return [
            {"label": "Fix it for me", "action": "plan_root", "payload": {"root_id": root.id}},
            {
                "label": "Show me",
                "action": "open_web",
                "payload": {"path": f"/graph?scenario={quote(result.id, safe='')}"},
            },
        ]

    async def text(self, update, context):
        if (uid := await self.uid(update, context)) is None:
            return
        chat_id = update.effective_chat.id
        text = update.effective_message.text.strip()
        if self.pending.get(chat_id) and re.fullmatch(r"[0-9]{4,8}", text):
            async with self.otp_relays[chat_id]:
                await self.relay_otp(update, context, uid, text)
            return
        async with self.conversations[chat_id]:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            task = asyncio.create_task(
                invoke(dependency("agent.agent").respond, uid, text, channel="telegram")
            )
            working = None
            try:
                done, _ = await asyncio.wait({task}, timeout=self.slow_after)
                if not done:
                    working = await context.bot.send_message(chat_id=chat_id, text="Working on it…")
                while not task.done():
                    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                    await asyncio.wait({task}, timeout=4)
                reply = task.result()
                text = reply["text"] if isinstance(reply, dict) else reply.text
                buttons = reply["buttons"] if isinstance(reply, dict) else reply.buttons
                await self.send(context.bot, uid, chat_id, text, buttons, edit=working)
            except (
                OpenAIError,
                TelegramError,
                RedisError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                OSError,
            ) as exc:
                logger.warning("Telegram reply failed: %s", type(exc).__name__, exc_info=exc)
                await self.send(
                    context.bot,
                    uid,
                    chat_id,
                    "I couldn't finish that request. Please try again.",
                    edit=working,
                )
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    if not task.done():
                        await task

    async def callback(self, update, context):
        query = update.callback_query
        await query.answer()
        if (uid := await self.uid(update, context)) is None:
            return
        chat_id = update.effective_chat.id
        if not isinstance(query.data, str) or not re.fullmatch(r"cb:[A-Za-z0-9_-]{16}", query.data):
            await query.edit_message_text("That button is invalid.")
            return
        stored = await self.redis.getdel(f"lapse:bot:callback:{uid}:{chat_id}:{query.data}")
        if stored is None:
            await query.edit_message_text("That button expired or was already used. Ask me for a fresh one.")
            return
        record = json.loads(stored)
        action, payload = record["action"], record["payload"]
        buttons = []
        if action == "approve_changes":
            approvals = dependency("approvals")
            token = payload.get("token")
            if token is None:
                token = await invoke(approvals.mint, uid, payload["change_ids"])
            change_ids = await invoke(approvals.consume, uid, token)
            approved = []
            for change_id in change_ids:
                change = await invoke(planner.update_change, uid, change_id, status=ChangeStatus.approved)
                await invoke(dependency("jobs").enqueue, uid, "execute_change", {"change_id": change_id})
                approved.append(change)
            automatic = [change for change in approved if change.tier <= 2]
            assisted = [change for change in approved if change.tier == 3]
            messages = []
            if automatic:
                messages.append(
                    "I'll handle "
                    + cards.join_names(cards.account_name(c.account_label) for c in automatic)
                    + " now and ping you when it's done."
                )
            if assisted:
                messages.append(
                    "I'll send you the link for "
                    + cards.join_names(cards.account_name(c.account_label) for c in assisted)
                    + " and watch for confirmation once you've changed it."
                )
            text = "\n\n".join(messages)
        elif action == "plan_root":
            roots = await invoke(views.list_roots, uid)
            root = next((root for root in roots if root.id == payload["root_id"]), None)
            replacement = payload.get("new_root_id")
            options = [
                other
                for other in roots
                if root
                and other.id != root.id
                and other.kind == root.kind
                and other.status == RootStatus.active
            ]
            if replacement is None and len(options) == 1:
                replacement = options[0].id
            elif replacement is None and len(options) > 1:
                await self.send(
                    context.bot,
                    uid,
                    chat_id,
                    "Which one should I switch to: "
                    + cards.join_names(cards.root_name(r) for r in options)
                    + "?",
                    [
                        {
                            "label": f"Switch to {cards.root_name(option)}",
                            "action": "plan_root",
                            "payload": {"root_id": root.id, "new_root_id": option.id},
                        }
                        for option in options
                    ],
                    edit=query.message,
                )
                return
            plan = await invoke(planner.plan_for_root, uid, payload["root_id"], replacement)
            result = await invoke(
                scenarios.run_scenario, uid, ScenarioRequest(kind=plan.kind, root_ids=[plan.old_root])
            )
            text = cards.plan_card(plan, result, roots=roots)
            ids = [step.id for step in plan.steps if step.tier <= 3 and step.status == ChangeStatus.proposed]
            if ids:
                buttons.append(
                    {
                        "label": "Approve all",
                        "action": "approve_changes",
                        "payload": {"change_ids": ids},
                    }
                )
            if len(plan.steps) == 1 and plan.steps[0].tier == 3 and plan.steps[0].settings_url:
                buttons.append(
                    {
                        "label": "Open the link",
                        "action": "open_url",
                        "payload": {"url": plan.steps[0].settings_url},
                    }
                )
            else:
                buttons.append(
                    {
                        "label": "Show me",
                        "action": "open_web",
                        "payload": {"path": f"/plans/{quote(plan.id, safe='')}"},
                    }
                )
        elif action == "check_change":
            await invoke(dependency("exec.verify").verify_pending, uid)
            change = await invoke(actions.get_change, uid, payload["change_id"])
            text = (
                f"Done ✅ I've confirmed the change to {cards.account_name(change.account_label)}."
                if change.status == ChangeStatus.done
                else "I haven't seen the confirmation yet. I'll keep checking your email."
            )
        elif action == "confirm_use":
            await invoke(
                actions.confirm_use,
                uid,
                payload["account_id"],
                payload["root_id"],
                payload["role"],
                payload["value"],
            )
            text = (
                "Confirmed and rechecked that account."
                if payload["value"]
                else "Removed that connection and rechecked the account."
            )
        elif action == "dismiss_signal":
            await invoke(actions.dismiss_signal, uid, payload["signal_id"])
            text = "Got it. I’ve dismissed that reminder."
        else:
            raise ValueError("Unsupported callback")
        await self.send(context.bot, uid, chat_id, text, buttons, edit=query.message)

    async def prompt_otp(self, bot, chat_id):
        queue = self.pending[chat_id]
        while queue and queue[0].expires_at <= monotonic():
            queue.popleft()
        if queue:
            pending = queue[0]
            await self.send(
                bot,
                pending.uid,
                chat_id,
                f"{cards.account_name(pending.service)} sent a code to {escape(pending.channel.removeprefix('sms:'))}.\n\nReply with it here before it expires.",
            )

    async def relay_otp(self, update, context, uid, code):
        chat_id = update.effective_chat.id
        queue = self.pending[chat_id]
        pending = queue[0] if queue else None
        try:
            if pending and pending.uid == uid and pending.expires_at > monotonic():
                await invoke(dependency("otp").submit, uid, pending.change_id, code)
                self.pending[chat_id] = deque(item for item in self.pending[chat_id] if item is not pending)
                reply = "Got it. I’m using that code now."
            else:
                if queue:
                    queue.popleft()
                reply = "That code request has expired. Try the change again to get a new one."
        finally:
            try:
                await context.bot.delete_message(
                    chat_id=chat_id, message_id=update.effective_message.message_id
                )
            except TelegramError:
                await self.send(
                    context.bot,
                    uid,
                    chat_id,
                    "I couldn't delete your code message. Please delete it in Telegram.",
                )
        await self.send(context.bot, uid, chat_id, reply)
        await self.prompt_otp(context.bot, chat_id)

    async def deliver(self, bot, channel: str, data: dict):
        prefix, _, uid = channel.removeprefix("lapse:").partition(":")
        prefix = f"lapse:{prefix}"
        if prefix not in {"lapse:notify", "lapse:events"}:
            return
        link = dependency("telegram_link")
        chat_id = await invoke(link.chat_for, uid)
        if chat_id is None or chat_id <= 0 or await invoke(link.uid_for, chat_id) != uid:
            return
        if prefix == "lapse:notify":
            await self.send(bot, uid, chat_id, data["text"], data.get("buttons", []))
            return
        event = GraphEvent.model_validate(data)
        if event.uid != uid or event.type != "exec":
            return
        step = event.data
        if step["kind"] == "need_otp":
            change = await invoke(actions.get_change, uid, step["change_id"])
            queue = self.pending[chat_id] = deque(
                item for item in self.pending[chat_id] if item.uid == uid and item.expires_at > monotonic()
            )
            if any(item.change_id == change.id and item.uid == uid for item in queue):
                return
            queue.append(
                PendingOTP(
                    uid,
                    change.id,
                    change.account_label,
                    step.get("channel") or "your device",
                    monotonic() + 300,
                )
            )
            if len(queue) == 1:
                await self.prompt_otp(bot, chat_id)
        elif step["kind"] in {"done", "failed"}:
            queue = self.pending[chat_id]
            was_current = queue and queue[0].change_id == step["change_id"]
            self.pending[chat_id] = deque(item for item in queue if item.change_id != step["change_id"])
            if was_current:
                await self.prompt_otp(bot, chat_id)

    async def listen(self, bot):
        while True:
            try:
                async with self.redis.pubsub() as pubsub:
                    await pubsub.psubscribe("lapse:notify:*", "lapse:events:*")
                    async for message in pubsub.listen():
                        if message["type"] != "pmessage":
                            continue
                        try:
                            await self.deliver(bot, message["channel"], json.loads(message["data"]))
                        except (
                            ValueError,
                            TypeError,
                            KeyError,
                            ImportError,
                            TelegramError,
                            RedisError,
                            OSError,
                        ):
                            logger.warning(
                                "Bot delivery failed; check service availability and message schema."
                            )
            except (RedisError, OSError):
                logger.warning("Bot subscription disconnected; reconnecting.")
                await asyncio.sleep(1)

    async def startup(self, application):
        self.delivery_task = asyncio.create_task(self.listen(application.bot))

    async def shutdown(self, application):
        if self.delivery_task:
            self.delivery_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.delivery_task
        await self.redis.aclose()

    async def error(self, update, context):
        if update is None or update.effective_chat is None:
            return
        logger.warning("Bot handler failed: %s", type(context.error).__name__, exc_info=context.error)
        text = "I couldn't finish that request. Check /status before retrying any approval."
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text)
