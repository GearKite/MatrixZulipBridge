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
from matrixzulipbridge.under_organization_room import UnderOrganizationRoom

if TYPE_CHECKING:
    from matrixzulipbridge.organization_room import OrganizationRoom


class PersonalRoom(UnderOrganizationRoom):
    commands: CommandManager

    def init(self):
        super().init()
        self.commands = CommandManager()

        cmd = CommandParser(
            prog="LOGINZULIP",
            description="enable Zulip puppeting and login",
            epilog=(),
        )
        cmd.add_argument("email", nargs="?", help="your Zulip account email")
        cmd.add_argument("api_key", nargs="?", help="your Zulip account API key")
        self.commands.register(cmd, self.cmd_loginzulip)

        cmd = CommandParser(
            prog="LOGOUTZULIP",
            description="disable Zulip puppeting",
            epilog=(),
        )
        self.commands.register(cmd, self.cmd_logoutzulip)

        self.mx_register("m.room.message", self.on_mx_message)

    @staticmethod
    async def create(
        organization: "OrganizationRoom", user_mxid: str
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

        organization.serv.register_room(room)
        organization.rooms[user_mxid] = room

        asyncio.ensure_future(room.create_mx(user_mxid))
        return room

    async def create_mx(self, user_mxid) -> None:
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
        await self.organization.login_zulip_puppet(
            self.user_id, args.email, args.api_key
        )
        await self.organization.save()
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

    async def on_mx_message(self, event) -> bool:
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
