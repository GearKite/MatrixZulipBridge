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
import json
import logging

import aiohttp
from mautrix.types.event import Event


class AppserviceWebsocket:
    def __init__(self, url, token, callback):
        self.url = url + "/_matrix/client/unstable/fi.mau.as_sync"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "X-Mautrix-Websocket-Version": "3",
        }
        self.callback = callback

    async def start(self):
        asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            try:
                logging.info(f"Connecting to {self.url}...")

                async with aiohttp.ClientSession(headers=self.headers) as sess:
                    async with sess.ws_connect(self.url) as ws:
                        logging.info("Websocket connected.")

                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                logging.debug("Unhandled WS message: %s", msg)
                                continue

                            data = msg.json()
                            if (
                                data["status"] == "ok"
                                and data["command"] == "transaction"
                            ):
                                logging.debug(f"Websocket transaction {data['txn_id']}")
                                for event in data["events"]:
                                    try:
                                        await self.callback(Event.deserialize(event))
                                    except Exception as e:
                                        logging.error(e)

                                await ws.send_str(
                                    json.dumps(
                                        {
                                            "command": "response",
                                            "id": data["id"],
                                            "data": {},
                                        }
                                    )
                                )
                            else:
                                logging.warn("Unhandled WS command: %s", data)

                logging.info("Websocket disconnected.")
            except asyncio.CancelledError:
                logging.info("Websocket was cancelled.")
                return
            except Exception as e:
                logging.error(e)

                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    return
