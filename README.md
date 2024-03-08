# MatrixZulipBridge

![Tests](https://github.com/GearKite/MatrixZulipBridge/actions/workflows/tests.yml/badge.svg)
![PyPI version](https://img.shields.io/pypi/v/matrixzulipbridge.svg)
![Python versions](https://img.shields.io/pypi/pyversions/matrixzulipbridge.svg)

A Matrix puppeting appservice bridge for Zulip

Join the Matrix room [#MatrixZulipBridge:shema.lv](https://matrix.to/#/#matrixzulipbridge:shema.lv) if you have any questions or just want to chat.

## Features

- [x] Streams
- [x] Zulip topics - Matrix threads
- [x] Direct messages
- [x] Formatted text\* (not all of Zulip's formatting)
- [x] Matrix puppets
- [x] Zulip puppets
- [x] Message Media\* (only translating URIs)
- [ ] Presence
- [x] Reactions
- [x] Redactions\* (only from Matrix in DMs)
- [x] Replies
- [ ] Typing indicators

## Installation

### Prerequisites

- [Python >=3.10](https://wiki.python.org/moin/BeginnersGuide/Download)
- Matrix homeserver with ability to add appservices

### PyPI

`pip install matrixzulipbridge`

### Docker

`docker run ghcr.io/gearkite/matrixzulipbridge:{tag} ...`  
See [packages](https://github.com/GearKite/MatrixZulipBridge/pkgs/container/matrixzulipbridge) for possible tags

### From source

1. Clone or download this git repository  
   `git clone https://github.com/GearKite/MatrixZulipBridge.git`
2. Install [Poetry](https://python-poetry.org/docs/#installation)
3. Install dependencies  
   `poetry install`
4. Enter the virtual environment  
   `poetry shell`

## Running

### Example

1. Generate a registration file  
   `python3 -m matrixzulipbridge --config config.yaml --generate`
2. Install the appservice on your homeserver
3. Run the bridge  
   `python3 -m matrixzulipbridge --config config.yaml https://homeserver.example.com`

### Usage

```shell
usage: python3 -m matrixzulipbridge [-h] [-v] (-c CONFIG | --version) [-l LISTEN_ADDRESS] [-p LISTEN_PORT] [-u UID] [-g GID] [--generate] [--generate-compat] [--reset] [--unsafe-mode] [-o OWNER] [homeserver]

A puppeting Matrix - Zulip appservice bridge (v0.1.1.dev2+g3ed8ed7)

positional arguments:
  homeserver            URL of Matrix homeserver (default: http://localhost:8008)

options:
  -h, --help            show this help message and exit
  -v, --verbose         log debug messages
  -c CONFIG, --config CONFIG
                        registration YAML file path, must be writable if generating (default: None)
  --version             show bridge version
  -l LISTEN_ADDRESS, --listen-address LISTEN_ADDRESS
                        bridge listen address (default: as specified in url in config, 127.0.0.1 otherwise) (default: None)
  -p LISTEN_PORT, --listen-port LISTEN_PORT
                        bridge listen port (default: as specified in url in config, 28464 otherwise) (default: None)
  -u UID, --uid UID     user id to run as (default: None)
  -g GID, --gid GID     group id to run as (default: None)
  --generate            generate registration YAML for Matrix homeserver (Synapse)
  --generate-compat     generate registration YAML for Matrix homeserver (Dendrite and Conduit)
  --reset               reset ALL bridge configuration from homeserver and exit
  --unsafe-mode         allow appservice to leave rooms on error (default: False)
  -o OWNER, --owner OWNER
                        set owner MXID (eg: @user:homeserver) or first talking local user will claim the bridge (default: None)
```

After registering and launching the bridge, start a chat. You can find the localpart in your `registration.yaml`  
This bridge is mainly configurable through Matrix, send `help` to get a list of commands

### Bridging a stream

In your control room chat with the bridge send:

1.  `addorganization {name}`
2.  `open {name}`

In the organization room send:

1. `site example.com`
2. `email my-bot@example.com`
3. `apikey xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
4. `connect`
5. `subscribe {zulip stream name}`

## Credits

This bridge is heavily based on [Heisenbridge](https://github.com/hifi/heisenbridge). Thank you, Heisenbridge contributors!
