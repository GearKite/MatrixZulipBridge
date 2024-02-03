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

from mautrix.api import Method, Path
from mautrix.types import SpaceChildStateEventContent
from mautrix.types.event.type import EventType

from matrixzulipbridge.under_organization_room import UnderOrganizationRoom

if TYPE_CHECKING:
    from mautrix.types import RoomID

    from matrixzulipbridge.organization_room import OrganizationRoom


class SpaceRoom(UnderOrganizationRoom):
    name: str

    # pending rooms to attach during space creation
    pending: list[str]

    def init(self) -> None:
        super().init()

        self.name = None

        self.pending = []

    def is_valid(self) -> bool:
        if not super().is_valid():
            return False

        # we are valid as long as our user is in the room
        if not self.in_room(self.user_id):
            return False

        return True

    @staticmethod
    async def create(
        organization: "OrganizationRoom", initial_rooms: list["RoomID"]
    ) -> "SpaceRoom":
        logging.debug(
            f"SpaceRoom.create(organization='{organization.id}' ({organization.name}))"
        )

        room = SpaceRoom(
            None,
            organization.user_id,
            organization.serv,
            [organization.user_id, organization.serv.user_id],
            [],
        )
        room.name = organization.name
        room.organization = organization  # only used in create_finalize
        room.organization_id = organization.id
        room.pending += initial_rooms
        return room

    async def create_finalize(self) -> None:
        resp = await self.az.intent.api.request(
            Method.POST,
            Path.v3.createRoom,
            {
                "creation_content": {
                    "type": "m.space",
                },
                "visibility": "private",
                "name": self.organization.name,
                "topic": f"Organization space for {self.organization.name}",
                "invite": [self.organization.user_id],
                "is_direct": False,
                "initial_state": [
                    {
                        "type": "m.space.child",
                        "state_key": self.organization.id,
                        "content": {"via": [self.organization.serv.server_name]},
                    }
                ],
                "power_level_content_override": {
                    "events_default": 50,
                    "users_default": 0,
                    "invite": 50,
                    "kick": 50,
                    "redact": 50,
                    "ban": 50,
                    "events": {
                        "m.room.name": 0,
                        "m.room.avatar": 0,  # these work as long as rooms are private
                    },
                    "users": self.organization.permissions
                    | {self.organization.serv.user_id: 100},
                },
            },
        )

        self.id = resp["room_id"]
        self.serv.register_room(self)
        await self.save()

        # attach all pending rooms
        rooms = self.pending
        self.pending = []

        for room_id in rooms:
            await self.attach(room_id)

    def cleanup(self) -> None:
        try:
            organization = self.serv._rooms[self.organization_id]

            if organization.space == self:
                organization.space = None
                organization.space_id = None
                asyncio.ensure_future(organization.save())
                logging.debug(
                    f"Space {self.id} cleaned up from organization {organization.id}"
                )
            else:
                logging.debug(
                    f"Space room cleaned up as a duplicate for organization {organization.id}, probably fine."
                )
        except KeyError:
            logging.debug(
                f"Space room cleaned up with missing organization {self.organization_id}, probably fine."
            )

        super().cleanup()

    async def attach(self, room_id: "RoomID") -> None:
        # if we are attached between space request and creation just add to pending list
        if self.id is None:
            logging.debug(f"Queuing room {room_id} attachment to pending space.")
            self.pending.append(room_id)
            return

        logging.debug(f"Attaching room {room_id} to space {self.id}.")
        await self.az.intent.send_state_event(
            self.id,
            EventType.SPACE_CHILD,  # pylint: disable=no-member
            state_key=room_id,
            content=SpaceChildStateEventContent(via=[self.serv.server_name]),
        )

    async def detach(self, room_id: "RoomID") -> None:
        if self.id is not None:
            logging.debug(f"Detaching room {room_id} from space {self.id}.")
            await self.az.intent.send_state_event(
                self.id,
                EventType.SPACE_CHILD,  # pylint: disable=no-member
                state_key=room_id,
                content=SpaceChildStateEventContent(),
            )
        elif room_id in self.pending:
            logging.debug(f"Removing {room_id} from space {self.id} pending queue.")
            self.pending.remove(room_id)

    async def post_init(self) -> None:
        try:
            organization = self.serv._rooms[self.organization_id]
            if organization.space is not None:
                logging.warning(
                    f"Network room {organization.id} already has space {organization.space.id} but I'm {self.id}, we are dangling."
                )
                return

            organization.space = self
            logging.debug(f"Space {self.id} attached to organization {organization.id}")
        except KeyError:
            logging.debug(
                f"Network room {self.organization_id} was not found for space {self.id}, we are dangling."
            )
            self.organization_id = None
