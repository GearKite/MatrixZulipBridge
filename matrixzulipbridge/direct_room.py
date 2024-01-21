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
import re
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

from mautrix.api import Method, SynapseAdminPath

from matrixzulipbridge.command_parse import (
    CommandManager,
    CommandParser,
    CommandParserError,
)
from matrixzulipbridge.room import InvalidConfigError
from matrixzulipbridge.under_organization_room import UnderOrganizationRoom, connected

if TYPE_CHECKING:
    from matrixzulipbridge.organization_room import OrganizationRoom


class DirectRoom(UnderOrganizationRoom):
    name: str
    media: list[list[str]]
    max_backfill_amount: int
    lazy_members: dict

    commands: CommandManager

    def init(self) -> None:
        self.name = None
        self.media = []
        self.lazy_members = {}  # allow lazy joining your own ghost for echo
        self.max_backfill_amount = None

        self.commands = CommandManager()

        if isinstance(self, DirectRoom):
            cmd = CommandParser(prog="WHOIS", description="WHOIS the other user")
            self.commands.register(cmd, self.cmd_whois)

        cmd = CommandParser(
            prog="BACKFILL",
            description="set the maximum amount of backfilled messages (0 to disable backfilling)",
        )
        cmd.add_argument("amount", nargs="?", help="new amount")
        self.commands.register(cmd, self.cmd_backfill)

        self.mx_register("m.room.message", self.on_mx_message)
        self.mx_register("m.room.redaction", self.on_mx_redaction)

    def from_config(self, config: dict) -> None:
        super().from_config(config)

        if "name" not in config:
            raise InvalidConfigError("No name key in config for ChatRoom")

        self.name = config["name"]

        if "media" in config:
            self.media = config["media"]

        if "max_backfill_amount" in config:
            self.max_backfill_amount = config["max_backfill_amount"]

    def to_config(self) -> dict:
        return {
            **(super().to_config()),
            "name": self.name,
            "organization_id": self.organization_id,
            "media": self.media[:5],
            "max_backfill_amount": self.max_backfill_amount,
        }

    @staticmethod
    async def create(organization: "OrganizationRoom", name: str) -> "DirectRoom":
        logging.debug(
            f"DirectRoom.create(organization='{organization.name}', name='{name}')"
        )
        raise NotImplementedError("Direct messaging")

        # asyncio.ensure_future(room.create_mx(name))
        # return room

    async def create_mx(self, name) -> None:
        if self.id is None:
            mx_user_id = await self.organization.serv.ensure_zulip_user_id(
                self.organization.name, name, update_cache=False
            )
            self.id = await self.organization.serv.create_room(
                f"{name} ({self.organization.name})",
                f"Private chat with {name} on {self.organization.name}",
                [self.organization.user_id, mx_user_id],
            )
            self.serv.register_room(self)
            await self.az.intent.user(mx_user_id).ensure_joined(self.id)
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

        if self.user_id is None:
            return False

        if not self.in_room(self.user_id):
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
        user_id: Optional[str] = None,
        formatted=None,
        fallback_html: Optional[str] = None,
        forward=False,
    ):
        if (
            self.force_forward or forward or self.organization.forward
        ) and user_id is None:
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
        self, text: str, user_id: Optional[str] = None, forward=False
    ) -> None:
        if (
            self.force_forward or forward or self.organization.forward
        ) and user_id is None:
            self.organization.send_notice_html(text=f"{self.name}: {text}")
        else:
            super().send_notice_html(text=text, user_id=user_id)

    async def _send_message(self, event, prefix=""):
        raise NotImplementedError("Private messages to Zulip")

    async def on_mx_message(self, event) -> None:
        if event.sender != self.user_id:
            return

        if (
            self.organization is None
            or self.organization.zulip is None
            or not self.organization.zulip.has_connected
        ):
            self.send_notice("Not connected to organization.")
            return

        if str(event.content.msgtype) == "m.emote":
            await self._send_message(event)
        elif str(event.content.msgtype) in ["m.image", "m.file", "m.audio", "m.video"]:
            self.organization.conn.privmsg(
                self.name, self.serv.mxc_to_url(event.content.url, event.content.body)
            )
            self.react(event.event_id, "\U0001F517")  # link
            self.media.append([event.event_id, event.content.url])
            await self.save()
        elif str(event.content.msgtype) == "m.text":
            # allow commanding the appservice in rooms
            match = re.match(r"^\s*@?([^:,\s]+)[\s:,]*(.+)$", event.content.body)
            if (
                match
                and match.group(1).lower() == self.serv.registration["sender_localpart"]
            ):
                try:
                    await self.commands.trigger(match.group(2))
                except CommandParserError as e:
                    self.send_notice(str(e))
                return

            await self._send_message(event)

        await self.az.intent.send_receipt(event.room_id, event.event_id)

    async def on_mx_redaction(self, event) -> None:
        for media in self.media:
            if media[0] == event.redacts:
                url = urlparse(media[1])
                if self.serv.synapse_admin:
                    try:
                        await self.az.intent.api.request(
                            Method.POST,
                            SynapseAdminPath.v1.media.quarantine[url.netloc][
                                url.path[1:]
                            ],
                        )

                        self.organization.send_notice(
                            f"Associated media {media[1]} for redacted event {event.redacts} "
                            + f"in room {self.name} was quarantined."
                        )
                    except Exception:
                        self.organization.send_notice(
                            f"Failed to quarantine media! Associated media {media[1]} "
                            + f"for redacted event {event.redacts} in room {self.name} is left available."
                        )
                else:
                    self.organization.send_notice(
                        f"No permission to quarantine media! Associated media {media[1]} "
                        + f"for redacted event {event.redacts} in room {self.name} is left available."
                    )
                return

    @connected
    async def cmd_whois(self, _args) -> None:
        self.organization.conn.whois(f"{self.name} {self.name}")

    async def cmd_backfill(self, args) -> None:
        if args.amount:
            self.max_backfill_amount = int(args.amount)
            await self.save()
        self.send_notice(
            f"Maximum backfill amount is set to: {self.max_backfill_amount}"
        )

    async def cmd_upgrade(self, args) -> None:
        if not args.undo:
            await self._attach_space()
