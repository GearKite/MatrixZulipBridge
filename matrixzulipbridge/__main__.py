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
import grp
import logging
import os
import pwd
import random
import re
import string
import sys
import urllib
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Optional

from mautrix.api import HTTPAPI, Method, Path, SynapseAdminPath
from mautrix.appservice import AppService as MauService
from mautrix.appservice.state_store import ASStateStore
from mautrix.client.state_store.memory import MemoryStateStore
from mautrix.errors import (
    MatrixConnectionError,
    MatrixRequestError,
    MForbidden,
    MNotFound,
    MUserInUse,
)
from mautrix.types import Membership
from mautrix.util.bridge_state import BridgeState, BridgeStateEvent
from mautrix.util.config import yaml

from matrixzulipbridge import __version__
from matrixzulipbridge.appservice import AppService
from matrixzulipbridge.control_room import ControlRoom
from matrixzulipbridge.direct_room import DirectRoom
from matrixzulipbridge.organization_room import OrganizationRoom
from matrixzulipbridge.personal_room import PersonalRoom
from matrixzulipbridge.room import Room, RoomInvalidError
from matrixzulipbridge.space_room import SpaceRoom
from matrixzulipbridge.stream_room import StreamRoom
from matrixzulipbridge.websocket import AppserviceWebsocket

try:  # Optionally load coloredlogs
    import coloredlogs
except ModuleNotFoundError:
    pass


if TYPE_CHECKING:
    from mautrix.types import Event, RoomID, UserID

    from matrixzulipbridge.types import ZulipUserID


class MemoryBridgeStateStore(ASStateStore, MemoryStateStore):
    def __init__(self) -> None:
        ASStateStore.__init__(self)
        MemoryStateStore.__init__(self)


class BridgeAppService(AppService):
    _api: HTTPAPI
    _rooms: dict[str, Room]
    _users: dict[str, str]

    DEFAULT_MEDIA_PATH = "/_matrix/media/v3/download/{netloc}{path}{filename}"

    registration: Optional[dict]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.registration = None
        self.puppet_separator = None
        self.puppet_prefix = None
        self.api = None
        self.synapse_admin = None
        self.endpoint = None

    async def push_bridge_state(
        self,
        state_event: BridgeStateEvent,
        error=None,
        message=None,
        ttl=None,
        remote_id=None,
    ) -> None:
        if (
            "zulipbridge" not in self.registration
            or "status_endpoint" not in self.registration["zulipbridge"]
        ):
            return

        state = BridgeState(
            state_event=state_event,
            error=error,
            message=message,
            ttl=ttl,
            remote_id=remote_id,
        )

        logging.debug(f"Updating bridge state {state}")

        await state.send(
            self.registration["zulipbridge"]["status_endpoint"],
            self.az.as_token,
            log=logging,
        )

    def register_room(self, room: Room):
        self._rooms[room.id] = room

    def unregister_room(self, room_id: "RoomID"):
        if room_id in self._rooms:
            del self._rooms[room_id]

    # this is mostly used by organization rooms at init, it's a bit slow
    def find_rooms(
        self, rtype=None, user_id: "UserID" = None, organization_id: "RoomID" = None
    ) -> list[Room]:
        ret = []

        if rtype is not None and not isinstance(rtype, str):
            rtype = rtype.__name__

        for room in self._rooms.values():
            if (
                (rtype is None or room.__class__.__name__ == rtype)
                and (user_id is None or room.user_id == user_id)
                and (organization_id is None or room.organization_id == organization_id)
            ):
                ret.append(room)

        return ret

    def is_admin(self, user_id: "UserID"):
        if user_id == self.config["owner"]:
            return True

        for mask, value in self.config["allow"].items():
            if fnmatch(user_id, mask) and value == "admin":
                return True

        return False

    def is_user(self, user_id: "UserID"):
        if self.is_admin(user_id):
            return True

        for mask in self.config["allow"].keys():
            if fnmatch(user_id, mask):
                return True

        return False

    def is_local(self, mxid: "UserID"):
        return mxid.endswith(":" + self.server_name)

    def is_puppet(self, mxid: "UserID") -> bool:
        """Checks whether a given MXID is our puppet

        Args:
            mxid (str): Matrix user ID

        Returns:
            bool:
        """
        return mxid.startswith("@" + self.puppet_prefix) and self.is_local(mxid)

    def get_mxid_from_zulip_user_id(
        self,
        organization: "OrganizationRoom",
        zulip_user_id: "ZulipUserID",
        at=True,
        server=True,
    ) -> "UserID":
        ret = re.sub(
            r"[^0-9a-z\-\.=\_/]",
            lambda m: "=" + m.group(0).encode("utf-8").hex(),
            f"{self.puppet_prefix}{organization.name}{self.puppet_separator}{zulip_user_id}".lower(),
        )
        # ret = f"{self.puppet_prefix}{organization.site}{self.puppet_separator}{zulip_user_id}".lower()

        if at:
            ret = "@" + ret

        if server:
            ret += ":" + self.server_name

        return ret

    async def cache_user(self, user_id: "UserID", displayname: str):
        # start by caching that the user_id exists without a displayname
        if user_id not in self._users:
            self._users[user_id] = None

        # if the cached displayname is incorrect
        if displayname and self._users[user_id] != displayname:
            try:
                await self.az.intent.user(user_id).set_displayname(displayname)
                self._users[user_id] = displayname
            except MatrixRequestError as e:
                logging.warning(
                    f"Failed to set displayname '{displayname}' for user_id '{user_id}', got '{e}'"
                )

    def is_user_cached(self, user_id: "UserID", displayname: str = None):
        return user_id in self._users and (
            displayname is None or self._users[user_id] == displayname
        )

    async def ensure_zulip_user_id(
        self,
        organization: "OrganizationRoom",
        zulip_user_id: "ZulipUserID" = None,
        update_cache=True,
        zulip_user: dict = None,
    ):
        if zulip_user_id is None:
            zulip_user_id = zulip_user["user_id"]

        mx_user_id = self.get_mxid_from_zulip_user_id(organization, zulip_user_id)

        # if we've seen this user before, we can skip registering
        if not self.is_user_cached(mx_user_id):
            await self.az.intent.user(mx_user_id).ensure_registered()

        # always ensure the displayname is up-to-date
        if update_cache:
            zulip_user = organization.get_zulip_user(zulip_user_id)
            await self.cache_user(mx_user_id, zulip_user["full_name"])

        return mx_user_id

    async def _on_mx_event(self, event: "Event"):
        if event.room_id and event.room_id in self._rooms:
            try:
                room = self._rooms[event.room_id]
                await room.on_mx_event(event)
            except RoomInvalidError:
                logging.info(
                    f"Event handler for {event.type} threw RoomInvalidError, leaving and cleaning up."
                )
                self.unregister_room(room.id)
                room.cleanup()

                await self.leave_room(room.id, room.members)
            except Exception:
                logging.exception(
                    "Ignoring exception from room handler. This should be fixed."
                )
        elif (
            str(event.type) == "m.room.member"
            and event.sender != self.user_id
            and event.content.membership == Membership.INVITE
        ):
            # set owner if we have none and the user is from the same HS
            if self.config.get("owner", None) is None and event.sender.endswith(
                ":" + self.server_name
            ):
                logging.info(f"We have an owner now, let us rejoice, {event.sender}!")
                self.config["owner"] = event.sender
                await self.save()

            if not self.is_user(event.sender):
                logging.info(
                    f"Non-whitelisted user {event.sender} tried to invite us, ignoring."
                )
                return
            else:
                logging.info(f"Got an invite from {event.sender}")

            if not event.content.is_direct:
                logging.debug("Got an invite to non-direct room, ignoring")
                return

            # only respond to invites unknown new rooms
            if event.room_id in self._rooms:
                logging.debug("Got an invite to room we're already in, ignoring")
                return

            # handle invites against puppets
            if event.state_key != self.user_id:
                logging.info(
                    f"Whitelisted user {event.sender} invited {event.state_key}, going to reject."
                )

                try:
                    await self.az.intent.user(event.state_key).kick_user(
                        event.room_id,
                        event.state_key,
                        "Will invite YOU instead",
                    )
                except Exception:
                    logging.exception("Failed to reject invitation.")

                raise NotImplementedError(
                    "Puppet invites as profile query"
                )  #:TODO: implement

                for room in self.find_rooms(OrganizationRoom, event.sender):
                    pass

                return

            logging.info(
                f"Whitelisted user {event.sender} invited us, going to accept."
            )

            # accept invite sequence
            try:
                room = ControlRoom(
                    id=event.room_id,
                    user_id=event.sender,
                    serv=self,
                    members=[event.sender],
                    bans=[],
                )
                await room.save()
                self.register_room(room)

                await self.az.intent.join_room(room.id)

                # show help on open
                await room.show_help()
            except Exception:
                if event.room_id in self._rooms:
                    del self._rooms[event.room_id]
                logging.exception("Failed to create control room.")
        else:
            pass

    async def detect_public_endpoint(self):
        async with self.api.session as session:
            # first try https well-known
            try:
                resp = await session.request(
                    "GET",
                    f"https://{self.server_name}/.well-known/matrix/client",
                )
                data = await resp.json(content_type=None)
                return data["m.homeserver"]["base_url"]
            except Exception:
                logging.debug("Did not find .well-known for HS")

            # try https directly
            try:
                resp = await session.request(
                    "GET", f"https://{self.server_name}/_matrix/client/versions"
                )
                await resp.json(content_type=None)
                return f"https://{self.server_name}"
            except Exception:
                logging.debug("Could not use direct connection to HS")

            # give up
            logging.warning(
                "Using internal URL for homeserver, media links are likely broken!"
            )
            return str(self.api.base_url)

    def mxc_to_url(self, mxc: str, filename: str = None):
        mxc = urllib.parse.urlparse(mxc)

        if filename is None:
            filename = ""
        else:
            filename = "/" + urllib.parse.quote(filename)

        media_path = self.media_path.format(
            netloc=mxc.netloc, path=mxc.path, filename=filename
        )

        return self.endpoint + media_path

    async def reset(self, config_file, homeserver_url):
        with open(config_file, encoding="utf-8") as f:
            registration = yaml.load(f)

        api = HTTPAPI(base_url=homeserver_url, token=registration["as_token"])
        whoami = await api.request(Method.GET, Path.v3.account.whoami)
        self.user_id = whoami["user_id"]
        self.server_name = self.user_id.split(":", 1)[1]
        logging.info("We are " + whoami["user_id"])

        self.az = MauService(
            id=registration["id"],
            domain=self.server_name,
            server=homeserver_url,
            as_token=registration["as_token"],
            hs_token=registration["hs_token"],
            bot_localpart=registration["sender_localpart"],
            state_store=MemoryBridgeStateStore(),
        )

        try:
            await self.az.start(host="127.0.0.1", port=None)
        except Exception:
            logging.exception("Failed to listen.")
            return

        joined_rooms = await self.az.intent.get_joined_rooms()
        logging.info(f"Leaving from {len(joined_rooms)} rooms...")

        for room_id in joined_rooms:
            logging.info(f"Leaving from {room_id}...")
            await self.leave_room(room_id, None)

        logging.info("Resetting configuration...")
        self.config = {}
        await self.save()

        logging.info("All done!")

    def load_reg(self, config_file):
        with open(config_file, encoding="utf-8") as f:
            self.registration = yaml.load(f)

    async def leave_room(self, room_id: "RoomID", members: list["UserID"]):
        members = members if members else []

        for member in members:
            (name, server) = member.split(":", 1)

            if name.startswith("@" + self.puppet_prefix) and server == self.server_name:
                try:
                    await self.az.intent.user(member).leave_room(room_id)
                except Exception:
                    logging.exception("Removing puppet on leave failed")

        try:
            await self.az.intent.leave_room(room_id)
        except MatrixRequestError:
            pass
        try:
            await self.az.intent.forget_room(room_id)
        except MatrixRequestError:
            pass

    def _keepalive(self):
        async def put_presence():
            try:
                await self.az.intent.set_presence(self.user_id)
            except Exception:
                pass

        asyncio.ensure_future(put_presence())
        asyncio.get_running_loop().call_later(60, self._keepalive)

    async def run(
        self, listen_address, listen_port, homeserver_url, owner, unsafe_mode
    ):
        if "sender_localpart" not in self.registration:
            logging.critical("Missing sender_localpart from registration file.")
            sys.exit(1)

        if (
            "namespaces" not in self.registration
            or "users" not in self.registration["namespaces"]
        ):
            logging.critical("User namespaces missing from registration file.")
            sys.exit(1)

        # remove self namespace if exists
        ns_users = [
            x
            for x in self.registration["namespaces"]["users"]
            if x["regex"].split(":")[0] != f"@{self.registration['sender_localpart']}"
        ]

        if len(ns_users) != 1:
            logging.critical(
                "A single user namespace is required for puppets in the registration file."
            )
            sys.exit(1)

        if "exclusive" not in ns_users[0] or not ns_users[0]["exclusive"]:
            logging.critical("User namespace must be exclusive.")
            sys.exit(1)

        m = re.match(r"^@(.+)([\_/])\.[\*\+]:?", ns_users[0]["regex"])
        if not m:
            logging.critical(
                "User namespace regex must be an exact prefix like '@zulip_bridge_.*' that includes the separator character (_ or /)."
            )
            sys.exit(1)

        self.puppet_separator = m.group(2)
        self.puppet_prefix = m.group(1) + self.puppet_separator

        logging.info(f"zulipbridge v{__version__}")
        if unsafe_mode:
            logging.warning("Running in unsafe mode, bridge may leave rooms on error")

        url = urllib.parse.urlparse(homeserver_url)
        ws = None
        if url.scheme in ["ws", "wss"]:
            logging.info(
                f"Using websockets to receive transactions. Listening is still enabled on http://{listen_address}:{listen_port}"
            )
            ws = AppserviceWebsocket(
                homeserver_url, self.registration["as_token"], self._on_mx_event
            )
            homeserver_url = url._replace(
                scheme=("https" if url.scheme == "wss" else "http")
            ).geturl()
            logging.info(f"Connecting to HS at {homeserver_url}")

        self.api = HTTPAPI(base_url=homeserver_url, token=self.registration["as_token"])

        # conduit requires that the appservice user is registered before whoami
        wait = 0
        while True:
            try:
                await self.api.request(
                    Method.POST,
                    Path.v3.register,
                    {
                        "type": "m.login.application_service",
                        "username": self.registration["sender_localpart"],
                    },
                )
                logging.debug("Appservice user registration succeeded.")
                break
            except MUserInUse:
                logging.debug("Appservice user is already registered.")
                break
            except MatrixConnectionError as e:
                if wait < 30:
                    wait += 5
                logging.warning(
                    f"Failed to connect to HS: {e}, retrying in {wait} seconds..."
                )
                await asyncio.sleep(wait)
            except Exception:
                logging.exception(
                    "Unexpected failure when registering appservice user."
                )
                sys.exit(1)

        # mautrix migration requires us to call whoami manually at this point
        whoami = await self.api.request(Method.GET, Path.v3.account.whoami)

        logging.info("We are %s", whoami["user_id"])

        self.user_id = whoami["user_id"]
        self.server_name = self.user_id.split(":", 1)[1]

        self.az = MauService(
            id=self.registration["id"],
            domain=self.server_name,
            server=homeserver_url,
            as_token=self.registration["as_token"],
            hs_token=self.registration["hs_token"],
            bot_localpart=self.registration["sender_localpart"],
            state_store=MemoryBridgeStateStore(),
        )
        self.az.matrix_event_handler(self._on_mx_event)

        try:
            await self.az.start(host=listen_address, port=listen_port)
        except Exception:
            logging.exception("Failed to listen.")
            sys.exit(1)

        try:
            await self.az.intent.ensure_registered()
            logging.debug("Appservice user exists at least now.")
        except Exception:
            logging.exception("Unexpected failure when registering appservice user.")
            sys.exit(1)

        if (
            "zulipbridge" in self.registration
            and "displayname" in self.registration["zulipbridge"]
        ):
            try:
                logging.debug(
                    f"Overriding displayname from registration file to {self.registration['zulipbridge']['displayname']}"
                )
                await self.az.intent.set_displayname(
                    self.registration["zulipbridge"]["displayname"]
                )
            except MatrixRequestError as e:
                logging.warning(f"Failed to set displayname: {str(e)}")

        self._rooms = {}
        self._users = {}
        self.config = {
            "organizations": {},
            "owner": None,
            "member_sync": "half",
            "media_url": None,
            "media_path": None,
            "namespace": self.puppet_prefix,
            "allow": {},
        }
        logging.debug(f"Default config: {self.config}")
        self.synapse_admin = False

        try:
            is_admin = await self.api.request(
                Method.GET, SynapseAdminPath.v1.users[self.user_id].admin
            )
            self.synapse_admin = is_admin["admin"]
        except MForbidden:
            logging.info(
                f"We ({self.user_id}) are not a server admin, inviting puppets is required."
            )
        except Exception:
            logging.info(
                "Seems we are not connected to Synapse, inviting puppets is required."
            )

        # load config from HS
        await self.load()

        async def _resolve_media_endpoint():
            endpoint = await self.detect_public_endpoint()

            # only rewrite it if it wasn't changed
            if self.endpoint == str(self.api.base_url):
                self.endpoint = endpoint

            logging.info("Homeserver is publicly available at " + self.endpoint)

        # use configured media_url for endpoint if we have it
        if (
            "zulipbridge" in self.registration
            and "media_url" in self.registration["zulipbridge"]
        ):
            logging.debug(
                f"Overriding media URL from registration file to {self.registration['zulipbridge']['media_url']}"
            )
            self.endpoint = self.registration["zulipbridge"]["media_url"]
        elif self.config["media_url"]:
            self.endpoint = self.config["media_url"]
        else:
            logging.info(
                "Trying to detect homeserver public endpoint, this might take a while..."
            )
            self.endpoint = str(self.api.base_url)
            asyncio.ensure_future(_resolve_media_endpoint())

        # use configured media_path for media_path if we have it
        if (
            "zulipbridge" in self.registration
            and "media_path" in self.registration["zulipbridge"]
        ):
            logging.debug(
                f"Overriding media path from registration file to {self.registration['zulipbridge']['media_path']}"
            )
            self.media_path = self.registration["zulipbridge"]["media_path"]
        elif self.config["media_path"]:
            self.media_path = self.config["media_path"]
        else:
            self.media_path = self.DEFAULT_MEDIA_PATH

        logging.info("Starting presence loop")
        self._keepalive()

        # prevent starting bridge with changed namespace
        if self.config["namespace"] != self.puppet_prefix:
            logging.error(
                f"Previously used namespace '{self.config['namespace']}' does not match current '{self.puppet_prefix}'."
            )
            sys.exit(1)

        # honor command line owner
        if owner is not None and self.config["owner"] != owner:
            logging.info(f"Overriding loaded owner with '{owner}'")
            self.config["owner"] = owner

        # always ensure our merged and migrated configuration is up-to-date
        await self.save()

        logging.info("Fetching joined rooms...")

        joined_rooms = await self.az.intent.get_joined_rooms()
        logging.debug(f"Appservice rooms: {joined_rooms}")

        logging.info(f"Bridge is in {len(joined_rooms)} rooms, initializing them...")

        Room.init_class(self.az)

        # room types and their init order, organization must be before chat and group
        room_types = [
            ControlRoom,
            OrganizationRoom,
            DirectRoom,
            StreamRoom,
            PersonalRoom,
            SpaceRoom,
        ]

        room_type_map = {}
        for room_type in room_types:
            room_type.init_class(self.az)
            room_type_map[room_type.__name__] = room_type

        # we always auto-open control room for owner
        owner_control_open = False

        # import all rooms
        for room_id in joined_rooms:
            joined = {}

            try:
                config = await self.az.intent.get_account_data("zulip", room_id)

                if "type" not in config or "user_id" not in config:
                    raise Exception("Invalid config")

                cls = room_type_map.get(config["type"])
                if not cls:
                    raise Exception("Unknown room type")

                # refresh room members state
                await self.az.intent.get_room_members(room_id)

                joined = await self.az.state_store.get_member_profiles(
                    room_id, (Membership.JOIN,)
                )
                banned = await self.az.state_store.get_members(
                    room_id, (Membership.BAN,)
                )

                room = cls(
                    id=room_id,
                    user_id=config["user_id"],
                    serv=self,
                    members=joined.keys(),
                    bans=banned,
                )
                room.from_config(config)

                # add to room displayname
                for user_id, member in joined.items():
                    if member.displayname is not None:
                        room.displaynames[user_id] = member.displayname
                    # add to global puppet cache if it's a puppet
                    if self.is_puppet(user_id):
                        self._users[user_id] = member.displayname

                # only add valid rooms to event handler
                if room.is_valid():
                    self._rooms[room_id] = room
                else:
                    room.cleanup()
                    raise Exception("Room validation failed after init")

                if cls == ControlRoom and room.user_id == self.config["owner"]:
                    owner_control_open = True
            except MNotFound:
                logging.error(
                    f"Leaving room with no data: {room_id}. How did this happen?"
                )
                self.unregister_room(room_id)
                await self.leave_room(room_id, joined.keys())
            except Exception:
                logging.exception(
                    f"Failed to reconfigure room {room_id} during init, leaving."
                )

                # regardless of safe mode, we ignore this room
                self.unregister_room(room_id)

                if unsafe_mode:
                    await self.leave_room(room_id, joined.keys())

        logging.info("All valid rooms initialized, connecting organization rooms...")

        wait = 1
        for room in list(self._rooms.values()):
            await room.post_init()

            # check again if we're still valid
            if not room.is_valid():
                logging.debug(
                    f"Room {room.id} failed validation after post init, leaving."
                )

                self.unregister_room(room.id)

                if unsafe_mode:
                    await self.leave_room(room.id, room.members)

                continue

            # connect organization rooms one by one, this may take a while
            if isinstance(room, OrganizationRoom) and not room.connected:

                def sync_connect(room):
                    asyncio.ensure_future(room.connect())

                asyncio.get_running_loop().call_later(wait, sync_connect, room)
                wait += 1

        logging.info(
            f"Init done with {wait-1} organizations connecting, bridge is now running!"
        )

        await self.push_bridge_state(BridgeStateEvent.UNCONFIGURED)

        # late start WS to avoid getting transactions too early
        if ws:
            await ws.start()

        if self.config["owner"] and not owner_control_open:
            logging.info(f"Opening control room for owner {self.config['owner']}")
            try:
                room_id = await self.az.intent.create_room(
                    invitees=[self.config["owner"]],
                    custom_request_fields={"com.beeper.auto_join_invites": True},
                )

                room = ControlRoom(
                    id=room_id,
                    user_id=self.config["owner"],
                    serv=self,
                    members=[self.config["owner"]],
                    bans=[],
                )
                await room.save()
                self.register_room(room)

                await self.az.intent.join_room(room.id)

                # show help on open
                await room.show_help()
            except Exception:
                logging.error("Failed to create control room, huh")

        await asyncio.Event().wait()


async def async_main():
    parser = argparse.ArgumentParser(
        prog=os.path.basename(sys.executable) + " -m " + __package__,
        description=f"A puppeting Matrix - Zulip appservice bridge (v{__version__})",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        help="log debug messages",
        action="store_true",
        default=argparse.SUPPRESS,
    )
    req = parser.add_mutually_exclusive_group(required=True)
    req.add_argument(
        "-c",
        "--config",
        help="registration YAML file path, must be writable if generating",
    )
    req.add_argument(
        "--version",
        action="store_true",
        help="show bridge version",
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-l",
        "--listen-address",
        help="bridge listen address (default: as specified in url in config, 127.0.0.1 otherwise)",
    )
    parser.add_argument(
        "-p",
        "--listen-port",
        help="bridge listen port (default: as specified in url in config, 28464 otherwise)",
        type=int,
    )
    parser.add_argument("-u", "--uid", help="user id to run as", default=None)
    parser.add_argument("-g", "--gid", help="group id to run as", default=None)
    parser.add_argument(
        "--generate",
        action="store_true",
        help="generate registration YAML for Matrix homeserver (Synapse)",
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--generate-compat",
        action="store_true",
        help="generate registration YAML for Matrix homeserver (Dendrite and Conduit)",
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="reset ALL bridge configuration from homeserver and exit",
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--unsafe-mode",
        action="store_true",
        help="allow appservice to leave rooms on error",
    )
    parser.add_argument(
        "-o",
        "--owner",
        help="set owner MXID (eg: @user:homeserver) or first talking local user will claim the bridge",
        default=None,
    )
    parser.add_argument(
        "homeserver",
        nargs="?",
        help="URL of Matrix homeserver",
        default="http://localhost:8008",
    )

    args = parser.parse_args()

    logging_level = logging.INFO
    if "verbose" in args:
        logging_level = logging.DEBUG

    logging.basicConfig(stream=sys.stdout, level=logging_level)

    try:
        coloredlogs.install(logging_level)
    except NameError:
        pass

    if "generate" in args or "generate_compat" in args:
        letters = string.ascii_letters + string.digits

        registration = {
            "id": "zulipbridge",
            "url": f"http://{args.listen_address or '127.0.0.1'}:{args.listen_port or 28464}",
            "as_token": "".join(random.choice(letters) for i in range(64)),
            "hs_token": "".join(random.choice(letters) for i in range(64)),
            "rate_limited": False,
            "sender_localpart": "zulipbridge",
            "namespaces": {
                "users": [{"regex": "@zulip_.*", "exclusive": True}],
                "aliases": [],
                "rooms": [],
            },
        }

        if "generate_compat" in args:
            registration["namespaces"]["users"].append(
                {"regex": "@zulipbridge:.*", "exclusive": True}
            )

        if os.path.isfile(args.config):
            logging.critical("Registration file already exists, not overwriting.")
            sys.exit(1)

        if args.config == "-":
            yaml.dump(registration, sys.stdout)
        else:
            with open(args.config, "w", encoding="utf-8") as f:
                yaml.dump(registration, f)

            logging.info(f"Registration file generated and saved to {args.config}")
    elif "reset" in args:
        service = BridgeAppService()
        logging.warning("Resetting will delete all bridge data, this is irreversible!")
        await asyncio.sleep(3)  # Gotta be careful
        if input("Are you SURE you want to continue? [y/n] ").lower() == "y":
            await service.reset(args.config, args.homeserver)
        else:
            logging.info("Not doing anything.")
            sys.exit(0)
    elif "version" in args:
        logging.info(__version__)
    else:
        service = BridgeAppService()

        service.load_reg(args.config)

        if os.getuid() == 0:
            if args.gid:
                gid = grp.getgrnam(args.gid).gr_gid
                os.setgid(gid)
                os.setgroups([])

            if args.uid:
                uid = pwd.getpwnam(args.uid).pw_uid
                os.setuid(uid)

        os.umask(0o077)

        listen_address = args.listen_address
        listen_port = args.listen_port

        if not listen_address:
            listen_address = "127.0.0.1"

            try:
                url = urllib.parse.urlparse(service.registration["url"])
                if url.hostname:
                    listen_address = url.hostname
            except Exception:
                pass

        if not listen_port:
            listen_port = 28464

            try:
                url = urllib.parse.urlparse(service.registration["url"])
                if url.port:
                    listen_port = url.port
            except Exception:
                pass

        await service.run(
            listen_address, listen_port, args.homeserver, args.owner, args.unsafe_mode
        )


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
