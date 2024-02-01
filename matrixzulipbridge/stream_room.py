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

from mautrix.types import MessageType

from matrixzulipbridge.command_parse import CommandParser
from matrixzulipbridge.direct_room import DirectRoom
from matrixzulipbridge.room import InvalidConfigError
from matrixzulipbridge.under_organization_room import connected

if TYPE_CHECKING:
    from matrixzulipbridge.organization_room import OrganizationRoom


class StreamRoom(DirectRoom):
    """Puppeting room for Zulip stream."""

    key: Optional[str]
    member_sync: str
    names_buffer: list[str]

    use_displaynames = True
    allow_notice = False
    force_forward = True
    topic_sync = None

    stream_id: Optional[int]
    stream_name: Optional[str]

    def init(self) -> None:
        super().init()

        self.key = None
        self.autocmd = None

        self.stream_id = None
        self.stream_name = None

        # for migration the class default is full
        self.member_sync = "full"

        cmd = CommandParser(
            prog="SYNC",
            description="override Zulip member sync type for this room",
            epilog="Note: To force full sync after setting to full, use the NAMES command",
        )
        group = cmd.add_mutually_exclusive_group()
        group.add_argument(
            "--lazy",
            help="set lazy sync, members are added when they talk",
            action="store_true",
        )
        group.add_argument(
            "--half",
            help="set half sync, members are added when they join or talk",
            action="store_true",
        )
        group.add_argument(
            "--full",
            help="set full sync, members are fully synchronized",
            action="store_true",
        )
        group.add_argument(
            "--off",
            help="disable member sync completely, the bridge will relay all messages, may be useful during spam attacks",
            action="store_true",
        )
        self.commands.register(cmd, self.cmd_sync)

        cmd = CommandParser(
            prog="UPGRADE",
            description="Perform any potential bridge-side upgrades of the room",
        )
        cmd.add_argument(
            "--undo", action="store_true", help="undo previously performed upgrade"
        )
        self.commands.register(cmd, self.cmd_upgrade)

        cmd = CommandParser(
            prog="DISPLAYNAMES",
            description="enable or disable use of displaynames in relayed messages",
        )
        cmd.add_argument(
            "--enable", dest="enabled", action="store_true", help="Enable displaynames"
        )
        cmd.add_argument(
            "--disable",
            dest="enabled",
            action="store_false",
            help="Disable displaynames (fallback to MXID)",
        )
        cmd.set_defaults(enabled=None)
        self.commands.register(cmd, self.cmd_displaynames)

        cmd = CommandParser(
            prog="NOTICERELAY",
            description="enable or disable relaying of Matrix notices to Zulip",
        )
        cmd.add_argument(
            "--enable", dest="enabled", action="store_true", help="Enable notice relay"
        )
        cmd.add_argument(
            "--disable",
            dest="enabled",
            action="store_false",
            help="Disable notice relay",
        )
        cmd.set_defaults(enabled=None)
        self.commands.register(cmd, self.cmd_noticerelay)

        cmd = CommandParser(
            prog="TOPIC",
            description="show or set channel topic and configure sync mode",
        )
        cmd.add_argument(
            "--sync",
            choices=["off", "zulip", "matrix", "any"],
            help="Topic sync targets, defaults to off",
        )
        cmd.add_argument("text", nargs="*", help="topic text if setting")
        self.commands.register(cmd, self.cmd_topic)

        self.mx_register("m.room.topic", self._on_mx_room_topic)

    def is_valid(self) -> bool:
        # we are valid as long as the appservice is in the room
        if not self.in_room(self.serv.user_id):
            return False

        return True

    @staticmethod
    async def create(organization: "OrganizationRoom", name: str) -> "StreamRoom":
        logging.debug(
            f"StreamRoom.create(organization='{organization.name}', name='{name}'"
        )

        organization.send_notice("Initializing room...")

        room = StreamRoom(
            None,
            organization.user_id,
            organization.serv,
            [organization.user_id, organization.serv.user_id],
            [],
        )
        room.name = name.lower()
        room.organization = organization
        room.organization_id = organization.id
        room.max_backfill_amount = organization.max_backfill_amount

        result = organization.zulip.get_stream_id(name)
        room.stream_id = result.get("stream_id")

        if not room.stream_id:
            organization.send_notice(
                f"A stream with the name {name} doesn't exist or we haven't been invited to it."
            )
            return None

        room.organization = organization
        room.organization_id = organization.id

        # stamp global member sync setting at room creation time
        room.member_sync = organization.serv.config["member_sync"]

        organization.serv.register_room(room)
        organization.rooms[room.stream_id] = room

        asyncio.ensure_future(room.create_mx(name))
        return room

    def from_config(self, config: dict) -> None:
        super().from_config(config)

        if "key" in config:
            self.key = config["key"]

        if "member_sync" in config:
            self.member_sync = config["member_sync"]

        if "stream_id" in config:
            self.stream_id = config["stream_id"]

        if self.stream_id is None:
            raise InvalidConfigError("No stream_id key in config for ChannelRoom")

        # initialize lazy members dict if sync is not off
        if self.member_sync != "off":
            if self.lazy_members is None:
                self.lazy_members = {}
        else:
            self.lazy_members = None

        if "use_displaynames" in config:
            self.use_displaynames = config["use_displaynames"]

        if "allow_notice" in config:
            self.allow_notice = config["allow_notice"]

        if "topic_sync" in config:
            self.topic_sync = config["topic_sync"]

    def to_config(self) -> dict:
        return {
            **(super().to_config()),
            "key": self.key,
            "member_sync": self.member_sync,
            "stream_id": self.stream_id,
            "use_displaynames": self.use_displaynames,
            "allow_notice": self.allow_notice,
            "topic_sync": self.topic_sync,
        }

    async def create_mx(self, name):
        # handle !room names properly
        visible_name = name
        if visible_name.startswith("!"):
            visible_name = "!" + visible_name[6:]

        result = self.organization.zulip.call_endpoint(
            url=f"/streams/{self.stream_id}", method="get"
        )
        if result["result"] != "success":
            self.send_notice(f"Could not get stream by id: {result}")
            return

        restricted = None  # Invite only
        name_prefix = "🔒"

        if not result["stream"]["invite_only"]:
            # Allow space members to join
            restricted = self.organization.space.id
            name_prefix = "#"

        self.id = await self.organization.serv.create_room(
            f"{name_prefix}{visible_name} ({self.organization.name})",
            "",
            [self.organization.user_id],
            permissions=self.organization.permissions,
            restricted=restricted,
        )
        self.serv.register_room(self)
        await self.save()
        # start event queue now that we have an id
        self._queue.start()

        # attach to organization space
        if self.organization.space:
            await self.organization.space.attach(self.id)

    @connected
    async def _on_mx_room_topic(self, event) -> None:
        if event.sender != self.serv.user_id and self.topic_sync in ["zulip", "any"]:
            # topic = re.sub(r"[\r\n]", " ", event.content.topic)
            raise NotImplementedError("Changing Zulip stream description")

    @connected
    async def on_mx_message(self, event) -> None:
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

        if self.use_displaynames and event.sender in self.displaynames:
            sender_displayname = self.displaynames[event.sender][:100]

            sender_displayname = f"[{sender_displayname}](https://matrix.to/#/{sender})"

            sender = sender_displayname

        if event.content.msgtype.is_media or event.content.msgtype in (
            MessageType.EMOTE,
            MessageType.TEXT,
            MessageType.NOTICE,
        ):
            await self._relay_message(event, sender)

        await self.az.intent.send_receipt(event.room_id, event.event_id)

    async def _relay_message(self, event, sender):
        prefix = ""
        client = self.organization.zulip_puppets.get(event.sender)
        if not client:
            client = self.organization.zulip
            prefix = f"<{sender}> "

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

        # Get topic (Matrix thread)
        thread_id = event.content.get_thread_parent()
        # Ignore messages outside a thread
        if thread_id is None:
            return

        thread_event = await self.az.intent.get_event(self.id, thread_id)
        topic = thread_event.content.body

        # Save last thread event for old clients
        self.thread_last_message[thread_event.event_id] = event.event_id

        # Save thread id for topic
        if topic not in self.threads:
            self.threads[topic] = thread_event.event_id

        if event.content.get_edit():
            message = await self._process_event_content(event, prefix, reply_to)
            self.last_messages[event.sender] = event
        else:
            # keep track of the last message
            self.last_messages[event.sender] = event
            message = await self._process_event_content(event, prefix)

        request = {
            "type": "stream",
            "to": self.stream_id,
            "topic": topic,
            "content": message,
        }

        result = client.send_message(request)
        if result["result"] != "success":
            logging.error(f"Failed sending message to Zulip: {result['msg']}")
            return

        self.organization.messages[result["id"]] = event.event_id
        await self.organization.save()

        await self.save()

    @connected
    async def on_mx_ban(self, user_id) -> None:
        pass

    @connected
    async def on_mx_unban(self, user_id) -> None:
        pass

    @connected
    async def on_mx_leave(self, user_id) -> None:
        pass

    async def cmd_displaynames(self, args) -> None:
        if args.enabled is not None:
            self.use_displaynames = args.enabled
            await self.save()

        self.send_notice(
            f"Displaynames are {'enabled' if self.use_displaynames else 'disabled'}"
        )

    async def cmd_noticerelay(self, args) -> None:
        if args.enabled is not None:
            self.allow_notice = args.enabled
            await self.save()

        self.send_notice(
            f"Notice relay is {'enabled' if self.allow_notice else 'disabled'}"
        )

    async def cmd_topic(self, args) -> None:
        if args.sync is None:
            self.organization.conn.topic(self.name, " ".join(args.text))
            return

        self.topic_sync = args.sync if args.sync != "off" else None
        self.send_notice(
            f"Topic sync is {self.topic_sync if self.topic_sync else 'off'}"
        )
        await self.save()

    async def cmd_sync(self, args):
        if args.lazy:
            self.member_sync = "lazy"
            await self.save()
        elif args.half:
            self.member_sync = "half"
            await self.save()
        elif args.full:
            self.member_sync = "full"
            await self.save()
        elif args.off:
            self.member_sync = "off"
            # prevent anyone already in lazy list to be invited
            self.lazy_members = None
            await self.save()

        self.send_notice(
            f"Member sync is set to {self.member_sync}", forward=args._forward
        )

    def _add_puppet(self, zulip_user):
        mx_user_id = self.serv.get_mxid_from_zulip_user_id(
            self.organization, zulip_user["user_id"]
        )

        self.ensure_zulip_user_id(self.organization, zulip_user=zulip_user)
        self.join(mx_user_id, zulip_user["full_name"])

    def _remove_puppet(self, user_id, reason=None):
        if user_id == self.serv.user_id or user_id == self.user_id:
            return

        self.leave(user_id, reason)

    def on_join(self, zulip_user_id: int = None, zulip_user: dict = None) -> None:
        if zulip_user_id is None:
            zulip_user_id = zulip_user["user_id"]
        # we don't need to sync ourself
        if zulip_user_id == self.organization.profile["user_id"]:
            return
        if zulip_user is None:
            zulip_user = self.organization.get_zulip_user(zulip_user_id)

        # ensure, append, invite and join
        self._add_puppet(zulip_user)
        mx_user_id = self.serv.get_mxid_from_zulip_user_id(
            self.organization, zulip_user_id
        )
        self.join(mx_user_id, zulip_user["full_name"], lazy=False)

    def on_part(self, zulip_user_id) -> None:
        # we don't need to sync ourself
        if zulip_user_id == self.organization.profile["user_id"]:
            return

        mx_user_id = self.serv.get_mxid_from_zulip_user_id(
            self.organization, zulip_user_id
        )
        self._remove_puppet(mx_user_id)

    async def sync_zulip_members(self, subscribers: list):
        to_remove = []
        to_add = []

        # always reset lazy list because it can be toggled on-the-fly
        self.lazy_members = {} if self.member_sync != "off" else None

        # build to_remove list from our own puppets
        for member in self.members:
            (name, server) = member.split(":", 1)

            if (
                name.startswith("@" + self.serv.puppet_prefix)
                and server == self.serv.server_name
            ):
                to_remove.append(member)

        for zulip_user_id in subscribers:
            # convert to mx id, check if we already have them
            mx_user_id = self.serv.get_mxid_from_zulip_user_id(
                self.organization, zulip_user_id
            )

            # make sure this user is not removed from room
            if mx_user_id in to_remove:
                to_remove.remove(mx_user_id)
                continue

            # ignore adding us here, only lazy join on echo allowed
            if zulip_user_id == self.organization.profile["user_id"]:
                continue

            # if this user is not in room, add to invite list
            if not self.in_room(mx_user_id):
                to_add.append((mx_user_id, zulip_user_id))

            # always put everyone in the room to lazy list if we have any member sync
            if self.lazy_members is not None:
                self.lazy_members[mx_user_id] = zulip_user_id

        # never remove us or appservice
        if self.serv.user_id in to_remove:
            to_remove.remove(self.serv.user_id)
        if self.user_id in to_remove:
            to_remove.remove(self.user_id)

        for mx_user_id, zulip_user_id in to_add:
            zulip_user = self.organization.get_zulip_user(zulip_user_id)

            self._add_puppet(zulip_user)

        for mx_user_id in to_remove:
            self._remove_puppet(mx_user_id, "Unsubcribed from stream")

    async def backfill_messages(self):
        request = {
            "anchor": "newest",
            "num_before": self.max_backfill_amount,
            "num_after": 0,
            "narrow": [
                {"operator": "stream", "operand": self.stream_id},
            ],
        }
        result = self.organization.zulip.get_messages(request)

        if result["result"] != "success":
            logging.error(f"Failed getting Zulip messages: {result['msg']}")
            return

        for message in result["messages"]:
            if str(message["id"]) in self.organization.messages:
                continue
            self.organization.zulip_handler.backfill_message(message)
