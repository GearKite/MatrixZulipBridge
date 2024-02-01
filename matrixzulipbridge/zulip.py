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
from typing import TYPE_CHECKING, Optional
from urllib.parse import urljoin

import emoji
from bs4 import BeautifulSoup
from markdownify import markdownify

from matrixzulipbridge.direct_room import DirectRoom
from matrixzulipbridge.stream_room import StreamRoom

if TYPE_CHECKING:
    from matrixzulipbridge.organization_room import OrganizationRoom


class ZulipEventHandler:
    def __init__(self, organization: "OrganizationRoom") -> None:
        self.organization = organization
        self.messages = set()

    def on_event(self, event: dict):
        match event["type"]:
            case "message":
                self._handle_message(event["message"])
            case "subscription":
                self._handle_subscription(event)
            case "reaction":
                self._handle_reaction(event)
            case "delete_message":
                self._handle_delete_message(event)
            case "realm_user":
                self._handle_realm_user(event)
            case _:
                logging.debug(f"Unhandled event type: {event['type']}")

    def backfill_message(self, message: dict):
        self._handle_message(message)

    def _handle_message(self, event: dict):
        if event["type"] != "stream":
            return
        if event["sender_id"] == self.organization.profile["user_id"]:
            return  # Ignore own messages
        if event["id"] in self.organization.messages:
            # Skip already forwarded messages
            return
        # Prevent race condition when single message is received by multiple clients
        if event["id"] in self.messages:
            return
        self.messages.add(event["id"])

        room = self._get_room_by_stream_id(event["stream_id"])

        if not room:
            logging.debug(
                f"Received message from stream with no associated Matrix room: {event}"
            )
            return

        topic = event["subject"]

        mx_user_id = room.serv.get_mxid_from_zulip_user_id(
            self.organization, event["sender_id"]
        )

        message, formatted_message = self._process_message_content(event["content"])

        custom_data = {
            "zulip_topic": topic,
            "zulip_user_id": event["sender_id"],
            "display_name": event["sender_full_name"],
            "zulip_message_id": event["id"],
            "type": "message",
            "timestamp": event["timestamp"],
            "target": "stream",
        }

        room.send_message(
            message,
            formatted=formatted_message,
            user_id=mx_user_id,
            custom_data=custom_data,
        )

    async def handle_dm_message(self, event: dict):
        if event["sender_id"] == self.organization.profile["user_id"]:
            return  # Ignore own messages
        if event["id"] in self.organization.messages:
            # Skip already forwarded messages
            return
        # Prevent race condition when single message is received by multiple clients
        if event["id"] in self.messages:
            return
        mx_user_id = self.organization.serv.get_mxid_from_zulip_user_id(
            self.organization, event["sender_id"]
        )
        recipient_ids = frozenset(user["id"] for user in event["display_recipient"])
        room = self.organization.direct_rooms.get(recipient_ids)
        if not room:
            room = await DirectRoom.create(
                self.organization, event["display_recipient"]
            )

        message, formatted_message = self._process_message_content(event["content"])

        custom_data = {
            "zulip_user_id": event["sender_id"],
            "display_name": event["sender_full_name"],
            "zulip_message_id": event["id"],
            "type": "message",
            "timestamp": event["timestamp"],
            "target": "direct",
        }

        room.send_message(
            message,
            formatted=formatted_message,
            user_id=mx_user_id,
            custom_data=custom_data,
        )

    def _handle_reaction(self, event: dict):
        zulip_user_id = event["user_id"]

        message_mxid = self.organization.messages.get(event["message_id"])
        if message_mxid is None:
            logging.warning(
                f"Could not find message with Zulip ID: {event['message_id']}, it probably wasn't sent to Matrix."
            )
            return
        # TODO: Implement

    def _handle_delete_message(self, event: dict):
        message_mxid = self._get_mxid_from_zulip_id(event["message_id"])
        if not message_mxid:
            return
        room = self._get_room_by_stream_id(event["stream_id"])
        room.redact(message_mxid, reason="Deleted on Zulip")
        del self.organization.messages[str(event["message_id"])]

    def _handle_subscription(self, event: dict):
        if not "stream_ids" in event:
            return
        for stream_id in event["stream_ids"]:
            room = self._get_room_by_stream_id(stream_id)

            if not room:
                logging.debug(
                    f"Received message from stream with no associated Matrix room: {event}"
                )
                return

            match event["op"]:
                case "peer_add":
                    for user_id in event["user_ids"]:
                        room.on_join(user_id)
                case "peer_remove":
                    for user_id in event["user_ids"]:
                        room.on_part(user_id)

    def _handle_realm_user(self, event: dict):
        # Update Zulip user cache
        if event["op"] == "update":
            user_id = event["person"]["user_id"]
            if not user_id in self.organization.zulip_users:
                return
            self.organization.zulip_users[user_id] |= event["person"]

    def _get_mxid_from_zulip_id(self, zulip_id: int | str):
        try:
            return self.organization.messages[str(zulip_id)]
        except KeyError:
            logging.debug(
                f"Message with Zulip ID {zulip_id} not found, it probably wasn't sent to Matrix"
            )

    def _get_room_by_stream_id(self, stream_id: int) -> Optional["StreamRoom"]:
        for room in self.organization.rooms.values():
            if not isinstance(room, StreamRoom):
                continue
            if room.stream_id == stream_id:
                return room
        return None

    def _process_message_content(self, html: str):
        # Replace Zulip file upload relative URLs with absolute
        soup = BeautifulSoup(html, "html.parser")
        for a_tag in soup.find_all("a"):
            href = a_tag.get("href")
            absolute_url = urljoin(self.organization.server["realm_uri"], href)
            a_tag["href"] = absolute_url

        formatted_message = emoji.emojize(soup.decode(), language="alias")
        message = markdownify(formatted_message).rstrip()
        return message, formatted_message
