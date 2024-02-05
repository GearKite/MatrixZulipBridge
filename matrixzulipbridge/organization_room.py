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
import argparse
import asyncio
import datetime
import functools
import html
import json
import logging
import re
from argparse import Namespace
from typing import TYPE_CHECKING, Any, Optional

import zulip
from bidict import bidict
from mautrix.util.bridge_state import BridgeStateEvent

from matrixzulipbridge import __version__
from matrixzulipbridge.command_parse import (
    CommandManager,
    CommandParser,
    CommandParserError,
)
from matrixzulipbridge.direct_room import DirectRoom
from matrixzulipbridge.personal_room import PersonalRoom
from matrixzulipbridge.room import InvalidConfigError, Room
from matrixzulipbridge.space_room import SpaceRoom
from matrixzulipbridge.stream_room import StreamRoom

# pylint: disable=unused-import
from matrixzulipbridge.under_organization_room import connected
from matrixzulipbridge.zulip import ZulipEventHandler

if TYPE_CHECKING:
    from mautrix.types import UserID

    from matrixzulipbridge.appservice import AppService
    from matrixzulipbridge.types import ZulipUserID


class OrganizationRoom(Room):
    # configuration stuff
    name: str
    connected: bool
    fullname: str

    api_key: str
    email: str
    site: str
    zulip: "zulip.Client"
    zulip_users: dict["ZulipUserID", dict]
    zulip_puppet_login: dict["UserID", dict]
    zulip_puppets: dict["UserID", "zulip.Client"]
    zulip_puppet_user_mxid: bidict["ZulipUserID", "UserID"]

    # state
    commands: CommandManager
    rooms: dict[str, Room]
    direct_rooms: dict[frozenset["ZulipUserID"], "DirectRoom"]
    connecting: bool
    backoff: int
    backoff_task: Any
    connected_at: int
    space: SpaceRoom
    post_init_done: bool
    disconnect: bool

    organization: "OrganizationRoom"

    profile: dict
    server: dict
    messages: dict[str, str]
    permissions: dict[str, str]
    zulip_handler: "ZulipEventHandler"
    max_backfill_amount: int
    relay_moderation: bool
    deactivated_users: set["ZulipUserID"]

    def init(self):
        self.name = None
        self.connected = False
        self.fullname = None
        self.backoff = 0
        self.backoff_task = None
        self.connected_at = 0

        self.api_key = None
        self.email = None
        self.site = None
        self.zulip_users = {}
        self.zulip_puppet_login = {}
        self.zulip_puppets = {}
        self.zulip_puppet_user_mxid = bidict()

        self.commands = CommandManager()
        self.zulip = None
        self.rooms = {}
        self.direct_rooms = {}
        self.connlock = asyncio.Lock()
        self.disconnect = True
        self.space = None

        self.organization = self

        self.profile = None
        self.server = None
        self.messages = {}
        self.permissions = {}
        self.zulip_handler = None
        self.max_backfill_amount = 100
        self.relay_moderation = True
        self.deactivated_users = set()

        cmd = CommandParser(
            prog="FULLNAME",
            description="set/change full name",
            epilog=(
                "You can always see your current full name on the organization without arguments.\n"
            ),
        )
        cmd.add_argument("fullname", nargs="?", help="new full name")
        self.commands.register(cmd, self.cmd_fullname)

        cmd = CommandParser(
            prog="SITE",
            description="set Zulip site",
        )
        cmd.add_argument("site", nargs="?", help="new site")
        self.commands.register(cmd, self.cmd_site)

        cmd = CommandParser(
            prog="EMAIL",
            description="set Zulip bot email",
        )
        cmd.add_argument("email", nargs="?", help="new bot email")
        self.commands.register(cmd, self.cmd_email)

        cmd = CommandParser(
            prog="APIKEY",
            description="set Zulip bot api key",
        )
        cmd.add_argument("api_key", nargs="?", help="new API key")
        self.commands.register(cmd, self.cmd_apikey)

        cmd = CommandParser(
            prog="CONNECT",
            description="connect to organization",
            epilog=(
                "When this command is invoked the connection to this organization will be persisted across disconnects and"
                " bridge restart.\n"
                "Only if the server KILLs your connection it will stay disconnected until CONNECT is invoked again.\n"
                "\n"
                "If you want to cancel automatic reconnect you need to issue the DISCONNECT command.\n"
            ),
        )
        self.commands.register(cmd, self.cmd_connect)

        cmd = CommandParser(
            prog="DISCONNECT",
            description="disconnect from organization",
            epilog=(
                "In addition to disconnecting from an active organization connection this will also cancel any automatic"
                "reconnection attempt.\n"
            ),
        )
        self.commands.register(cmd, self.cmd_disconnect)

        cmd = CommandParser(prog="RECONNECT", description="reconnect to organization")
        self.commands.register(cmd, self.cmd_reconnect)

        cmd = CommandParser(
            prog="SUBSCRIBE",
            description="bridge a stream",
            epilog=(
                "Manually subscribe to a stream and bridge it\n"
                "\n"
                "Any subscriptions will be persisted between reconnects.\n"
                "\n"
                "Specifying a room will make the bridge join that room, instead of creating a new one\n"
            ),
        )
        cmd.add_argument("stream", help="target stream")
        cmd.add_argument(
            "backfill", nargs="?", help="number of messages to backfill", type=int
        )
        cmd.add_argument("room", nargs="?", help="room ID")
        self.commands.register(cmd, self.cmd_subscribe)

        cmd = CommandParser(
            prog="UNSUBSCRIBE",
            description="unbridge a stream and leave the room",
        )
        cmd.add_argument("stream", help="target stream")
        self.commands.register(cmd, self.cmd_unsubscribe)

        cmd = CommandParser(
            prog="SPACE", description="join/create a space for this organization"
        )
        self.commands.register(cmd, self.cmd_space)

        cmd = CommandParser(
            prog="SYNCPERMISSIONS", description="resync all permissions"
        )
        self.commands.register(cmd, self.cmd_syncpermissions)

        cmd = CommandParser(prog="PROFILE", description="fetch our Zulip profile")
        self.commands.register(cmd, self.cmd_profile)

        cmd = CommandParser(
            prog="ROOM",
            description="run a room command from organization room",
            epilog=(
                "Try 'ROOM #foo' to get the list of commands for a room."
                "If a command generates Zulip replies in a bouncer room they will appear in the room itself."
            ),
        )
        cmd.add_argument("target", help="Zulip stream name")
        cmd.add_argument(
            "command", help="Command and arguments", nargs=argparse.REMAINDER
        )
        self.commands.register(cmd, self.cmd_room)

        cmd = CommandParser(
            prog="STATUS", description="show current organization status"
        )
        self.commands.register(cmd, self.cmd_status)

        cmd = CommandParser(
            prog="BACKFILL",
            description="set the default maximum amount of backfilled messages (0 to disable backfilling)",
        )
        cmd.add_argument("amount", nargs="?", help="new amount")
        cmd.add_argument(
            "--update", action="store_true", help="also set this to all existing rooms"
        )
        cmd.add_argument("--now", action="store_true", help="start backfilling now")
        self.commands.register(cmd, self.cmd_backfill)

        cmd = CommandParser(
            prog="PERSONALROOM",
            description="create a personal room",
        )
        self.commands.register(cmd, self.cmd_personalroom)
        cmd = CommandParser(
            prog="RELAYMODERATION",
            description="Whether to relay bans to Zulip",
            epilog="When a user is banned in one room, their Zulip account is deactivated and removed from all rooms.",
        )
        group = cmd.add_mutually_exclusive_group()
        group.add_argument(
            "--on",
            help="turn relaying moderation on",
            action="store_true",
        )
        group.add_argument(
            "--off",
            help="turn relaying moderation off",
            action="store_true",
        )
        self.commands.register(cmd, self.cmd_relaymoderation)

        self.mx_register("m.room.message", self.on_mx_message)

    @staticmethod
    async def create(serv: "AppService", organization: dict, user_id: "UserID", name):
        room_id = await serv.create_room(
            name, f"Organization room for {organization['name']}", [user_id]
        )
        room = OrganizationRoom(
            room_id, user_id, serv, [serv.user_id, user_id], bans=[]
        )
        room.from_config(organization)
        serv.register_room(room)

        room.space = await SpaceRoom.create(room, [r.id for r in room.rooms.values()])

        # calls the api and attaches rooms
        await room.space.create_finalize()
        await room.space.attach(room.id)

        await room.save()

        await room.show_help()
        return room

    def from_config(self, config: dict):
        if "name" in config:
            self.name = config["name"]
        else:
            raise InvalidConfigError("No name key in config for OrganizationRoom")

        if "api_key" in config and config["api_key"]:
            self.api_key = config["api_key"]

        if "email" in config and config["email"]:
            self.email = config["email"]

        if "site" in config and config["site"]:
            self.site = config["site"]

        if "messages" in config and config["messages"]:
            self.messages = config["messages"]

        if "max_backfill_amount" in config and config["max_backfill_amount"]:
            self.max_backfill_amount = config["max_backfill_amount"]

        if "zulip_puppet_login" in config and config["zulip_puppet_login"]:
            self.zulip_puppet_login = config["zulip_puppet_login"]

    def to_config(self) -> dict:
        return {
            "name": self.name,
            "api_key": self.api_key,
            "email": self.email,
            "site": self.site,
            "messages": self.messages,
            "max_backfill_amount": self.max_backfill_amount,
            "zulip_puppet_login": self.zulip_puppet_login,
        }

    def is_valid(self) -> bool:
        if self.name is None:
            return False

        # we require user to be in organization room or be connected with channels or PMs
        if not self.in_room(self.user_id):
            # if not connected (or trying to) we can clean up
            if not self.connected:
                return False

            # only if post_init has been done and we're connected with no rooms clean up
            if self.post_init_done and self.connected and len(self.rooms) == 0:
                return False

        return True

    def cleanup(self) -> None:
        logging.debug(f"Network {self.id} cleaning up")

        # prevent reconnecting ever again
        self.connected = False
        self.disconnect = True

        if self.backoff_task:
            self.backoff_task.cancel()
            self.backoff_task = None
            logging.debug("... cancelled backoff task")

        if self.zulip:
            self.zulip = None

        if self.space:
            self.serv.unregister_room(self.space.id)
            self.space.cleanup()
            asyncio.ensure_future(
                self.serv.leave_room(self.space.id, self.space.members)
            )
            logging.debug("... cleaned up space")

        super().cleanup()

    async def show_help(self):
        self.send_notice_html(
            f"Welcome to the organization room for <b>{html.escape(self.name)}</b>!"
        )

        try:
            return await self.commands.trigger("HELP")
        except CommandParserError as e:
            return self.send_notice(str(e))

    async def on_mx_message(self, event) -> None:
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

    async def cmd_connect(self, _args) -> None:
        await self.connect()

    @connected  # pylint: disable=used-before-assignment
    async def cmd_disconnect(self, _args) -> None:
        self.disconnect = True

        if self.backoff_task:
            self.backoff_task.cancel()
            self.backoff_task = None

        self.backoff = 0
        self.connected_at = 0

        if self.connected:
            self.connected = False
            await self.save()

        if self.zulip:
            self.zulip = None
            self.send_notice("Disconnected")

    async def cmd_reconnect(self, _args) -> None:
        await self.cmd_disconnect(Namespace())
        await self.cmd_connect(Namespace())

    @connected
    async def cmd_subscribe(self, args) -> None:
        stream = args.stream

        for room in self.rooms.values():
            if not isinstance(room, StreamRoom):
                continue
            if stream.lower() == room.name:
                self.send_notice(f"Stream {stream} already exists at {room.id}.")
                return

        self.zulip.add_subscriptions([{"name": stream}])
        room = await StreamRoom.create(
            organization=self,
            name=stream,
            backfill=args.backfill,
            room_id=args.room,
        )
        await room.backfill_messages()

    @connected
    async def cmd_unsubscribe(self, args) -> None:
        stream = args.stream.lower()

        room = None
        for r in self.rooms.values():
            if not isinstance(r, StreamRoom):
                continue
            if r.name.lower() == stream:
                room = r
                break

        if room is None:
            self.send_notice("No room with that name exists.")
            return

        self.serv.unregister_room(room.id)
        room.cleanup()
        await self.serv.leave_room(room.id, room.members)
        del self.rooms[room.stream_id]

        self.zulip.remove_subscriptions([stream])
        self.send_notice(f"Unsubscribed from {stream} and removed room {room.id}.")

    def get_fullname(self):
        if self.fullname:
            return self.fullname
        return self.profile["full_name"]

    async def cmd_fullname(self, args) -> None:
        if args.fullname is None:
            fullname = self.get_fullname()
            if self.zulip and self.zulip.has_connected:
                self.send_notice(f"Full name: {fullname}")
            return

        if self.zulip and self.zulip.has_connected:
            self.zulip.update_user_by_id(
                self.profile["user_id"], full_name=args.fullname
            )
            self.fullname = args.fullname
            self.send_notice(f"Full name set to {self.fullname}")

    async def cmd_site(self, args) -> None:
        if not args.site:
            self.send_notice(f"Zulip site is: {self.site}")
            return

        self.site = args.site
        await self.save()
        self.send_notice(f"Zulip site set to {self.site}")

    async def cmd_email(self, args) -> None:
        if not args.email:
            self.send_notice(f"Bot email is: {self.email}")
            return

        self.email = args.email
        await self.save()
        self.send_notice(f"Bot email set to {self.email}")

    async def cmd_apikey(self, args) -> None:
        if not args.api_key:
            self.send_notice(f"Bot API key is {'not ' if self.api_key else ''}set")
            return

        self.api_key = args.api_key
        await self.save()
        self.send_notice("Bot API Key changed")

    async def cmd_profile(self, _args) -> None:
        self.profile = self.zulip.get_profile()
        self.send_notice(json.dumps(self.profile, indent=4))

    async def cmd_room(self, args) -> None:
        target = args.target.lower()

        room = None

        for r in self.rooms.values():
            if not isinstance(r, StreamRoom):
                continue
            if r.name.lower() == target:
                room = r

        if not room:
            self.send_notice(f"No room for {args.target}")
            return

        if len(args.command) == 0:
            args.command = ["HELP"]

        await room.commands.trigger_args(args.command, forward=True)

    async def cmd_status(self, _args) -> None:
        if self.connected_at > 0:
            conntime = asyncio.get_running_loop().time() - self.connected_at
            conntime = str(datetime.timedelta(seconds=int(conntime)))
            self.send_notice(f"Connected for {conntime}")

        else:
            self.send_notice("Not connected to server.")

        dms = []
        streams = []

        for room in self.rooms.values():
            if isinstance(room, StreamRoom):
                streams.append(room.name)
        for dm_room in self.direct_rooms.values():
            dms.append(dm_room.name)

        if len(streams) > 0:
            self.send_notice(f"Streams: #{', #'.join(streams)}")

        if len(dms) > 0:
            self.send_notice(f"Open DMs: {len(dms)}")

    async def cmd_space(self, _args) -> None:
        if self.space is None:
            # sync create to prevent race conditions
            self.space = SpaceRoom.create(
                self, [room.id for room in self.rooms.values()]
            )

            # calls the api and attaches rooms
            self.send_notice("Creating space and inviting you to it.")
            await self.space.create_finalize()
        else:
            self.send_notice(f"Space already exists ({self.space.id}).")

    async def cmd_syncpermissions(self, _args) -> None:
        await self._sync_permissions()
        self.send_notice("Permissions synched successfully")

    async def cmd_backfill(self, args) -> None:
        if args.amount:
            self.max_backfill_amount = int(args.amount)
            await self.save()
        if args.update:
            for room in self.rooms.values():
                if not isinstance(room, DirectRoom):
                    continue
                room.max_backfill_amount = self.max_backfill_amount
                await room.save()
            self.send_notice(
                f"Set maximum backfill amount to {self.max_backfill_amount} and updated all rooms"
            )
        else:
            self.send_notice(
                f"Maximum backfill amount is set to: {self.max_backfill_amount}"
            )
        if args.now:
            await self.backfill_messages()

    async def cmd_personalroom(self, _args) -> None:
        await PersonalRoom.create(self, self.user_id)
        self.send_notice("Personal room created")

    async def cmd_relaymoderation(self, args) -> None:
        if args.on:
            self.relay_moderation = True
            await self.save()
        elif args.off:
            self.relay_moderation = False
            await self.save()

        self.send_notice(
            f"Relaying moderation is {'enabled' if self.relay_moderation else 'disabled'}",
        )

    async def connect(self) -> None:
        if not self.is_valid():
            logging.warning(
                "Trying to connect an invalid organization {self.id}, this is likely a dangling organization."
            )
            return

        if self.connlock.locked():
            self.send_notice("Already connecting.")
            return

        async with self.connlock:
            if self.zulip and self.connected:
                self.send_notice("Already connected.")
                return

            self.disconnect = False
            await self._connect()

    async def post_init(self) -> None:
        # attach loose sub-rooms to us
        for room_type in [DirectRoom, StreamRoom, PersonalRoom]:
            for room in self.serv.find_rooms(room_type, organization_id=self.id):
                room.organization = self

                logging.debug(f"{self.id} attaching {room.id}")
                match room:
                    case StreamRoom():
                        self.rooms[room.stream_id] = room
                    case DirectRoom():
                        self.direct_rooms[frozenset(room.recipient_ids)] = room
                    case _:
                        self.rooms[room.id] = room
        logging.debug(self.direct_rooms)

        self.post_init_done = True

    async def _connect(self) -> None:
        if not self.site:
            self.send_notice("Zulip site is not set!")
            return
        if not self.email:
            self.send_notice("Bot email is not set!")
            return
        if not self.api_key:
            self.send_notice("Bot API key is not set!")
            return
        # force cleanup
        if self.zulip:
            self.zulip = None

        while not self.disconnect:
            if self.name not in self.serv.config["organizations"]:
                self.send_notice(
                    "This organization does not exist on this bridge anymore."
                )
                return

            try:
                asyncio.ensure_future(
                    self.serv.push_bridge_state(
                        BridgeStateEvent.CONNECTING, remote_id=self.name
                    )
                )

                self.zulip = zulip.Client(
                    self.email, api_key=self.api_key, site=self.site
                )

                if not self.connected:
                    self.connected = True
                    await self.save()

                # awaiting above allows disconnect to happen in-between
                if self.zulip is None:
                    logging.debug("Zulip disconnected")
                    return

                self.disconnect = False
                self.connected_at = asyncio.get_running_loop().time()

                self.profile = self.zulip.get_profile()
                self.server = self.zulip.get_server_settings()

                self.zulip_handler = ZulipEventHandler(self)

                # Start Zulip event listerner
                asyncio.get_running_loop().run_in_executor(
                    None,
                    functools.partial(
                        self.zulip.call_on_each_event,
                        lambda event: self.zulip_handler.on_event(  # pylint: disable=unnecessary-lambda
                            event
                        ),
                        apply_markdown=True,
                    ),
                )

                asyncio.ensure_future(self._on_connect())

                self.send_notice(f"Connected to {self.site}")

                asyncio.ensure_future(
                    self.serv.push_bridge_state(
                        BridgeStateEvent.CONNECTED, remote_id=self.name
                    )
                )

                return
            except Exception as e:
                self.send_notice(f"Failed to connect: {str(e)}")

            if self.backoff < 1800:
                self.backoff = self.backoff * 2
            if self.backoff < 5:
                self.backoff = 5

            self.send_notice(f"Retrying in {self.backoff} seconds...")

            self.backoff_task = asyncio.ensure_future(asyncio.sleep(self.backoff))
            try:
                await self.backoff_task
            except asyncio.CancelledError:
                break
            finally:
                self.backoff_task = None

        self.send_notice("Connection aborted.")

    async def _on_connect(self):
        await self._get_users()
        await self._login_zulip_puppets()
        await self._sync_all_room_members()
        await self._sync_permissions()
        await self.backfill_messages()

    async def _get_users(self):
        result = self.zulip.get_members()
        if result["result"] != "success":
            raise Exception(f"Could not get Zulip users: {result['msg']}")
        for user in result["members"]:
            self.zulip_users[user["user_id"]] = user
            if not user["is_active"]:
                self.deactivated_users.add(str(user["user_id"]))

    async def _login_zulip_puppets(self):
        for user_id, login in self.zulip_puppet_login.items():
            await self.login_zulip_puppet(user_id, login["email"], login["api_key"])

    async def login_zulip_puppet(self, user_id: "UserID", email: str, api_key: str):
        """Create a Zulip puppet

        Args:
            user_id (str): MXID
            email (str): Zulip account email
            api_key (str): Zulip account API key
        """
        client = zulip.Client(email, api_key=api_key, site=self.site)
        self.zulip_puppets[user_id] = client
        profile = client.get_profile()
        if "user_id" not in profile:
            return
        self.zulip_puppet_user_mxid[str(profile["user_id"])] = user_id

        # Create event queue for receiving DMs
        asyncio.get_running_loop().run_in_executor(
            None,
            functools.partial(
                client.call_on_each_event,
                lambda event: self.on_puppet_event(  # pylint: disable=unnecessary-lambda
                    event
                ),
                apply_markdown=True,
                event_types=["message"],  # required for narrow
                narrow=[["is", "dm"]],
            ),
        )
        await self.save()
        return profile

    def delete_zulip_puppet(self, user_id: "UserID"):
        if user_id in self.zulip_puppets:
            del self.zulip_puppets[user_id]
        if user_id in self.zulip_puppet_user_mxid.inv:
            del self.zulip_puppet_user_mxid.inv[user_id]
        if user_id in self.zulip_puppet_login:
            del self.zulip_puppet_login[user_id]

    async def _sync_permissions(self):
        # Owner should have the highest permissions (after bot)
        self.permissions[self.serv.config["owner"]] = 99

        # arbitrary translations of Zulip roles to Matrix permissions
        role_permission_mapping = {
            100: 95,  # owner
            200: 80,  # administrator
            300: 50,  # moderator
            400: 0,  # member
            600: 0,  # guest
        }

        for zulip_user_id, user in self.zulip_users.items():
            user_id = self.serv.get_mxid_from_zulip_user_id(self, zulip_user_id)
            power_level = role_permission_mapping[user["role"]]
            self.permissions[user_id] = power_level

        rooms = set(self.rooms.values())
        rooms.add(self)
        rooms.add(self.space)
        logging.info(len(rooms))

        for room in rooms:
            if not isinstance(room, (StreamRoom, OrganizationRoom, SpaceRoom)):
                continue
            logging.debug(f"Synching permissions in {self.name} - {room.id}")
            await room.sync_permissions(self.permissions)

    async def _sync_all_room_members(self):
        result = self.zulip.get_subscriptions(request={"include_subscribers": True})
        if result["result"] != "success":
            logging.error(
                f"Getting subscriptions for {self.name} failed! Message: {result['msg']}"
            )
            return
        streams = result["subscriptions"]
        for stream in streams:
            room = self.rooms.get(stream["stream_id"])
            if not room or not isinstance(room, StreamRoom):
                continue
            asyncio.ensure_future(room.sync_zulip_members(stream["subscribers"]))

    def get_zulip_user(self, user_id: "ZulipUserID", update_cache: bool = False):
        if update_cache or user_id not in self.zulip_users:
            result = self.zulip.get_user_by_id(user_id)
            if result["result"] != "success":
                return None
            self.zulip_users[user_id] = result["user"]
        return self.zulip_users[user_id]

    def get_zulip_user_id_from_mxid(self, mxid: "UserID") -> Optional["ZulipUserID"]:
        if self.serv.is_puppet(mxid):
            ret = re.search(
                rf"@{self.serv.puppet_prefix}{self.name.lower()}{self.serv.puppet_separator}(\d+):{self.serv.server_name}",
                mxid,
            )
            return ret.group(1)
        elif mxid in self.zulip_puppet_user_mxid.inv:
            return self.zulip_puppet_user_mxid.inv[mxid]
        else:
            return None

    async def backfill_messages(self):
        for room in self.rooms.values():
            if not isinstance(room, StreamRoom):
                continue
            if room.max_backfill_amount == 0:
                continue

            await room.backfill_messages()

        for room in self.direct_rooms.values():
            await room.backfill_messages()

    def on_puppet_event(self, event: dict) -> None:
        if event["type"] != "message":
            return
        self.dm_message(event["message"])

    def dm_message(self, message: dict) -> None:
        event = {"type": "_dm_message", "message": message}
        self._queue.enqueue(event)

    async def _flush_event(self, event: dict):
        if event["type"] == "_dm_message":
            await self.zulip_handler.handle_dm_message(event["message"])
        else:
            return await super()._flush_event(event)
