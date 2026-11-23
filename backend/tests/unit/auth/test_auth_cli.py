"""Tests of the `homeproxy` command line: set-password and the root check."""

import getpass
import io
import os
import pwd
from pathlib import Path

import pytest

from homeproxy import cli
from homeproxy.auth.dashboard import read_dashboard_settings
from homeproxy.auth.passwords import hash_password, verify_password
from homeproxy.store import pending as pending_module
from homeproxy.store.files import Store
from homeproxy.store.pending import PendingChanges

OLD = "the old password"
NEW = "the new password"


def write_config(root: Path) -> None:
    """Write a valid config.yaml with comments around the hash."""
    (root / "config").mkdir(parents=True)
    (root / "config" / "config.yaml").write_text(
        "# top comment\n"
        "version: 1\n"
        "dashboard:\n"
        "  listen: 192.168.1.10:8443\n"
        "  # the hash\n"
        f'  password_hash: "{hash_password(OLD)}"\n'
        "  session_ttl: 12h  # keep\n"
        "network:\n"
        "  lan_cidrs: [192.168.1.0/24]\n"
        "  haproxy_lan_ip: 192.168.1.10\n"
        "dns_server:\n"
        "  listen: 192.168.1.10:5335\n"
    )


def test_set_password_replaces_only_the_hash(tmp_path: Path) -> None:
    """The new hash verifies, the old password stops working, comments stay."""
    write_config(tmp_path)
    before = (tmp_path / "config" / "config.yaml").read_text()
    cli.set_password(Store(tmp_path), NEW)
    after = (tmp_path / "config" / "config.yaml").read_text()
    settings = read_dashboard_settings(tmp_path / "config" / "config.yaml")
    assert verify_password(settings.password_hash, NEW)
    assert not verify_password(settings.password_hash, OLD)
    assert NEW not in after
    for line in ("# top comment", "  # the hash", "  session_ttl: 12h  # keep"):
        assert line in after
    changed = [
        (old, new)
        for old, new in zip(before.splitlines(), after.splitlines(), strict=True)
        if old != new
    ]
    assert len(changed) == 1
    assert changed[0][1].startswith('  password_hash: "$argon2id$')


def stage_ttl(store: Store, ttl: str) -> PendingChanges:
    """Stage a `session_ttl` edit of config.yaml, as the dashboard settings do."""
    pending = PendingChanges(store)
    with store.lock() as held:
        staged = store.config_path.read_text().replace("session_ttl: 12h", f"session_ttl: {ttl}")
        pending.stage("config.yaml", staged.encode(), held)
    return pending


def test_set_password_keeps_a_staged_config_edit(tmp_path: Path) -> None:
    """A locked-out admin's staged edits stay, and applying them keeps the new password."""
    write_config(tmp_path)
    store = Store(tmp_path)
    pending = stage_ttl(store, "24h")

    cli.set_password(store, NEW)

    with store.lock() as held:
        assert not pending.status(held).conflict
        pending.promote(held)
    settings = read_dashboard_settings(store.config_path)
    assert verify_password(settings.password_hash, NEW)
    assert "session_ttl: 24h  # keep" in store.config_path.read_text()


def test_set_password_after_a_promote_cut_short_keeps_the_promoted_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store finishes the promote under the lock first, so the password goes on top of it."""
    write_config(tmp_path)
    store = Store(tmp_path)
    pending = stage_ttl(store, "24h")

    def full_disk(path: Path, data: bytes, *, mode: int | None = None) -> None:
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch, store.lock() as held:
        patch.setattr(pending_module, "atomic_write", full_disk)
        with pytest.raises(OSError, match="No space"):
            pending.promote(held)

    cli.set_password(store, NEW)

    assert "session_ttl: 24h  # keep" in store.config_path.read_text()
    assert verify_password(read_dashboard_settings(store.config_path).password_hash, NEW)


def test_set_password_leaves_a_conflict_with_a_hand_edit(tmp_path: Path) -> None:
    """A hand edit already made after staging must still stop the promote that would undo it."""
    write_config(tmp_path)
    store = Store(tmp_path)
    pending = stage_ttl(store, "24h")
    store.config_path.write_text(store.config_path.read_text().replace("# top", "# hand"))

    cli.set_password(store, NEW)

    with store.lock() as held:
        assert pending.status(held).conflicting_paths == ("config.yaml",)


def test_set_password_before_setup_refuses(tmp_path: Path) -> None:
    """There is no password to reset before setup, and no config.yaml is made."""
    with pytest.raises(cli.CliError, match="not set up"):
        cli.set_password(Store(tmp_path), NEW)
    assert not (tmp_path / "config").exists()


def test_short_password_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    """The rules apply to the CLI too."""
    write_config(tmp_path)
    before = (tmp_path / "config" / "config.yaml").read_bytes()
    with pytest.raises(cli.CliError, match="at least"):
        cli.set_password(Store(tmp_path), "short")
    assert (tmp_path / "config" / "config.yaml").read_bytes() == before


def run_as_me(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the test's own user the service user, so the CLI accepts it."""
    monkeypatch.setattr(cli, "SERVICE_USER", pwd.getpwuid(os.geteuid()).pw_name)


def test_main_reads_stdin_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`homeproxy set-password --stdin` sets the password from one line and exits 0."""
    if os.geteuid() == 0:
        pytest.skip("the test would drop privileges")
    run_as_me(monkeypatch)
    write_config(tmp_path)
    monkeypatch.setenv("HOMEPROXY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO(NEW + "\n"))
    assert cli.main(["set-password", "--stdin"]) == 0
    output = capsys.readouterr()
    assert "Password changed" in output.out
    assert NEW not in output.out + output.err
    settings = read_dashboard_settings(tmp_path / "config" / "config.yaml")
    assert verify_password(settings.password_hash, NEW)


def test_main_reports_errors_with_exit_code_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Failures print one line on stderr and exit 1."""
    if os.geteuid() == 0:
        pytest.skip("the test would drop privileges")
    run_as_me(monkeypatch)
    monkeypatch.setenv("HOMEPROXY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO(NEW + "\n"))
    assert cli.main(["set-password", "--stdin"]) == 1
    assert "not set up" in capsys.readouterr().err


def test_terminal_prompt_must_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asking twice catches a typo."""
    answers = iter(["one password!", "two password!"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt: next(answers))
    with pytest.raises(cli.CliError, match="do not match"):
        cli.read_new_password(use_stdin=False)


def test_stdin_line_end_is_stripped() -> None:
    """Only the line end is removed; other spaces are part of the password."""
    assert cli.read_new_password(True, io.StringIO(" spaced pass \r\n")) == " spaced pass "


class FakeIds:
    """Records privilege changes instead of making them."""

    def __init__(self, euid: int) -> None:
        self.euid = euid
        self.calls: list[tuple[str, object]] = []

    def setgroups(self, groups: list[int]) -> None:
        self.calls.append(("setgroups", groups))

    def setgid(self, gid: int) -> None:
        self.calls.append(("setgid", gid))

    def setuid(self, uid: int) -> None:
        self.calls.append(("setuid", uid))
        self.euid = uid


def _fake_root(monkeypatch: pytest.MonkeyPatch, ids: FakeIds) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: ids.euid)
    monkeypatch.setattr(os, "getuid", lambda: ids.euid)
    monkeypatch.setattr(os, "setgroups", ids.setgroups)
    monkeypatch.setattr(os, "setgid", ids.setgid)
    monkeypatch.setattr(os, "setuid", ids.setuid)


def test_root_drops_to_the_service_user_groups_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """As root, groups, then gid, then uid change to the homeproxy user."""
    ids = FakeIds(euid=0)
    _fake_root(monkeypatch, ids)
    entry = pwd.struct_passwd(("homeproxy", "x", 10001, 10001, "", "/nonexistent", "/bin/false"))
    monkeypatch.setattr(pwd, "getpwnam", lambda name: entry)
    monkeypatch.setenv("HOME", "/root")
    cli.drop_privileges()
    assert ids.calls == [("setgroups", []), ("setgid", 10001), ("setuid", 10001)]
    assert os.environ["HOME"] == "/nonexistent"


def test_root_without_the_service_user_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the user, the CLI refuses rather than writing files as root."""
    ids = FakeIds(euid=0)
    _fake_root(monkeypatch, ids)

    def missing(name: str) -> pwd.struct_passwd:
        raise KeyError(name)

    monkeypatch.setattr(pwd, "getpwnam", missing)
    with pytest.raises(cli.CliError, match="does not exist"):
        cli.drop_privileges()
    assert ids.calls == []


def test_failed_drop_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If root is still root after setuid, the command stops."""
    ids = FakeIds(euid=0)
    _fake_root(monkeypatch, ids)
    monkeypatch.setattr(os, "setuid", lambda uid: None)
    entry = pwd.struct_passwd(("homeproxy", "x", 10001, 10001, "", "/nonexistent", "/bin/false"))
    monkeypatch.setattr(pwd, "getpwnam", lambda name: entry)
    with pytest.raises(cli.CliError, match="Could not drop"):
        cli.drop_privileges()


SERVICE_ENTRY = pwd.struct_passwd(
    ("homeproxy", "x", 10001, 10001, "", "/nonexistent", "/bin/false")
)


def test_the_service_user_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Already the homeproxy user: nothing changes."""
    ids = FakeIds(euid=10001)
    _fake_root(monkeypatch, ids)
    monkeypatch.setattr(pwd, "getpwnam", lambda name: SERVICE_ENTRY)
    cli.drop_privileges()
    assert ids.calls == []


def test_another_user_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any other non-root user would write files under /data with the wrong owner."""
    ids = FakeIds(euid=1000)
    _fake_root(monkeypatch, ids)
    monkeypatch.setattr(pwd, "getpwnam", lambda name: SERVICE_ENTRY)
    with pytest.raises(cli.CliError, match="as root or as the 'homeproxy' user"):
        cli.drop_privileges()
    assert ids.calls == []
