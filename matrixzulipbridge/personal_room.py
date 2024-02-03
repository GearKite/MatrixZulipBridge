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
from typing import TYPE_CHECKING

from matrixzulipbridge import __version__
from matrixzulipbridge.command_parse import (
    CommandManager,
    CommandParser,
    CommandParserError,
)
from matrixzulipbridge.direct_room import DirectRoom
from matrixzulipbridge.under_organization_room import UnderOrganizationRoom

if TYPE_CHECKING:
    from mautrix.types import MessageEvent, UserID

    from matrixzulipbridge.organization_room import OrganizationRoom
    from matrixzulipbridge.types import ZulipUserID


class PersonalRoom(UnderOrganizationRoom):
    commands: CommandManager

    owner_mxid: "UserID"
    owner_zulip_id: "ZulipUserID"

    def init(self):
        super().init()

        self.owner_mxid = None
        self.owner_zulip_id = None

        self.commands = CommandManager()

        cmd = CommandParser(
            prog="LOGINZULIP",
            description="enable Zulip puppeting and login",
        )
        cmd.add_argument("email", nargs="?", help="your Zulip account email")
        cmd.add_argument("api_key", nargs="?", help="your Zulip account API key")
        self.commands.register(cmd, self.cmd_loginzulip)

        cmd = CommandParser(
            prog="LOGOUTZULIP",
            description="disable Zulip puppeting",
        )
        self.commands.register(cmd, self.cmd_logoutzulip)

        cmd = CommandParser(
            prog="DM",
            description="create a direct message room",
        )
        cmd.add_argument("user", nargs="+", help="Zulip puppet or Matrix user IDs")
        self.commands.register(cmd, self.cmd_dm)

        self.mx_register("m.room.message", self.on_mx_message)

    @staticmethod
    async def create(
        organization: "OrganizationRoom", user_mxid: "UserID"
    ) -> "PersonalRoom":
        logging.debug(
            f"PersonalRoom.create(organization='{organization.name}', user_mxid='{user_mxid}'"
        )
        room = PersonalRoom(
            None,
            user_mxid,
            organization.serv,
            [user_mxid, organization.serv.user_id],
            [],
        )
        room.organization = organization
        room.organization_id = organization.id

        room.owner_mxid = user_mxid

        organization.serv.register_room(room)
        organization.rooms[user_mxid] = room

        asyncio.ensure_future(room.create_mx(user_mxid))
        return room

    def from_config(self, config: dict) -> None:
        super().from_config(config)

        if "owner_mxid" in config:
            self.owner_mxid = config["owner_mxid"]

        if "owner_zulip_id" in config:
            self.owner_zulip_id = config["owner_zulip_id"]

    def to_config(self) -> dict:
        return {
            **(super().to_config()),
            "owner_mxid": self.owner_mxid,
            "owner_zulip_id": self.owner_zulip_id,
        }

    async def create_mx(self, user_mxid: "UserID") -> None:
        if self.id is None:
            self.id = await self.organization.serv.create_room(
                f"{self.organization.name} (Personal room)",
                f"Personal room for {self.organization.name}",
                [user_mxid],
            )
            self.serv.register_room(self)
            await self.save()
            # start event queue now that we have an id
            self._queue.start()

            # attach to organization space
            if self.organization.space:
                await self.organization.space.attach(self.id)

    def is_valid(self) -> bool:
        if self.user_id is None:
            return False

        if len(self.members) != 2:
            return False

        if self.owner_mxid is None:
            return False

        return True

    async def show_help(self):
        self.send_notice_html(
            f"<b>Howdy, stranger!</b> This is your personal room for {self.organization.name}."
        )

        try:
            return await self.commands.trigger("HELP")
        except CommandParserError as e:
            return self.send_notice(str(e))

    async def cmd_loginzulip(self, args):
        if not args.email or not args.api_key:
            self.send_notice("Specify an email address and API key to login.")
            return
        self.organization.zulip_puppet_login[self.user_id] = {
            "email": args.email,
            "api_key": args.api_key,
        }
        profile = await self.organization.login_zulip_puppet(
            self.user_id, args.email, args.api_key
        )
        self.owner_zulip_id = profile["user_id"]
        await self.save()
        self.send_notice_html("Enabled Zulip puppeting and logged in")

    async def cmd_logoutzulip(self, _args):
        try:
            del self.organization.zulip_puppet_login[self.user_id]
        except KeyError:
            self.send_notice("You haven't enabled Zulip puppeting")
            return
        try:
            del self.organization.zulip_puppets[self.user_id]
        except KeyError:
            pass
        self.send_notice("Logged out of Zulip")

    async def cmd_dm(self, args):
        users: list[str] = args.user
        users.append(self.owner_mxid)

        recipients = []
        for user in users:
            user_zulip_id = None
            if user in self.organization.zulip_puppet_user_mxid.inverse:
                user_zulip_id = self.organization.zulip_puppet_user_mxid.inverse[user]
            elif self.serv.is_puppet(user):
                user_zulip_id = self.organization.get_zulip_user_id_from_mxid(user)
            else:
                self.send_notice(f"Can't create DM with {user}")
                return

            zulip_user = self.organization.get_zulip_user(user_zulip_id)
            if zulip_user is None or "user_id" not in zulip_user:
                self.send_notice(f"Can't find Zulip user with ID {user_zulip_id}")
                return
            recipients.append(
                {
                    "id": zulip_user["user_id"],
                    "full_name": zulip_user["full_name"],
                }
            )
        recipient_ids = frozenset(user["id"] for user in recipients)
        room = self.organization.direct_rooms.get(recipient_ids)
        if room is not None:
            self.send_notice(f"You already have a room with these users at {room.id}")
            await room.check_if_nobody_left()
            return
        room = await DirectRoom.create(self.organization, recipients)
        self.send_notice("Created a DM room and invited you to it.")

    async def on_mx_message(self, event: "MessageEvent") -> bool:
        if str(event.content.msgtype) != "m.text" or event.sender == self.serv.user_id:
            return

        # ignore edits
        if event.content.get_edit():
            return

        try:
            lines = event.content.body.split("\n")

            command = lines.pop(0)
            tail = "\n".join(lines) if len(lines) > 0 else None

            await self.commands.trigger(command, tail)
        except CommandParserError as e:
            self.send_notice(str(e))

    async def cmd_version(self, _args):
        self.send_notice(f"zulipbridge v{__version__}")
