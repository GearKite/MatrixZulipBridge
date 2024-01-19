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
import os
import shutil
import subprocess

module_dir = os.path.dirname(__file__)
root_dir = module_dir + "/../"

__version__ = "0.0.0"
__git_version__ = None

if os.path.exists(module_dir + "/version.txt"):
    __version__ = open(module_dir + "/version.txt", encoding="utf-8").read().strip()

if os.path.exists(root_dir + ".git") and shutil.which("git"):
    try:
        git_env = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ["HOME"],
            "LANG": "C",
            "LC_ALL": "C",
        }
        git_bits = (
            subprocess.check_output(
                ["git", "describe", "--tags"],
                stderr=subprocess.DEVNULL,
                cwd=root_dir,
                env=git_env,
            )
            .strip()
            .decode("ascii")
            .split("-")
        )

        __git_version__ = git_bits[0][1:]

        if len(git_bits) > 1:
            __git_version__ += f".dev{git_bits[1]}"

        if len(git_bits) > 2:
            __git_version__ += f"+{git_bits[2]}"

        # always override version with git version if we have a valid version number
        __version__ = __git_version__
    except (subprocess.SubprocessError, OSError):
        pass
