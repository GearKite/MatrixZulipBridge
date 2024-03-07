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
import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from bidict import bidict
from mautrix.types import MessageType
from zulip_emoji_mapping import EmojiNotFoundException, ZulipEmojiMapping

from matrixzulipbridge.command_parse import (
    CommandManager,
    CommandParser,
    CommandParserError,
)
from matrixzulipbridge.room import InvalidConfigError
from matrixzulipbridge.under_organization_room import UnderOrganizationRoom, connected

if TYPE_CHECKING:
    import zulip
    from mautrix.types import (
        EventID,
        MessageEvent,
        ReactionEvent,
        RedactionEvent,
        UserID,
    )

    from matrixzulipbridge.organization_room import OrganizationRoom
    from matrixzulipbridge.types import ZulipMessageID, ZulipUserID


class DirectRoom(UnderOrganizationRoom):
    name: str
    media: list[list[str]]
    recipient_ids: list["ZulipUserID"]
    max_backfill_amount: int
    lazy_members: dict
    messages: bidict["ZulipMessageID", "EventID"]
    reactions: bidict["EventID", frozenset]

    commands: CommandManager

    def init(self) -> None:
        super().init()

        self.name = None
        self.media = []
        self.recipient_ids = []
        self.max_backfill_amount = None
        self.messages = bidict()
        self.reactions = bidict()

        self.commands = CommandManager()

        cmd = CommandParser(
            prog="BACKFILL",
            description="set the maximum amount of backfilled messages (0 to disable backfilling)",
        )
        cmd.add_argument("amount", nargs="?", help="new amount")
        cmd.add_argument("--now", action="store_true", help="start backfilling now")
        self.commands.register(cmd, self.cmd_backfill)

        self.mx_register("m.room.message", self.on_mx_message)
        self.mx_register("m.room.redaction", self.on_mx_redaction)
        self.mx_register("m.reaction", self.on_mx_reaction)

    def from_config(self, config: dict) -> None:
        super().from_config(config)

        if "name" not in config:
            raise InvalidConfigError("No name key in config for ChatRoom")

        self.name = config["name"]

        if "media" in config:
            self.media = config["media"]

        if "max_backfill_amount" in config:
            self.max_backfill_amount = config["max_backfill_amount"]

        if "recipient_ids" in config:
            self.recipient_ids = config["recipient_ids"]

        if "messages" in config and config["messages"]:
            self.messages = bidict(config["messages"])

        if "reactions" in config and config["reactions"]:
            self.reactions = bidict(
                {
                    k: frozenset({l[0]: l[1] for l in v}.items())
                    for k, v in config["reactions"].items()
                }
            )

    def to_config(self) -> dict:
        return {
            **(super().to_config()),
            "name": self.name,
            "organization_id": self.organization_id,
            "media": self.media[:5],
            "max_backfill_amount": self.max_backfill_amount,
            "recipient_ids": self.recipient_ids,
            "messages": dict(self.messages),
            "reactions": {k: list(v) for k, v in self.reactions.items()},
        }

    @staticmethod
    async def create(
        organization: "OrganizationRoom",
        zulip_recipients: dict,
    ) -> "DirectRoom":
        logging.debug(
            f"DirectRoom.create(organization='{organization.name}', recipients='{zulip_recipients}'"
        )
        mx_recipients = []
        for user in zulip_recipients:
            if str(user["id"]) in organization.zulip_puppet_user_mxid:
                mxid = organization.zulip_puppet_user_mxid[str(user["id"])]

            else:
                mxid = organization.serv.get_mxid_from_zulip_user_id(
                    organization, user["id"]
                )
                if "full_name" in user:
                    await organization.serv.cache_user(mxid, user["full_name"])

            mx_recipients.append(mxid)

        room = DirectRoom(
            None,
            organization.user_id,
            organization.serv,
            mx_recipients,
            [],
        )
        room.name = ", ".join([user["full_name"] for user in zulip_recipients])
        room.organization = organization
        room.organization_id = organization.id
        room.max_backfill_amount = organization.max_backfill_amount

        room.recipient_ids = [user["id"] for user in zulip_recipients]

        organization.serv.register_room(room)

        recipient_ids = frozenset(room.recipient_ids)
        organization.direct_rooms[recipient_ids] = room

        asyncio.ensure_future(room.create_mx(mx_recipients))
        return room

    async def create_mx(self, user_mxids: list["UserID"]) -> None:
        if self.id is None:
            self.id = await self.organization.serv.create_room(
                f"{self.name} ({self.organization.name})",
                f"Direct messages with {self.name} from {self.organization.name}",
                user_mxids,
                is_direct=True,
            )
            self.serv.register_room(self)

            for user_mxid in user_mxids:
                if self.serv.is_puppet(user_mxid):
                    await self.az.intent.user(user_mxid).ensure_joined(self.id)

            await self.save()
            # start event queue now that we have an id
            self._queue.start()

            # attach to organization space
            if self.organization.space:
                await self.organization.space.attach(self.id)

    def is_valid(self) -> bool:
        if self.organization_id is None:
            return False

        if self.name is None:
            return False

        if len(self.recipient_ids) == 0:
            return False

        return True

    def cleanup(self) -> None:
        logging.debug(f"Cleaning up organization connected room {self.id}.")

        # cleanup us from organization space if we have it
        if self.organization and self.organization.space:
            asyncio.ensure_future(self.organization.space.detach(self.id))

        # cleanup us from organization rooms
        if self.organization and self.name in self.organization.rooms:
            logging.debug(
                f"... and we are attached to organization {self.organization.id}, detaching."
            )
            del self.organization.rooms[self.name]
        super().cleanup()

    def send_notice(
        self,
        text: str,
        user_id: Optional["UserID"] = None,
        formatted=None,
        fallback_html: Optional[str] = None,
        forward=False,
    ):
        if (self.force_forward or forward) and user_id is None:
            self.organization.send_notice(
                text=f"{self.name}: {text}",
                formatted=formatted,
                fallback_html=fallback_html,
            )
        else:
            super().send_notice(
                text=text,
                user_id=user_id,
                formatted=formatted,
                fallback_html=fallback_html,
            )

    def send_notice_html(
        self, text: str, user_id: Optional["UserID"] = None, forward=False
    ) -> None:
        if (self.force_forward or forward) and user_id is None:
            self.organization.send_notice_html(text=f"{self.name}: {text}")
        else:
            super().send_notice_html(text=text, user_id=user_id)

    @connected
    async def on_mx_message(self, event: "MessageEvent") -> None:
        await self.check_if_nobody_left()

        sender = str(event.sender)
        (name, server) = sender.split(":", 1)

        # ignore self messages
        if sender == self.serv.user_id:
            return

        # prevent re-sending federated messages back
        if (
            name.startswith("@" + self.serv.puppet_prefix)
            and server == self.serv.server_name
        ):
            return

        if event.content.msgtype.is_media or event.content.msgtype in (
            MessageType.EMOTE,
            MessageType.TEXT,
            MessageType.NOTICE,
        ):
            await self._relay_message(event)

        await self.az.intent.send_receipt(event.room_id, event.event_id)

    @connected
    async def on_mx_redaction(self, event: "RedactionEvent"):
        event_id = event.redacts

        client = self.organization.zulip_puppets.get(event.sender)

        if event_id in self.messages.inverse:
            zulip_message_id = self.messages.inverse[event_id]
            result = client.delete_message(zulip_message_id)
            del self.messages.inverse[event_id]
        elif event_id in self.reactions:
            reaction = {i[0]: i[1] for i in self.reactions[event_id]}
            request = {
                "message_id": reaction["message_id"],
                "emoji_name": reaction["emoji_name"],
            }
            result = client.remove_reaction(request)
            zulip_user_id = self.organization.zulip_puppet_user_mxid.inverse[
                event.sender
            ]
            request["user_id"] = str(zulip_user_id)
            frozen_request = frozenset(request.items())
            del self.reactions.inverse[frozen_request]
        else:
            return

        if result["result"] != "success":
            logging.debug(f"Couldn't redact event on Zulip: {result['msg']}")

    @connected
    async def on_mx_reaction(self, event: "ReactionEvent"):
        client = self.organization.zulip_puppets.get(event.sender)
        # This only works for logged in users
        if not client:
            return
        if event.content.relates_to.rel_type.value != "m.annotation":
            return

        zulip_user_id = self.organization.zulip_puppet_user_mxid.inverse[event.sender]

        reaction = event.content.relates_to.key

        try:
            emoji_name = ZulipEmojiMapping.get_emoji_name(reaction)
        except EmojiNotFoundException:
            emoji_name = reaction

        event_id = event.content.relates_to.event_id

        zulip_message_id = self.messages.inverse.get(event_id)
        if not zulip_message_id:
            logging.error(
                f"Could not find a message to react to for {event_id}. Was it sent to Zulip?"
            )
            return

        request = {
            "message_id": zulip_message_id,
            "emoji_name": emoji_name,
        }

        result = client.add_reaction(request)
        if result["result"] != "success":
            logging.debug(f"Failed adding reaction {emoji_name} to {zulip_message_id}!")
            return

        request["user_id"] = str(zulip_user_id)
        frozen_request = frozenset(request.items())

        if frozen_request in self.reactions.inverse:
            del self.reactions.inverse[frozen_request]
        self.reactions[event.event_id] = frozen_request

    async def _relay_message(self, event: "MessageEvent"):
        prefix = ""
        client = self.organization.zulip_puppets.get(event.sender)
        if not client:
            logging.error(
                f"Matrix user ({event.sender}) sent a DM without having logged in to Zulip"
            )
            return

        # try to find out if this was a reply
        reply_to = None
        if event.content.get_reply_to():
            rel_event = event

            # traverse back all edits
            while rel_event.content.get_edit():
                rel_event = await self.az.intent.get_event(
                    self.id, rel_event.content.get_edit()
                )

            # see if the original is a reply
            if rel_event.content.get_reply_to():
                reply_to = await self.az.intent.get_event(
                    self.id, rel_event.content.get_reply_to()
                )

        # keep track of the last message
        self.last_messages[event.sender] = event
        message = await self._process_event_content(event, prefix, reply_to)

        request = {
            "type": "private",
            "to": self.recipient_ids,
            "content": message,
        }

        result = client.send_message(request)
        if result["result"] != "success":
            logging.error(f"Failed sending message to Zulip: {result['msg']}")
            return

        self.messages[str(result["id"])] = event.event_id
        await self.organization.save()
        await self.save()

    async def _flush_event(self, event: dict):
        if event["type"] == "_zulip_react":
            intent = self.az.intent.user(event["user_id"])
            message_event_id = event["event_id"]

            request = {
                "message_id": event["zulip_message_id"],
                "emoji_name": event["zulip_emoji_name"],
                "user_id": event["zulip_user_id"],
            }
            frozen_request = frozenset(request.items())

            # Check if this reaction has already been relayed
            if self.reactions.inverse.get(frozen_request) is not None:
                return

            event_id = await intent.react(self.id, message_event_id, event["key"])

            self.reactions[event_id] = frozen_request
            await self.save()
        else:
            await super()._flush_event(event)

    def relay_zulip_react(
        self,
        user_id: "UserID",
        event_id: "EventID",
        key: str,
        zulip_message_id: "ZulipMessageID",
        zulip_emoji_name: str,
        zulip_user_id: "ZulipUserID",
    ):
        self._queue.enqueue(
            {
                "type": "_zulip_react",
                "user_id": user_id,
                "event_id": event_id,
                "key": key,
                "zulip_message_id": zulip_message_id,
                "zulip_emoji_name": zulip_emoji_name,
                "zulip_user_id": zulip_user_id,
            }
        )

    async def check_if_nobody_left(self):
        """Invite back everyone who left"""
        mx_recipients = []
        for user_id in self.recipient_ids:
            if str(user_id) not in self.organization.zulip_puppet_user_mxid:
                continue
            mx_recipients.append(self.organization.zulip_puppet_user_mxid[str(user_id)])
        for mxid in mx_recipients:
            if mxid in self.members:
                continue
            await self.az.intent.invite_user(self.id, mxid)

    async def cmd_backfill(self, args) -> None:
        if args.amount:
            self.max_backfill_amount = int(args.amount)
            await self.save()
        self.send_notice(
            f"Maximum backfill amount is set to: {self.max_backfill_amount}"
        )
        if args.now:
            await self.backfill_messages()

    async def cmd_upgrade(self, args) -> None:
        if not args.undo:
            await self._attach_space()

    async def backfill_messages(self):
        if not self.organization.max_backfill_amount:
            return
        request = {
            "anchor": "newest",
            "num_before": self.organization.max_backfill_amount,
            "num_after": 0,
            "narrow": [
                {"operator": "dm", "operand": self.recipient_ids},
            ],
        }

        client = self.get_any_zulip_client()
        if client is None:
            return

        result = client.get_messages(request)

        if result["result"] != "success":
            logging.error(f"Failed getting Zulip messages: {result['msg']}")
            return

        for message in result["messages"]:
            if str(message["id"]) in self.messages:
                continue
            if str(message["id"]) in self.organization.messages:
                continue
            self.organization.dm_message(message)

    def get_any_zulip_client(self) -> "zulip.Client":
        for recipient_id in self.recipient_ids:
            mxid = self.organization.zulip_puppet_user_mxid.get(str(recipient_id))
            if not mxid:
                continue
            client = self.organization.zulip_puppets.get(mxid)
            if client is None:
                continue
            return client
        return None
