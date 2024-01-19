# MatrixZulipBridge - an appservice puppeting bridge for Matrix - Zulip
#
# Copyright (C) 2024 Emma Meijere <emgh@em.id.lv>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Originally licensed under the MIT (Expat) license:
# <https://github.com/hifi/heisenbridge/blob/2532905f13835762870de55ba8a404fad6d62d81/LICENSE>.
#
# [This file includes modifications made by Emma Meijere]
#
#
import logging
import re
from abc import ABC
from collections import defaultdict
from typing import Callable
from typing import Optional

from mautrix.appservice import AppService as MauService
from mautrix.types import Membership
from mautrix.types.event.type import EventType
from mautrix.errors.base import IntentError

from matrixzulipbridge.appservice import AppService
from matrixzulipbridge.event_queue import EventQueue


class RoomInvalidError(Exception):
    pass


class Room(ABC):
    az: MauService
    id: str
    user_id: str
    serv: AppService
    members: list[str]
    lazy_members: Optional[dict[str, str]]
    bans: list[str]
    displaynames: dict[str, str]
    thread_last_message: dict[str, str]
    threads: dict[str, str]

    _mx_handlers: dict[str, list[Callable[[dict], bool]]]
    _queue: EventQueue

    def __init__(
        self,
        id: str,
        user_id: str,
        serv: AppService,
        members: list[str],
        bans: list[str],
    ):
        self.id = id
        self.user_id = user_id
        self.serv = serv
        self.members = list(members)
        self.bans = list(bans) if bans else []
        self.lazy_members = None
        self.displaynames = {}
        self.last_messages = defaultdict(str)
        self.thread_last_message = {}
        self.threads = {}

        self._mx_handlers = {}
        self._queue = EventQueue(self._flush_events)

        # start event queue
        if self.id:
            self._queue.start()

        # we track room members
        self.mx_register("m.room.member", self._on_mx_room_member)

        self.init()

    @classmethod
    def init_class(cls, az: MauService):
        cls.az = az

    async def post_init(self):
        pass

    def from_config(self, config: dict) -> None:
        if "threads" in config:
            self.threads = config["threads"]

    def init(self) -> None:
        pass

    def is_valid(self) -> bool:
        return True

    def cleanup(self):
        self._queue.stop()

    def to_config(self) -> dict:
        return {"threads": self.threads}

    async def save(self) -> None:
        config = self.to_config()
        config["type"] = type(self).__name__
        config["user_id"] = self.user_id
        await self.az.intent.set_account_data("zulip", config, self.id)

    def mx_register(self, type: str, func: Callable[[dict], bool]) -> None:
        if type not in self._mx_handlers:
            self._mx_handlers[type] = []

        self._mx_handlers[type].append(func)

    async def on_mx_event(self, event: dict) -> None:
        handlers = self._mx_handlers.get(str(event.type), [self._on_mx_unhandled_event])

        for handler in handlers:
            await handler(event)

    def in_room(self, user_id):
        return user_id in self.members

    async def on_mx_ban(self, user_id) -> None:
        pass

    async def on_mx_unban(self, user_id) -> None:
        pass

    async def on_mx_leave(self, user_id) -> None:
        pass

    async def _on_mx_unhandled_event(self, event: dict) -> None:
        pass

    async def _on_mx_room_member(self, event: dict) -> None:
        if (
            event.content.membership in [Membership.LEAVE, Membership.BAN]
            and event.state_key in self.members
        ):
            self.members.remove(event.state_key)
            if event.state_key in self.displaynames:
                del self.displaynames[event.state_key]
            if event.state_key in self.last_messages:
                del self.last_messages[event.state_key]

            if not self.is_valid():
                raise RoomInvalidError(
                    f"Room {self.id} ended up invalid after membership change, returning false from event handler."
                )

        if event.content.membership == Membership.LEAVE:
            if event.state_key in self.bans:
                self.bans.remove(event.state_key)
                await self.on_mx_unban(event.state_key)
            else:
                await self.on_mx_leave(event.state_key)

        if event.content.membership == Membership.BAN:
            if event.state_key not in self.bans:
                self.bans.append(event.state_key)

            await self.on_mx_ban(event.state_key)

        if event.content.membership == Membership.JOIN:
            if event.state_key not in self.members:
                self.members.append(event.state_key)

            if event.content.displayname is not None:
                self.displaynames[event.state_key] = str(event.content.displayname)
            elif event.state_key in self.displaynames:
                del self.displaynames[event.state_key]

    async def _join(self, user_id, nick=None):
        await self.az.intent.user(user_id).ensure_joined(self.id, ignore_cache=True)

        self.members.append(user_id)
        if nick is not None:
            self.displaynames[user_id] = nick

    async def _flush_events(self, events):
        for event in events:
            try:
                if event["type"] == "_join":
                    if event["user_id"] not in self.members:
                        await self._join(event["user_id"], event["nick"])
                elif event["type"] == "_leave":
                    if (
                        self.lazy_members is not None
                        and event["user_id"] in self.lazy_members
                    ):
                        del self.lazy_members[event["user_id"]]

                    if event["user_id"] in self.members:
                        if event["reason"] is not None:
                            await self.az.intent.user(event["user_id"]).kick_user(
                                self.id, event["user_id"], event["reason"]
                            )
                        else:
                            await self.az.intent.user(event["user_id"]).leave_room(
                                self.id
                            )
                        if event["user_id"] in self.members:
                            self.members.remove(event["user_id"])
                        if event["user_id"] in self.displaynames:
                            del self.displaynames[event["user_id"]]

                elif event["type"] == "_kick":
                    if event["user_id"] in self.members:
                        await self.az.intent.kick_user(
                            self.id, event["user_id"], event["reason"]
                        )
                        self.members.remove(event["user_id"])
                        if event["user_id"] in self.displaynames:
                            del self.displaynames[event["user_id"]]
                elif event["type"] == "_ensure_zulip_user_id":
                    await self.serv.ensure_zulip_user_id(
                        event["organization"],
                        zulip_user_id=event["zulip_user_id"],
                        zulip_user=event["zulip_user"],
                    )
                elif event["type"] == "_redact":
                    await self.az.intent.redact(
                        room_id=self.id,
                        event_id=event["event_id"],
                        reason=event["reason"],
                    )
                elif event["type"] == "_permission":
                    if len(event["content"]["users"]) == 0:
                        return  # No need to send an empty event
                    try:
                        await self.az.intent.set_power_levels(
                            room_id=self.id,
                            content=event["content"],
                        )
                    except IntentError:
                        pass
                elif "state_key" in event:
                    intent = self.az.intent

                    if event["user_id"]:
                        intent = intent.user(event["user_id"])

                    await intent.send_state_event(
                        self.id,
                        EventType.find(event["type"]),
                        state_key=event["state_key"],
                        content=event["content"],
                    )
                else:
                    bridge_data = event["content"].get("lv.shema.zulipbridge")
                    if bridge_data is None:
                        bridge_data = {}

                    if bridge_data.get("type") == "message":
                        thread_id = self.threads[bridge_data["zulip_topic"]]
                        event["content"]["m.relates_to"] = {
                            "event_id": thread_id,
                            "rel_type": "m.thread",
                        }
                        # https://spec.matrix.org/v1.9/client-server-api/#fallback-for-unthreaded-clients
                        if thread_id in self.thread_last_message:
                            event["content"]["m.relates_to"]["is_falling_back"] = True
                            event["content"]["m.relates_to"]["m.in_reply_to"] = {
                                "event_id": self.thread_last_message[thread_id]
                            }

                    intent = (
                        self.az.intent.user(event["user_id"])
                        if event["user_id"]
                        else self.az.intent
                    )

                    if "zulip_user_id" in bridge_data and "display_name" in bridge_data:
                        # TODO: Check if the display name is already cached
                        await intent.set_displayname(bridge_data["display_name"])

                    # Remove bridge data before sending it to Matrix
                    # This saves a few bytes!
                    event["content"].pop("lv.shema.zulipbridge", None)

                    event_type = EventType.find(event["type"])
                    event_id = await intent.send_message_event(
                        self.id, event_type, event["content"]
                    )
                    if (
                        "m.relates_to" in event["content"]
                        and event["content"]["m.relates_to"]["rel_type"] == "m.thread"
                    ):
                        self.thread_last_message[
                            event["content"]["m.relates_to"]["event_id"]
                        ] = event_id

                    match bridge_data.get("type"):
                        case "message":
                            # Is this efficient?
                            self.organization.messages[
                                bridge_data["zulip_message_id"]
                            ] = event_id
                            await self.organization.save()
                        case "topic":
                            self.threads[bridge_data["zulip_topic"]] = event_id
                            await self.save()

            except Exception:
                logging.exception("Queued event failed")

    # send message to mx user (may be puppeted)
    def send_message(
        self,
        text: str,
        user_id: Optional[str] = None,
        formatted=None,
        fallback_html: Optional[str] = None,
        thread_id: Optional[str] = None,
        custom_data: Optional[dict] = None,
    ) -> None:
        if formatted:
            event = {
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "format": "org.matrix.custom.html",
                    "body": text,
                    "formatted_body": formatted,
                },
                "user_id": user_id,
                "fallback_html": fallback_html,
            }
        else:
            event = {
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": text,
                },
                "user_id": user_id,
                "fallback_html": fallback_html,
            }

        if thread_id:
            event["content"]["m.relates_to"] = {
                "event_id": thread_id,
                "rel_type": "m.thread",
            }

        if custom_data is not None:
            event["content"]["lv.shema.zulipbridge"] = custom_data

        if "lv.shema.zulipbridge" in event["content"]:
            bridge_data: dict = event["content"]["lv.shema.zulipbridge"]
            if bridge_data["type"] == "message":
                self._ensure_thread_for_topic(bridge_data.copy(), user_id)

        self._queue.enqueue(event)

    def redact(self, event_id: str, reason: Optional[str] = None) -> None:
        event = {"type": "_redact", "event_id": event_id, "reason": reason}

        self._queue.enqueue(event)

    def _ensure_thread_for_topic(
        self, bridge_data: dict, mx_user_id: Optional[str] = None
    ) -> Optional[str]:
        zulip_topic = bridge_data["zulip_topic"]

        if zulip_topic in self.threads:
            return self.threads[zulip_topic]

        bridge_data["type"] = "topic"

        ## Create a new thread
        logging.debug(f"Creating new thread for topic: {zulip_topic}")
        # Send topic name as a message
        event = {
            "type": "m.room.message",
            "content": {
                "msgtype": "m.text",
                "body": zulip_topic,
                "lv.shema.zulipbridge": bridge_data,
            },
            "user_id": mx_user_id,
        }
        self._queue.enqueue(event)
        self.threads[zulip_topic] = None
        return None

    # send emote to mx user (may be puppeted)
    def send_emote(
        self,
        text: str,
        user_id: Optional[str] = None,
        fallback_html: Optional[str] = None,
    ) -> None:
        event = {
            "type": "m.room.message",
            "content": {
                "msgtype": "m.emote",
                "body": text,
            },
            "user_id": user_id,
            "fallback_html": fallback_html,
        }

        self._queue.enqueue(event)

    # send notice to mx user (may be puppeted)
    def send_notice(
        self,
        text: str,
        user_id: Optional[str] = None,
        formatted=None,
        fallback_html: Optional[str] = None,
    ) -> None:
        if formatted:
            event = {
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.notice",
                    "format": "org.matrix.custom.html",
                    "body": text,
                    "formatted_body": formatted,
                },
                "user_id": user_id,
                "fallback_html": fallback_html,
            }
        else:
            event = {
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.notice",
                    "body": text,
                },
                "user_id": user_id,
                "fallback_html": fallback_html,
            }

        self._queue.enqueue(event)

    # send notice to mx user (may be puppeted)
    def send_notice_html(self, text: str, user_id: Optional[str] = None) -> None:
        event = {
            "type": "m.room.message",
            "content": {
                "msgtype": "m.notice",
                "body": re.sub("<[^<]+?>", "", text),
                "format": "org.matrix.custom.html",
                "formatted_body": text,
            },
            "user_id": user_id,
        }

        self._queue.enqueue(event)

    def react(self, event_id: str, text: str, user_id: Optional[str] = None) -> None:
        event = {
            "type": "m.reaction",
            "content": {
                "m.relates_to": {
                    "event_id": event_id,
                    "key": text,
                    "rel_type": "m.annotation",
                }
            },
            "user_id": user_id,
        }

        self._queue.enqueue(event)

    def set_topic(self, topic: str, user_id: Optional[str] = None) -> None:
        event = {
            "type": "m.room.topic",
            "content": {
                "topic": topic,
            },
            "state_key": "",
            "user_id": user_id,
        }

        self._queue.enqueue(event)

    def join(self, user_id: str, nick=None, lazy=False) -> None:
        event = {
            "type": "_join",
            "content": {},
            "user_id": user_id,
            "nick": nick,
            "lazy": lazy,
        }

        self._queue.enqueue(event)

    def leave(self, user_id: str, reason: Optional[str] = None) -> None:
        event = {
            "type": "_leave",
            "content": {},
            "reason": reason,
            "user_id": user_id,
        }

        self._queue.enqueue(event)

    def rename(self, old_nick: str, new_nick: str) -> None:
        event = {
            "type": "_rename",
            "content": {},
            "old_nick": old_nick,
            "new_nick": new_nick,
        }

        self._queue.enqueue(event)

    def kick(self, user_id: str, reason: str) -> None:
        event = {
            "type": "_kick",
            "content": {},
            "reason": reason,
            "user_id": user_id,
        }

        self._queue.enqueue(event)

    def ensure_zulip_user_id(self, organization, zulip_user_id=None, zulip_user=None):
        event = {
            "type": "_ensure_zulip_user_id",
            "content": {},
            "organization": organization,
            "zulip_user": zulip_user,
            "zulip_user_id": zulip_user_id,
        }

        self._queue.enqueue(event)

    async def sync_permissions(self, permissions):
        room_power_levels = await self.az.intent.get_power_levels(
            self.id, ensure_joined=False
        )

        permissions = room_power_levels.users | permissions

        if permissions == room_power_levels.users:
            logging.debug(f"Nothing chnaged: {permissions=}")
            return  # Nothing changed

        self._queue.enqueue(
            {
                "type": "_permission",
                "content": {
                    "users": permissions,
                },
            }
        )
