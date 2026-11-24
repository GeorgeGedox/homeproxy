"""The config migration at the dashboard's start, in the built image.

Every format is at version 1, so no migration step exists yet. A file from a newer release is
what reaches the start-up migration today, as after a rollback to an older image.
"""

from tests.integration.haproxy_live import docker
from tests.integration.image_server import finish_setup, start_server

CONFIG = "/data/config/config.yaml"


def test_a_config_from_a_newer_release_is_kept_and_the_dashboard_still_starts(
    test_name_prefix: str,
) -> None:
    """The start names both versions, leaves the file as it is, and the dashboard answers."""
    with start_server(f"{test_name_prefix}-migrate") as server:
        finish_setup(server)
        bump = ["sed", "-i", "s/^version: 1$/version: 2/", CONFIG]
        docker("exec", "--user", "homeproxy", server.name, *bump)

        count = server.logs().count("dashboard listening on") + 1
        docker("exec", server.name, "/command/s6-svc", "-r", "/run/service/api")
        server.wait_for_start(count)

        assert "'config.yaml' has version 2, and this release supports 1" in server.logs()
        assert "version: 2" in docker("exec", server.name, "cat", CONFIG)
