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
import html
import logging
import re
from datetime import datetime
from datetime import timezone
from typing import Optional
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from markdownify import markdownify

from mautrix.api import Method
from mautrix.api import SynapseAdminPath
from mautrix.errors import MatrixStandardRequestError
from mautrix.types.event.state import JoinRestriction
from mautrix.types.event.state import JoinRestrictionType
from mautrix.types.event.state import JoinRule
from mautrix.types.event.state import JoinRulesStateEventContent
from mautrix.types.event.type import EventType

from matrixzulipbridge.command_parse import CommandManager
from matrixzulipbridge.command_parse import CommandParser
from matrixzulipbridge.command_parse import CommandParserError
from matrixzulipbridge.room import Room

if TYPE_CHECKING:
    from matrixzulipbridge.organization_room import OrganizationRoom


def connected(f):
    def wrapper(*args, **kwargs):
        self = args[0]

        if (
            not self.organization
            or not self.organization.conn
            or not self.organization.conn.connected
        ):
            self.send_notice("Need to be connected to use this command.")
            return asyncio.sleep(0)

        return f(*args, **kwargs)

    return wrapper


class PrivateRoom(Room):
    name: str
    organization: Optional["OrganizationRoom"]
    organization_id: str
    organization_name: Optional[str]
    media: list[list[str]]

    force_forward = False

    commands: CommandManager

    def init(self) -> None:
        self.name = None
        self.organization = None
        self.organization_id = None
        self.organization_name = None  # deprecated
        self.media = []
        self.lazy_members = {}  # allow lazy joining your own ghost for echo

        self.commands = CommandManager()

        if type(self) == PrivateRoom:
            cmd = CommandParser(prog="WHOIS", description="WHOIS the other user")
            self.commands.register(cmd, self.cmd_whois)

        self.mx_register("m.room.message", self.on_mx_message)
        self.mx_register("m.room.redaction", self.on_mx_redaction)

    def from_config(self, config: dict) -> None:
        super().from_config(config)

        if "name" not in config:
            raise Exception("No name key in config for ChatRoom")

        self.name = config["name"]

        if "organization_id" in config:
            self.organization_id = config["organization_id"]

        if "media" in config:
            self.media = config["media"]

        # only used for migration
        if "organization" in config:
            self.organization_name = config["organization"]

        if self.organization_name is None and self.organization_id is None:
            raise Exception(
                "No organization or organization_id key in config for PrivateRoom"
            )

    def to_config(self) -> dict:
        return {
            **(super().to_config()),
            "name": self.name,
            "organization": self.organization_name,
            "organization_id": self.organization_id,
            "media": self.media[:5],
        }

    @staticmethod
    async def create(organization: "OrganizationRoom", name: str) -> "PrivateRoom":
        logging.debug(
            f"PrivateRoom.create(organization='{organization.name}', name='{name}')"
        )
        raise NotImplementedError("Direct messaging")

        # asyncio.ensure_future(room.create_mx(name))
        # return room

    async def create_mx(self, displayname) -> None:
        if self.id is None:
            mx_user_id = await self.organization.serv.ensure_zulip_user_id(
                self.organization.name, displayname, update_cache=False
            )
            self.id = await self.organization.serv.create_room(
                f"{displayname} ({self.organization.name})",
                "Private chat with {displayname} on {self.organization.name}",
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
        if self.organization_id is None and self.organization_name is None:
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

    def pills(self):
        # if pills are disabled, don't generate any
        if self.organization.pills_length < 1:
            return None

        ret = {}
        ignore = list(map(lambda x: x.lower(), self.organization.pills_ignore))

        # push our own name first
        lnick = self.organization.conn.real_nickname.lower()
        if (
            self.user_id in self.displaynames
            and len(lnick) >= self.organization.pills_length
            and lnick not in ignore
        ):
            ret[lnick] = (self.user_id, self.displaynames[self.user_id])

        # assuming displayname of a puppet matches nick
        for member in self.members:
            if not member.startswith(
                "@" + self.serv.puppet_prefix
            ) or not member.endswith(":" + self.serv.server_name):
                continue

            if member in self.displaynames:
                nick = self.displaynames[member]
                lnick = nick.lower()
                if len(nick) >= self.organization.pills_length and lnick not in ignore:
                    ret[lnick] = (member, nick)

        return ret

    async def _process_event_content(self, event, prefix="", _reply_to=None):
        content = event.content

        if content.formatted_body:
            message = markdownify(content.formatted_body)
        elif content.body:
            message = content.body
        else:
            logging.warning("_process_event_content called with no usable body")
            return
        message = prefix + message
        return message

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

    async def _attach_space_internal(self) -> None:
        await self.az.intent.send_state_event(
            self.id,
            EventType.ROOM_JOIN_RULES,  # Why does this happend? pylint: disable=no-member
            content=JoinRulesStateEventContent(
                join_rule=JoinRule.RESTRICTED,
                allow=[
                    JoinRestriction(
                        type=JoinRestrictionType.ROOM_MEMBERSHIP,
                        room_id=self.organization.space.id,
                    ),
                ],
            ),
        )

    async def _attach_space(self) -> None:
        logging.debug(
            f"Attaching room {self.id} to organization space {self.organization.space.id}."
        )
        try:
            room_create = await self.az.intent.get_state_event(
                self.id, EventType.ROOM_CREATE
            )  # pylint: disable=no-member
            if room_create.room_version in [str(v) for v in range(1, 9)]:
                self.send_notice(
                    "Only rooms of version 9 or greater can be attached to a space."
                )
                self.send_notice(
                    "Leave and re-create the room to ensure the correct version."
                )
                return

            await self._attach_space_internal()
            self.send_notice("Attached to space.")
        except MatrixStandardRequestError as e:
            logging.debug("Setting join_rules for space failed.", exc_info=True)
            self.send_notice(f"Failed attaching space: {e.message}")
            self.send_notice("Make sure the room is at least version 9.")
        except Exception:
            logging.exception(
                f"Failed to attach {self.id} to space {self.organization.space.id}."
            )

    async def cmd_upgrade(self, args) -> None:
        if not args.undo:
            await self._attach_space()
