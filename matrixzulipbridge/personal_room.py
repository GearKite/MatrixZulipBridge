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
from matrixzulipbridge import __version__
from matrixzulipbridge.command_parse import CommandManager
from matrixzulipbridge.command_parse import CommandParserError
from matrixzulipbridge.room import Room


class PersonalRoom(Room):
    commands: CommandManager

    def init(self):
        self.commands = CommandManager()

        self.mx_register("m.room.message", self.on_mx_message)

    def is_valid(self) -> bool:
        if self.user_id is None:
            return False

        if len(self.members) != 2:
            return False

        return True

    async def show_help(self):
        self.send_notice_html(
            f"<b>Howdy, stranger!</b> This is your personal <b>{self.serv.server_name}</b> room."
        )

        try:
            return await self.commands.trigger("HELP")
        except CommandParserError as e:
            return self.send_notice(str(e))

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
