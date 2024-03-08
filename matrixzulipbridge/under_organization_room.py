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
from typing import TYPE_CHECKING, Optional
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify
from mautrix.errors import MatrixStandardRequestError
from mautrix.types.event.state import (
    JoinRestriction,
    JoinRestrictionType,
    JoinRule,
    JoinRulesStateEventContent,
)
from mautrix.types.event.type import EventType

from matrixzulipbridge.room import InvalidConfigError, Room

if TYPE_CHECKING:
    from mautrix.types import MessageEvent, RoomID, UserID

    from matrixzulipbridge.organization_room import OrganizationRoom


def connected(f):
    def wrapper(*args, **kwargs):
        self = args[0]

        if not self.organization.zulip or not self.organization.zulip.has_connected:
            self.send_notice("Need to be connected to use this command.")
            return asyncio.sleep(0)

        return f(*args, **kwargs)

    return wrapper


class UnderOrganizationRoom(Room):
    """Base class for all rooms under an organization"""

    organization: Optional["OrganizationRoom"]
    organization_id: "RoomID"
    force_forward: bool

    def init(self) -> None:
        self.organization = None
        self.organization_id = None
        self.force_forward = True

    def from_config(self, config: dict) -> None:
        super().from_config(config)

        self.organization_id = config["organization_id"]

        if not self.organization_id:
            raise InvalidConfigError("No organization_id in config for room")

    def to_config(self) -> dict:
        return {
            **(super().to_config()),
            "organization_id": self.organization_id,
        }

    def is_valid(self) -> bool:
        if self.organization_id is None:
            return False

        return True

    async def join_existing_room(self, room_id: "RoomID"):
        self.id = await self.organization.az.intent.join_room(room_id)

        if self.id is None:
            self.organization.send_notice(f"Could not join room {room_id}")
            return

        self.serv.register_room(self)
        await self.save()
        # start event queue now that we have an id
        self._queue.start()

        # attach to organization space
        if self.organization.space:
            await self.organization.space.attach(self.id)

    async def _process_event_content(
        self,
        event: "MessageEvent",
        prefix: str = "",
        reply_to=None,
        topic: str = None,
    ):
        content = event.content

        if content.msgtype.is_media:
            media_url = self.serv.mxc_to_url(
                mxc=event.content.url, filename=event.content.body
            )
            message = f"[{content.body}]({media_url})"
        elif content.formatted_body:
            message = content.formatted_body

            # Replace all puppet mentions with Zulip mentions
            soup = BeautifulSoup(content.formatted_body, features="html.parser")
            for link in soup.find_all("a"):
                href: str = link.get("href", "")
                if not href.startswith("https://matrix.to/#/"):
                    continue
                mxid = href.split("https://matrix.to/#/")[1]
                # Translate puppet mentions as native Zulip mentions
                if not self.serv.is_puppet(mxid):
                    continue

                user_id = self.organization.get_zulip_user_id_from_mxid(mxid)
                zulip_user = self.organization.get_zulip_user(user_id)

                zulip_mention = soup.new_tag("span")
                zulip_mention.string = " @"
                zulip_mention_content = soup.new_tag("strong")
                zulip_mention_content.string = f"{zulip_user['full_name']}|{user_id}"
                zulip_mention.append(zulip_mention_content)

                link.replace_with(zulip_mention)

            if reply_to is not None:
                # Attempt to parse reply, it's alright if this fails
                try:
                    reply_block = soup.find("mx-reply")
                    if reply_block is not None:
                        links = reply_block.find_all("a")
                        if type(self).__name__ in (
                            "DirectRoom",
                            "StreamRoom",
                        ):
                            # Replace reply event link with Zulip link
                            in_reply_to_link = links[0]
                            narrow = self._construct_zulip_narrow_url(
                                topic=topic,
                                message_id=self.messages.inv.get(reply_to.event_id),
                            )
                            in_reply_to_link["href"] = narrow

                        # Replace mxid with display name (non-puppet users)
                        if len(links) > 1:
                            author_link = links[1]
                            author_mxid = author_link["href"].split(
                                "https://matrix.to/#/"
                            )[1]
                            author_link.string.replace_with(
                                self._get_displayname(author_mxid)
                            )
                except Exception:  # pylint: disable=broad-exception-caught
                    pass

            message = soup.encode(formatter="html5")

            message = markdownify(message)
        elif content.body:
            message = content.body
        else:
            logging.warning("_process_event_content called with no usable body")
            return
        message = prefix + message
        return message

    def _get_displayname(self, mxid: "UserID"):
        if mxid in self.displaynames:
            sender_displayname = self.displaynames[mxid][:100]
            return sender_displayname
        # Fallback to mxid
        return mxid

    def _construct_zulip_narrow_url(self, topic=None, message_id=None):
        zulip_uri = urlparse(self.organization.zulip.base_url)
        base_url = zulip_uri.scheme + "://" + zulip_uri.netloc

        narrow = base_url + "/#narrow"

        if type(self).__name__ == "DirectRoom":
            recipients_string = ""
            for recipient in set(self.recipient_ids):
                recipients_string += str(recipient) + ","
            recipients_string = recipients_string[:-1]
            narrow += f"/dm/{recipients_string}"
        elif type(self).__name__ == "StreamRoom":
            narrow += f"/stream/{self.stream_id}"

        if topic is not None:
            narrow += f"/topic/{quote(topic, safe='')}"
        if message_id is not None:
            narrow += f"/near/{message_id}"

        return narrow

    async def _attach_space_internal(self) -> None:
        await self.az.intent.send_state_event(
            self.id,
            EventType.ROOM_JOIN_RULES,  # Why does this happend? pylint: disable=no-member
            content=JoinRulesStateEventContent(
                join_rule=JoinRule.RESTRICTED,
                allow=[
                    JoinRestriction(
                        type=JoinRestrictionType.ROOM_MEMBERSHIP,
                        room_id=self.organization.space.id,
                    ),
                ],
            ),
        )

    async def _attach_space(self) -> None:
        logging.debug(
            f"Attaching room {self.id} to organization space {self.organization.space.id}."
        )
        try:
            room_create = await self.az.intent.get_state_event(
                self.id, EventType.ROOM_CREATE  # pylint: disable=no-member
            )  # pylint: disable=no-member
            if room_create.room_version in [str(v) for v in range(1, 9)]:
                self.send_notice(
                    "Only rooms of version 9 or greater can be attached to a space."
                )
                self.send_notice(
                    "Leave and re-create the room to ensure the correct version."
                )
                return

            await self._attach_space_internal()
            self.send_notice("Attached to space.")
        except MatrixStandardRequestError as e:
            logging.debug("Setting join_rules for space failed.", exc_info=True)
            self.send_notice(f"Failed attaching space: {e.message}")
            self.send_notice("Make sure the room is at least version 9.")
        except Exception:
            logging.exception(
                f"Failed to attach {self.id} to space {self.organization.space.id}."
            )
