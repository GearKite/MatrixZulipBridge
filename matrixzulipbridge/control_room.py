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
import re
from argparse import Namespace
from html import escape
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from mautrix.errors import MatrixRequestError

from matrixzulipbridge import __version__
from matrixzulipbridge.command_parse import (
    CommandManager,
    CommandParser,
    CommandParserError,
)
from matrixzulipbridge.organization_room import OrganizationRoom
from matrixzulipbridge.personal_room import PersonalRoom
from matrixzulipbridge.room import Room

if TYPE_CHECKING:
    from mautrix.types import MessageEvent


class ControlRoom(Room):
    commands: CommandManager

    def init(self):
        self.commands = CommandManager()

        cmd = CommandParser(
            prog="PERSONALROOM",
            description="create a personal room for an organization",
        )
        cmd.add_argument("organization", nargs="?", help="organization name")
        self.commands.register(cmd, self.cmd_personalroom)

        if self.serv.is_admin(self.user_id):
            cmd = CommandParser(
                prog="ORGANIZATIONS", description="list available Zulip organizations"
            )
            self.commands.register(cmd, self.cmd_organizations)

            cmd = CommandParser(
                prog="OPEN", description="open organization for connecting"
            )
            cmd.add_argument("name", help="organization name (see ORGANIZATIONS)")
            cmd.add_argument(
                "--new",
                action="store_true",
                help="force open a new organization connection",
            )
            self.commands.register(cmd, self.cmd_open)

            cmd = CommandParser(
                prog="STATUS",
                description="show bridge status",
                epilog="Note: admins see all users but only their own rooms",
            )
            self.commands.register(cmd, self.cmd_status)

            cmd = CommandParser(
                prog="QUIT",
                description="disconnect from all organizations",
                epilog=(
                    "For quickly leaving all organizations and removing configurations in a single command.\n"
                    "\n"
                    "Additionally this will close current DM session with the bridge.\n"
                ),
            )
            self.commands.register(cmd, self.cmd_quit)

            cmd = CommandParser(prog="MASKS", description="list allow masks")
            self.commands.register(cmd, self.cmd_masks)

            cmd = CommandParser(
                prog="ADDMASK",
                description="add new allow mask",
                epilog=(
                    "For anyone else than the owner to use this bridge they need to be allowed to talk with the bridge bot.\n"
                    "This is accomplished by adding an allow mask that determines their permission level when using the bridge.\n"
                    "\n"
                    "Only admins can manage organizations, normal users can just connect.\n"
                ),
            )
            cmd.add_argument(
                "mask", help="Matrix ID mask (eg: @friend:contoso.com or *:contoso.com)"
            )
            cmd.add_argument("--admin", help="Admin level access", action="store_true")
            self.commands.register(cmd, self.cmd_addmask)

            cmd = CommandParser(
                prog="DELMASK",
                description="delete allow mask",
                epilog=(
                    "Note: Removing a mask only prevents starting a new DM with the bridge bot. Use FORGET for ending existing"
                    " sessions."
                ),
            )
            cmd.add_argument(
                "mask", help="Matrix ID mask (eg: @friend:contoso.com or *:contoso.com)"
            )
            self.commands.register(cmd, self.cmd_delmask)

            cmd = CommandParser(
                prog="ADDORGANIZATION", description="add a Zulip organization"
            )
            cmd.add_argument("name", help="server address")
            self.commands.register(cmd, self.cmd_addorganization)

            cmd = CommandParser(
                prog="DELORGANIZATION", description="delete a Zulip organization"
            )
            cmd.add_argument("name", help="organization name")
            self.commands.register(cmd, self.cmd_delorganization)

            cmd = CommandParser(prog="FORGET", description="Forget a Matrix user")
            cmd.add_argument("user", help="Matrix ID (eg: @ex-friend:contoso.com)")
            self.commands.register(cmd, self.cmd_forget)

            cmd = CommandParser(
                prog="DISPLAYNAME", description="change bridge displayname"
            )
            cmd.add_argument("displayname", help="new bridge displayname")
            self.commands.register(cmd, self.cmd_displayname)

            cmd = CommandParser(prog="AVATAR", description="change bridge avatar")
            cmd.add_argument("url", help="new avatar URL (mxc:// format)")
            self.commands.register(cmd, self.cmd_avatar)

            cmd = CommandParser(
                prog="MEDIAURL", description="configure media URL for links"
            )
            cmd.add_argument("url", nargs="?", help="new URL override")
            cmd.add_argument(
                "--remove",
                help="remove URL override (will retry auto-detection)",
                action="store_true",
            )
            self.commands.register(cmd, self.cmd_media_url)

            cmd = CommandParser(
                prog="MEDIAPATH", description="configure media path for links"
            )
            cmd.add_argument("path", nargs="?", help="new path override")
            cmd.add_argument(
                "--remove", help="remove path override", action="store_true"
            )
            self.commands.register(cmd, self.cmd_media_path)

            cmd = CommandParser(prog="VERSION", description="show bridge version")
            self.commands.register(cmd, self.cmd_version)

        self.mx_register("m.room.message", self.on_mx_message)

    def is_valid(self) -> bool:
        if self.user_id is None:
            return False

        if len(self.members) != 2:
            return False

        return True

    async def show_help(self):
        self.send_notice_html(
            f"<b>Howdy, stranger!</b> You have been granted access to the Zulip bridge of <b>{self.serv.server_name}</b>."
        )

        try:
            return await self.commands.trigger("HELP")
        except CommandParserError as e:
            return self.send_notice(str(e))

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

    def organizations(self):
        organizations = {}

        for organization, config in self.serv.config["organizations"].items():
            config["name"] = organization
            organizations[organization.lower()] = config

        return organizations

    async def cmd_masks(self, args):
        msg = "Configured masks:\n"

        for mask, value in self.serv.config["allow"].items():
            msg += "\t{} -> {}\n".format(mask, value)

        self.send_notice(msg)

    async def cmd_addmask(self, args):
        masks = self.serv.config["allow"]

        if args.mask in masks:
            return self.send_notice("Mask already exists")

        masks[args.mask] = "admin" if args.admin else "user"
        await self.serv.save()

        self.send_notice("Mask added.")

    async def cmd_delmask(self, args):
        masks = self.serv.config["allow"]

        if args.mask not in masks:
            return self.send_notice("Mask does not exist")

        del masks[args.mask]
        await self.serv.save()

        self.send_notice("Mask removed.")

    async def cmd_organizations(self, args):
        organizations: dict["OrganizationRoom", dict] = self.serv.config[
            "organizations"
        ]

        self.send_notice("Configured organizations:")

        for _, data in organizations.items():
            self.send_notice(f"\t{data}")

    async def cmd_addorganization(self, args):
        organizations = self.organizations()

        if args.name.lower() in organizations:
            return self.send_notice("Organization with that name already exists")

        self.serv.config["organizations"][args.name] = {
            "name": args.name,
        }
        await self.serv.save()

        self.send_notice("Organization added.")

    async def cmd_delorganization(self, args):
        organizations = self.organizations()

        if args.name.lower() not in organizations:
            return self.send_notice("Organization does not exist")

        del self.serv.config["organizations"][args.name.lower()]
        await self.serv.save()

        return self.send_notice("Organization removed.")

    async def cmd_status(self, _args):
        users = set()
        response = ""

        if self.serv.is_admin(self.user_id):
            for room in self.serv.find_rooms():
                if not room.user_id:
                    continue
                users.add(room.user_id)

            users = list(users)
            users.sort()
        else:
            users.add(self.user_id)

        response += f"I have {len(users)} known users:"
        for user_id in users:
            ncontrol = len(self.serv.find_rooms("ControlRoom", user_id))

            response += f"<br>{indent(1)}{user_id} ({ncontrol} open control rooms):"

            for organization in self.serv.find_rooms("OrganizationRoom", user_id):
                connected = "not connected"
                stream = "no streams"
                direct = "no DMs"

                if organization.zulip and organization.zulip.has_connected:
                    connected = f"connected to {organization.site}"

                nstream = 0
                ndirect = len(organization.direct_rooms)

                for room in organization.rooms.values():
                    if type(room).__name__ == "StreamRoom":
                        nstream += 1

                if nstream > 0:
                    stream = f"{nstream} streams"

                if ndirect > 0:
                    direct = f"{ndirect} DMs"

                response += f"<br>{indent(2)}{organization.name}, {connected}, {stream}, {direct}"
        self.send_notice_html(response)

    async def cmd_forget(self, args):
        if args.user == self.user_id:
            return self.send_notice("I can't forget you, silly!")

        rooms = self.serv.find_rooms(None, args.user)

        if len(rooms) == 0:
            return self.send_notice("No such user. See STATUS for list of users.")

        # disconnect each organization room in first pass
        for room in rooms:
            if type(room) == OrganizationRoom and room.conn and room.conn.connected:
                self.send_notice(f"Disconnecting {args.user} from {room.name}...")
                await room.cmd_disconnect(Namespace())

        self.send_notice(f"Leaving all {len(rooms)} rooms {args.user} was in...")

        # then just forget everything
        for room in rooms:
            self.serv.unregister_room(room.id)

            try:
                await self.az.intent.leave_room(room.id)
            except MatrixRequestError:
                pass
            try:
                await self.az.intent.forget_room(room.id)
            except MatrixRequestError:
                pass

        self.send_notice(f"Done, I have forgotten about {args.user}")

    async def cmd_displayname(self, args):
        try:
            await self.az.intent.set_displayname(args.displayname)
        except MatrixRequestError as e:
            self.send_notice(f"Failed to set displayname: {str(e)}")

    async def cmd_avatar(self, args):
        try:
            await self.az.intent.set_avatar_url(args.url)
        except MatrixRequestError as e:
            self.send_notice(f"Failed to set avatar: {str(e)}")

    async def cmd_ident(self, args):
        idents = self.serv.config["idents"]

        if args.cmd == "list" or args.cmd is None:
            self.send_notice("Configured custom idents:")
            for mxid, ident in idents.items():
                self.send_notice(f"\t{mxid} -> {ident}")
        elif args.cmd == "set":
            if not re.match(r"^[a-z][-a-z0-9]*$", args.ident):
                self.send_notice(f"Invalid ident string: {args.ident}")
                self.send_notice(
                    "Must be lowercase, start with a letter, can contain dashes, letters and numbers."
                )
            else:
                idents[args.mxid] = args.ident
                self.send_notice(f"Set custom ident for {args.mxid} to {args.ident}")
                await self.serv.save()
        elif args.cmd == "remove":
            if args.mxid in idents:
                del idents[args.mxid]
                self.send_notice(f"Removed custom ident for {args.mxid}")
                await self.serv.save()
            else:
                self.send_notice(f"No custom ident for {args.mxid}")

    async def cmd_sync(self, args):
        if args.lazy:
            self.serv.config["member_sync"] = "lazy"
            await self.serv.save()
        elif args.half:
            self.serv.config["member_sync"] = "half"
            await self.serv.save()
        elif args.full:
            self.serv.config["member_sync"] = "full"
            await self.serv.save()

        self.send_notice(f"Member sync is set to {self.serv.config['member_sync']}")

    async def cmd_media_url(self, args):
        if args.remove:
            self.serv.config["media_url"] = None
            await self.serv.save()
            self.serv.endpoint = await self.serv.detect_public_endpoint()
        elif args.url:
            parsed = urlparse(args.url)
            if (
                parsed.scheme in ["http", "https"]
                and not parsed.params
                and not parsed.query
                and not parsed.fragment
            ):
                self.serv.config["media_url"] = args.url
                await self.serv.save()
                self.serv.endpoint = args.url
            else:
                self.send_notice(f"Invalid media URL format: {args.url}")
                return

        self.send_notice(
            f"Media URL override is set to {self.serv.config['media_url']}"
        )
        self.send_notice(f"Current active media URL: {self.serv.endpoint}")

    async def cmd_media_path(self, args):
        if args.remove:
            self.serv.config["media_path"] = None
            await self.serv.save()
            self.serv.media_path = self.serv.DEFAULT_MEDIA_PATH
        elif args.path:
            self.serv.config["media_path"] = args.path
            await self.serv.save()
            self.serv.media_path = args.path

        self.send_notice(
            f"Media Path override is set to {self.serv.config['media_path']}"
        )
        self.send_notice(f"Current active media path: {self.serv.media_path}")

    async def cmd_open(self, args):
        organizations = self.organizations()
        name = args.name.lower()

        if name not in organizations:
            return self.send_notice("Organization does not exist")

        organization = organizations[name]

        found = 0
        for room in self.serv.find_rooms(OrganizationRoom, self.user_id):
            if room.name == organization["name"]:
                found += 1

                if not args.new:
                    if self.user_id not in room.members:
                        self.send_notice(f"Inviting back to {room.name} ({room.id})")
                        await self.az.intent.invite_user(room.id, self.user_id)
                    else:
                        self.send_notice(f"You are already in {room.name} ({room.id})")

        # if we found at least one organization room, no need to create unless forced
        if found > 0 and not args.new:
            return

        name = (
            organization["name"]
            if found == 0
            else f"{organization['name']} {found + 1}"
        )

        self.send_notice(f"You have been invited to {name}")
        await OrganizationRoom.create(self.serv, organization, self.user_id, name)

    async def cmd_quit(self, args):
        rooms = self.serv.find_rooms(None, self.user_id)

        # disconnect each organization room in first pass
        for room in rooms:
            if (
                type(room) == OrganizationRoom
                and room.zulip
                and room.zulip.has_connected
            ):
                self.send_notice(f"Disconnecting from {room.name}...")
                await room.cmd_disconnect(Namespace())

        self.send_notice("Closing all channels and private messages...")

        # then just forget everything
        for room in rooms:
            if room.id == self.id:
                continue

            self.serv.unregister_room(room.id)

            try:
                await self.az.intent.leave_room(room.id)
            except MatrixRequestError:
                pass
            try:
                await self.az.intent.forget_room(room.id)
            except MatrixRequestError:
                pass

    async def cmd_version(self, args):
        self.send_notice(f"zulipbridge v{__version__}")

    async def cmd_personalroom(self, args) -> None:
        organization = None
        for room in self.serv.find_rooms():
            if not isinstance(room, OrganizationRoom):
                continue
            if room.name.lower() == args.organization:
                organization = room
                break
        if not organization:
            # TODO: Add permissions for creating a personal room
            self.send_notice(
                "Could not find an organization with that name or you don't have permissions"
            )
            return
        await PersonalRoom.create(organization, self.user_id)
        self.send_notice("Personal room created")


def indent(n):
    return "&nbsp;" * n * 8
