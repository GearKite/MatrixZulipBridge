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
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from mautrix.api import Method, Path
from mautrix.errors import MNotFound

if TYPE_CHECKING:
    from mautrix.appservice import AppService as MauService
    from mautrix.types import RoomID, UserID

    from matrixzulipbridge.room import Room


class AppService(ABC):
    az: "MauService"

    user_id: "UserID"
    server_name: str
    config: dict

    async def load(self):
        try:
            self.config.update(await self.az.intent.get_account_data("zulip"))
        except MNotFound:
            await self.save()

    async def save(self):
        await self.az.intent.set_account_data("zulip", self.config)

    async def create_room(
        self,
        name: str,
        topic: str,
        invite: list["UserID"],
        restricted: str = None,
        permissions: dict = None,
        is_direct: bool = False,
    ) -> "RoomID":
        if permissions is None:
            permissions = {}

        req = {
            "visibility": "private",
            "name": name,
            "topic": topic,
            "invite": invite,
            "is_direct": is_direct,
            "power_level_content_override": {
                "users_default": 0,
                "invite": 50,
                "kick": 50,
                "redact": 50,
                "ban": 50,
                "events": {
                    "m.room.name": 0,
                    "m.room.avatar": 0,  # these work as long as rooms are private
                    "m.room.encryption": 100,
                    "m.space.parent": 90,
                },
                "users": {self.user_id: 100} | permissions,
            },
            "com.beeper.auto_join_invites": True,
        }

        if restricted is not None:
            resp = await self.az.intent.api.request(Method.GET, Path.v3.capabilities)
            try:
                def_ver = resp["capabilities"]["m.room_versions"]["default"]
            except KeyError:
                logging.debug("Unexpected capabilities reply")
                def_ver = None

            # If room version is in range of 1..8, request v9
            if def_ver in [str(v) for v in range(1, 9)]:
                req["room_version"] = "9"

            req["initial_state"] = [
                {
                    "type": "m.room.join_rules",
                    "state_key": "",
                    "content": {
                        "join_rule": "restricted",
                        "allow": [{"type": "m.room_membership", "room_id": restricted}],
                    },
                }
            ]

        resp = await self.az.intent.api.request(Method.POST, Path.v3.createRoom, req)

        return resp["room_id"]

    @abstractmethod
    def register_room(self, room: "Room"):
        pass

    @abstractmethod
    def find_rooms(self, rtype=None, user_id: "UserID" = None) -> list["Room"]:
        pass
