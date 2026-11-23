"""The `homeproxy` command.

Subcommands:

- `homeproxy serve`: run the dashboard API over HTTPS. The s6 `api` service runs this.
- `homeproxy set-password`: set a new dashboard password. It asks twice on a terminal, or reads
  one line from stdin with `--stdin`. Every dashboard session is signed out.

Every subcommand that touches `/data` runs as the `homeproxy` user. Started as root, for example
through `docker exec`, it switches to that user first, so files it creates keep the right owner.
It refuses to run as any other user, or when that user does not exist.
"""

import argparse
import getpass
import os
import pwd
import sys
from collections.abc import Sequence
from typing import TextIO

from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from homeproxy.auth.passwords import PasswordRejected, check_new_password, hash_password
from homeproxy.auth.runtime import data_root
from homeproxy.store.errors import StoreError
from homeproxy.store.files import Store
from homeproxy.store.pending import CONFIG_FILE, Change, PendingChanges
from homeproxy.store.tree import load_tree

SERVICE_USER = "homeproxy"


class CliError(Exception):
    """A failure to report on stderr with exit code 1."""


def drop_privileges() -> None:
    """Run as the service user: switch to it from root, and refuse any other user.

    Files written under `/data` by another uid would lock the dashboard out. Raises `CliError`.
    """
    name = SERVICE_USER
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        raise CliError(f"Refusing to run: user {name!r} does not exist.") from None
    if entry.pw_uid == 0:
        raise CliError(f"Refusing to run: user {name!r} is root.")
    euid = os.geteuid()
    if euid == entry.pw_uid:
        return
    if euid != 0:
        raise CliError(
            f"Run this as root or as the {name!r} user, so files under /data keep their owner."
        )
    os.setgroups([])
    os.setgid(entry.pw_gid)
    os.setuid(entry.pw_uid)
    os.environ["HOME"] = entry.pw_dir
    os.environ["USER"] = entry.pw_name
    if os.geteuid() == 0 or os.getuid() == 0:
        raise CliError("Could not drop root privileges.")


def read_new_password(use_stdin: bool, stdin: TextIO | None = None) -> str:
    """Read the new password: one line from stdin, or twice from the terminal."""
    if use_stdin:
        stream = stdin or sys.stdin
        line = stream.readline()
        return line.removesuffix("\n").removesuffix("\r")
    first = getpass.getpass("New dashboard password: ")
    second = getpass.getpass("Repeat the password: ")
    if first != second:
        raise CliError("The passwords do not match.")
    return first


def set_password(store: Store, password: str) -> None:
    """Hash `password` and write it to `dashboard.password_hash` in `config.yaml`.

    Comments and the rest of the file stay as they are. The running API reads the new hash on its
    next request, and every older session stops working. A staged `config.yaml` gets the new hash
    too and keeps its edits: the admin may be locked out with edits waiting to be applied.
    """
    try:
        check_new_password(password)
    except PasswordRejected as error:
        raise CliError(str(error)) from None
    if not store.config_path.exists():
        raise CliError(
            "HomeProxy is not set up yet, so there is no password to change. "
            "Finish the setup wizard in the dashboard first."
        )
    password_hash = hash_password(password)
    with store.lock() as held:
        pending = PendingChanges(store)
        # Read first: it finishes a promote cut short, which writes config.yaml.
        status = pending.status(held)
        tree = load_tree(store.config_path)
        dashboard = tree.get("dashboard") if isinstance(tree, CommentedMap) else None
        if not isinstance(dashboard, CommentedMap):
            raise CliError("config.yaml has no dashboard section. Fix the file first.")
        dashboard["password_hash"] = DoubleQuotedScalarString(password_hash)
        staged = None
        # A conflict stays one, so a hand edit is never undone by the promote.
        if (
            Change(CONFIG_FILE, "write") in status.changes
            and CONFIG_FILE not in status.conflicting_paths
        ):
            staged = pending.read_tree(CONFIG_FILE, held)
            staged["dashboard"]["password_hash"] = DoubleQuotedScalarString(password_hash)
        store.save_config(tree, held)
        if staged is not None:
            # Dropping the change and staging it again makes the new file its base.
            pending.stage(CONFIG_FILE, store.config_path.read_bytes(), held)
            pending.stage_tree(CONFIG_FILE, staged, held)


def _cmd_serve(args: argparse.Namespace) -> int:
    from homeproxy.auth.server import ServerConfigError, serve

    drop_privileges()
    try:
        serve()
    except ServerConfigError as error:
        raise CliError(str(error)) from None
    return 0


def _cmd_set_password(args: argparse.Namespace) -> int:
    drop_privileges()
    password = read_new_password(args.stdin)
    try:
        set_password(Store(data_root()), password)
    except StoreError as error:
        # Store errors name the file and the field, and never quote file content.
        raise CliError(f"Could not save the password: {error}") from None
    print("Password changed. Every dashboard session is signed out.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser."""
    parser = argparse.ArgumentParser(prog="homeproxy", description="HomeProxy management.")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the dashboard API over HTTPS.")
    serve.set_defaults(handler=_cmd_serve)
    password = commands.add_parser(
        "set-password",
        help="Set a new dashboard password and sign out every session.",
    )
    password.add_argument(
        "--stdin",
        action="store_true",
        help="Read the password from the first line of stdin instead of asking twice.",
    )
    password.set_defaults(handler=_cmd_set_password)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit code."""
    args = build_parser().parse_args(argv)
    try:
        result: int = args.handler(args)
    except CliError as error:
        print(f"homeproxy: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return result


if __name__ == "__main__":
    sys.exit(main())
