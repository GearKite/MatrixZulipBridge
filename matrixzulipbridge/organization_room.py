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
import hashlib
import html
import logging
from argparse import Namespace
from base64 import b32encode
from collections import defaultdict
from typing import TYPE_CHECKING, Any
import json
import zulip

from mautrix.util.bridge_state import BridgeStateEvent

from matrixzulipbridge import __version__
from matrixzulipbridge.stream_room import StreamRoom
from matrixzulipbridge.command_parse import CommandManager
from matrixzulipbridge.command_parse import CommandParser
from matrixzulipbridge.command_parse import CommandParserError
from matrixzulipbridge.private_room import PrivateRoom
from matrixzulipbridge.room import Room
from matrixzulipbridge.space_room import SpaceRoom
from matrixzulipbridge.zulip import ZulipEventHandler

if TYPE_CHECKING:
    from matrixzulipbridge.appservice import AppService


def connected(f):
    def wrapper(*args, **kwargs):
        self = args[0]

        if not self.zulip or not self.zulip.has_connected:
            self.send_notice("Need to be connected to use this command.")
            return asyncio.sleep(0)

        return f(*args, **kwargs)

    return wrapper


class OrganizationRoom(Room):
    # configuration stuff
    name: str
    connected: bool
    fullname: str

    api_key: str
    email: str
    site: str
    zulip: "zulip.Client"

    # state
    commands: CommandManager
    rooms: dict[str, Room]
    connecting: bool
    real_host: str
    real_user: str
    pending_kickbans: dict[str, list[tuple[str, str]]]
    backoff: int
    backoff_task: Any
    next_server: int
    connected_at: int
    space: SpaceRoom
    post_init_done: bool
    caps_supported: list
    caps_enabled: list
    caps_task: Any
    disconnect: bool

    organization: Any

    profile: dict
    messages: dict[int, str]
    permissions: dict[str, str]

    def init(self):
        self.name = None
        self.connected = False
        self.fullname = None
        self.ircname = None
        self.password = None
        self.sasl_mechanism = None
        self.sasl_username = None
        self.sasl_password = None
        self.autocmd = None
        self.pills_length = 2
        self.pills_ignore = []
        self.autoquery = True
        self.allow_ctcp = False
        self.tls_cert = None
        self.rejoin_invite = True
        self.rejoin_kick = False
        self.caps = ["message-tags", "chghost", "znc.in/self-message"]
        self.forward = False
        self.backoff = 0
        self.backoff_task = None
        self.next_server = 0
        self.connected_at = 0

        self.api_key = None
        self.email = None
        self.site = None

        self.commands = CommandManager()
        self.zulip = None
        self.rooms = {}
        self.connlock = asyncio.Lock()
        self.disconnect = True
        self.real_host = "?" * 63  # worst case default
        self.real_user = "?" * 8  # worst case default
        self.keys = {}  # temp dict of join channel keys
        self.keepnick_task = None  # async task
        self.whois_data = defaultdict(dict)  # buffer for keeping partial whois replies
        self.pending_kickbans = defaultdict(list)
        self.space = None
        self.post_init_done = False
        self.caps_supported = []
        self.caps_enabled = []
        self.caps_task = None

        self.organization = self

        self.profile = None
        self.messages = {}
        self.permissions = {}

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
            epilog=(),
        )
        cmd.add_argument("site", nargs="?", help="new site")
        self.commands.register(cmd, self.cmd_site)

        cmd = CommandParser(
            prog="EMAIL",
            description="set Zulip bot email",
            epilog=(),
        )
        cmd.add_argument("email", nargs="?", help="new bot email")
        self.commands.register(cmd, self.cmd_email)

        cmd = CommandParser(
            prog="APIKEY",
            description="set Zulip bot api key",
            epilog=(),
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
                "Manually subscribe to a stream and bridge it\n",
                "\n",
                "Any subscriptions will be persisted between reconnects.\n",
            ),
        )
        cmd.add_argument("stream", help="target stream")
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

        self.mx_register("m.room.message", self.on_mx_message)

    @staticmethod
    async def create(serv: "AppService", organization: dict, user_id: str, name):
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
            raise Exception("No name key in config for OrganizationRoom")

        if "api_key" in config and config["api_key"]:
            self.api_key = config["api_key"]

        if "email" in config and config["email"]:
            self.email = config["email"]

        if "site" in config and config["site"]:
            self.site = config["site"]

        if "messages" in config and config["messages"]:
            self.messages = config["messages"]

    def to_config(self) -> dict:
        return {
            "name": self.name,
            "api_key": self.api_key,
            "email": self.email,
            "site": self.site,
            "messages": self.messages,
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

        if self.caps_task:
            self.caps_task.cancel()
            self.caps_task = None
            logging.debug("... cancelled caps task")

        if self.backoff_task:
            self.backoff_task.cancel()
            self.backoff_task = None
            logging.debug("... cancelled backoff task")

        if self.zulip:
            raise NotImplementedError("Zulip disconnect is not implemented")

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

        if self.caps_task:
            self.caps_task.cancel()
            self.caps_task = None

        self.backoff = 0
        self.next_server = 0
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
            if stream.lower() == room.name:
                self.send_notice(f"Stream {stream} already exists at {room.id}.")
                return

        self.zulip.add_subscriptions([{"name": stream}])
        await StreamRoom.create(organization=self, name=stream)

    @connected
    async def cmd_unsubscribe(self, args) -> None:
        stream = args.stream.lower()

        removed_rooms = False

        for room in self.rooms.values():
            if room.name.lower() == stream:
                removed_rooms = True

                self.serv.unregister_room(room.id)
                room.cleanup()
                await self.serv.leave_room(room.id, room.members)
                del self.rooms[room.stream_id]

                self.zulip.remove_subscriptions([stream])
                self.send_notice(
                    f"Unsubscribed from {stream} and removed room {room.id}."
                )

        if not removed_rooms:
            self.send_notice("No room with that name exists.")

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

            if self.real_host[0] != "?":
                self.send_notice(f"Connected from host {self.real_host}")
        else:
            self.send_notice("Not connected to server.")

        pms = []
        chans = []
        plumbs = []

        for room in self.rooms.values():
            match room:
                case PrivateRoom():
                    pms.append(room.name)
                case StreamRoom():
                    chans.append(room.name)

        if len(chans) > 0:
            self.send_notice(f"Channels: {', '.join(chans)}")

        if len(plumbs) > 0:
            self.send_notice(f"Plumbs: {', '.join(plumbs)}")

        if len(pms) > 0:
            self.send_notice(f"PMs: {', '.join(pms)}")

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
        for room_type in [PrivateRoom, StreamRoom]:
            for room in self.serv.find_rooms(room_type, self.user_id):
                if room.stream_id not in self.rooms and (
                    room.organization_id == self.id
                    or (
                        room.organization_id is None
                        and room.organization_name == self.name
                    )
                ):
                    room.organization = self
                    # this doubles as a migration
                    if room.organization_id is None:
                        logging.debug(f"{self.id} attaching and migrating {room.id}")
                        room.organization_id = self.id
                        await room.save()
                    else:
                        logging.debug(f"{self.id} attaching {room.id}")
                    self.rooms[room.stream_id] = room

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

        # reset whois and kickbans buffers
        self.whois_data.clear()
        self.pending_kickbans.clear()

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

                zulip_handler = ZulipEventHandler(self)

                # Start Zulip event listerner
                asyncio.get_running_loop().run_in_executor(
                    None,
                    self.zulip.call_on_each_event,
                    lambda event: zulip_handler.on_event(
                        event
                    ),  # pylint: disable=unnecessary-lambda
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

    def on_disconnect(self, conn, event) -> None:
        if self.caps_task:
            self.caps_task.cancel()
            self.caps_task = None

        if self.zulip:
            self.zulip = None

        # if we were connected for a while, consider the server working
        if (
            self.connected_at > 0
            and asyncio.get_running_loop().time() - self.connected_at > 300
        ):
            self.backoff = 0
            self.next_server = 0
            self.connected_at = 0

        if self.connected and not self.disconnect:
            if self.backoff < 1800:
                self.backoff += 5

            self.send_notice(f"Disconnected, reconnecting in {self.backoff} seconds...")

            async def later(self):
                self.backoff_task = asyncio.ensure_future(asyncio.sleep(self.backoff))
                try:
                    await self.backoff_task
                    await self.connect()
                except asyncio.CancelledError:
                    self.send_notice("Reconnect cancelled.")
                finally:
                    self.backoff_task = None

            asyncio.ensure_future(later(self))
            asyncio.ensure_future(
                self.serv.push_bridge_state(
                    BridgeStateEvent.TRANSIENT_DISCONNECT, remote_id=self.name
                )
            )
        else:
            self.send_notice("Disconnected.")
            asyncio.ensure_future(
                self.serv.push_bridge_state(
                    BridgeStateEvent.LOGGED_OUT, remote_id=self.name
                )
            )

    def source_text(self, conn, event) -> str:
        source = None

        if event.source is not None:
            source = str(event.source.nick)

            if event.source.user is not None and event.source.host is not None:
                source += f" ({event.source.user}@{event.source.host})"
        else:
            source = conn.server

        return source

    def on_quit(self, conn, event) -> None:
        # leave channels
        for room in self.rooms.values():
            if isinstance(room, StreamRoom):
                room.on_quit(conn, event)

    async def _on_connect(self):
        await self._sync_permissions()
        await self._sync_all_room_members()

    async def _sync_permissions(self):
        # Owner should have the highest permissions (after bot)
        self.permissions[self.serv.config["owner"]] = 99

        # TODO: Get permissions from zulip

        rooms = set(self.rooms.values())
        rooms.add(self)
        rooms.add(self.space)
        logging.info(len(rooms))

        for room in rooms:
            if room is None:
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
