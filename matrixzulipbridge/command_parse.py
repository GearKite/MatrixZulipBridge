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
import shlex
from typing import Awaitable


class CommandParserFormatter(
    argparse.ArgumentDefaultsHelpFormatter, argparse.RawTextHelpFormatter
):
    pass


class CommandParserError(Exception):
    pass


class CommandParser(argparse.ArgumentParser):
    def __init__(self, *args, formatter_class=CommandParserFormatter, **kwargs):
        super().__init__(*args, formatter_class=formatter_class, **kwargs)

    @property
    def short_description(self):
        return self.description.split("\n")[0]

    def error(self, message):
        raise CommandParserError(message)

    def print_usage(self, *args, **kwargs):
        raise CommandParserError(self.format_usage())

    def print_help(self, *args, **kwargs):
        raise CommandParserError(self.format_help())

    def exit(self, status=0, message=None):
        pass


def split(text):
    commands = []

    sh_split = shlex.shlex(text, posix=True, punctuation_chars=";")
    sh_split.commenters = ""
    sh_split.wordchars += "!#$%&()*+,-./:<=>?@[\\]^_`{|}~"

    args = []
    for v in list(sh_split):
        if v == ";":
            commands.append(args)
            args = []
        else:
            args.append(v)

    if len(args) > 0:
        commands.append(args)

    return commands


class CommandManager:
    _commands: dict[str, tuple[CommandParser, Awaitable]]

    def __init__(self):
        self._commands = {}

    def register(self, cmd: CommandParser, func, aliases=None):
        self._commands[cmd.prog] = (cmd, func)

        if aliases is not None:
            for alias in aliases:
                self._commands[alias] = (cmd, func)

    async def trigger_args(self, args, tail=None, allowed=None, forward=None):
        command = args.pop(0).upper()

        if allowed is not None and command not in allowed:
            raise CommandParserError(f"Illegal command supplied: '{command}'")

        if command in self._commands:
            (cmd, func) = self._commands[command]
            cmd_args = cmd.parse_args(args)
            cmd_args._tail = tail
            cmd_args._forward = forward
            await func(cmd_args)
        elif command == "HELP":
            out = ["Following commands are supported:", ""]
            for name, (cmd, func) in self._commands.items():
                if cmd.prog == name:
                    out.append("\t{} - {}".format(cmd.prog, cmd.short_description))

            out.append("")
            out.append("To get more help, add -h to any command without arguments.")

            raise CommandParserError("\n".join(out))
        else:
            raise CommandParserError(
                'Unknown command "{}", type HELP for list'.format(command)
            )

    async def trigger(self, text, tail=None, allowed=None, forward=None):
        for args in split(text):
            await self.trigger_args(args, tail, allowed, forward)
            tail = None
