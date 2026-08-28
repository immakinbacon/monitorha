"""Two hosts in one Proxmox cluster must be published once, not twice.

`/cluster/resources` and `/cluster/status` answer identically from every node,
so without an election each configured host reports the whole cluster and every
node, guest, storage and the quorum sensor appears twice in Home Assistant.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from monitorha.app.api import build_client
from monitorha.app.manager import Manager, SourceRunner
from monitorha.app.store import Store

from .test_mikrotik import ROUTES as MIKROTIK_ROUTES
from .test_proxmox import ROUTES as PROXMOX_ROUTES

PROXMOX_SOURCE = {
    "type": "proxmox",
    # Deduplication only applies to hosts that report a whole cluster. In the
    # default node scope each host reports only itself, so there is nothing to
    # deduplicate and both must publish.
    "scope": "cluster",
    "token_id": "monitoring@pve!ha",
    "token_secret": "x",
}
MIKROTIK_SOURCE = {"type": "mikrotik", "username": "monitor", "password": "secret"}


def standalone_routes() -> dict[tuple[str, str], Any]:
    """A Proxmox host that is not in a cluster: no `type: cluster` entry."""
    routes = copy.deepcopy(PROXMOX_ROUTES)
    routes[("GET", "/api2/json/cluster/status")] = {
        "data": [{"type": "node", "name": "pve1", "online": 1, "local": 1, "nodeid": 1}]
    }
    return routes


async def add_runner(
    store: Store, manager: Manager, make_session, spec: dict[str, Any], routes
) -> SourceRunner:
    source = store.add(spec)
    runner = SourceRunner(source, manager.events, store)
    await runner._session.close()
    runner._session = make_session(routes)
    runner._client = build_client(source, runner._session)
    await runner._poll_once()
    manager.runners[source["id"]] = runner
    return runner


@pytest.fixture
async def cluster(tmp_path: Path, make_session):
    """Two configured hosts that both report the `homelab` cluster."""
    store = Store(tmp_path / "config.json")
    manager = Manager(store)
    first = await add_runner(
        store,
        manager,
        make_session,
        {**PROXMOX_SOURCE, "name": "pve1", "host": "192.0.2.100"},
        PROXMOX_ROUTES,
    )
    second = await add_runner(
        store,
        manager,
        make_session,
        {**PROXMOX_SOURCE, "name": "pve2", "host": "192.0.2.101"},
        PROXMOX_ROUTES,
    )
    return store, manager, first, second


# -- identity ------------------------------------------------------------


async def test_backend_reports_the_cluster_it_belongs_to(cluster) -> None:
    _, _, first, _ = cluster
    assert first.cluster_id == "homelab"
    assert first.snapshot.meta["local_node"] == "pve1"


async def test_a_standalone_host_is_not_part_of_a_cluster(
    tmp_path: Path, make_session
) -> None:
    store = Store(tmp_path / "config.json")
    manager = Manager(store)
    runner = await add_runner(
        store,
        manager,
        make_session,
        {**PROXMOX_SOURCE, "name": "solo", "host": "10.0.0.9"},
        standalone_routes(),
    )
    assert runner.cluster_id is None
    assert len(manager.as_dict()["sources"]) == 1


# -- deduplication -------------------------------------------------------


async def test_a_cluster_is_published_once(cluster) -> None:
    _, manager, _, _ = cluster
    published = manager.as_dict()["sources"]
    assert len(published) == 1


async def test_the_published_data_is_not_halved(cluster) -> None:
    """Deduplicating must drop the duplicate, not the content."""
    _, manager, first, _ = cluster
    published = manager.as_dict()["sources"][0]
    alone = first.as_dict()
    assert len(published["sensors"]) == len(alone["sensors"])
    assert len(published["devices"]) == len(alone["devices"])


async def test_the_group_is_named_after_the_first_configured_member(cluster) -> None:
    """Anything less stable would re-key the cluster's entities."""
    _, manager, first, _ = cluster
    published = manager.as_dict()["sources"][0]
    assert published["id"] == first.id
    assert published["reported_by"] == first.id
    assert set(published["cluster_members"]) == {first.id, second_id(manager, first)}


def second_id(manager: Manager, first: SourceRunner) -> str:
    return next(i for i in manager.runners if i != first.id)


async def test_members_are_listed_for_the_ui(cluster) -> None:
    _, manager, first, second = cluster
    assert manager.cluster_status(first.id)["reporting"] is True
    standby = manager.cluster_status(second.id)
    assert standby["reporting"] is False
    assert standby["reported_by"] == "pve1"
    assert standby["members"] == 2


async def test_a_lone_source_has_no_cluster_status(
    tmp_path: Path, make_session
) -> None:
    store = Store(tmp_path / "config.json")
    manager = Manager(store)
    runner = await add_runner(
        store,
        manager,
        make_session,
        {**PROXMOX_SOURCE, "name": "solo", "host": "10.0.0.9"},
        PROXMOX_ROUTES,
    )
    assert manager.cluster_status(runner.id) is None


async def test_other_source_types_are_never_grouped(
    tmp_path: Path, make_session
) -> None:
    """Two routers are two devices, however similar their data looks."""
    store = Store(tmp_path / "config.json")
    manager = Manager(store)
    await add_runner(
        store,
        manager,
        make_session,
        {**MIKROTIK_SOURCE, "name": "rtr1", "host": "192.0.2.10"},
        MIKROTIK_ROUTES,
    )
    await add_runner(
        store,
        manager,
        make_session,
        {**MIKROTIK_SOURCE, "name": "rtr2", "host": "10.0.0.2"},
        MIKROTIK_ROUTES,
    )
    assert len(manager.as_dict()["sources"]) == 2


# -- failover ------------------------------------------------------------


async def test_reporting_moves_on_when_the_owner_goes_down(cluster) -> None:
    _, manager, first, second = cluster
    first.error = "Connection refused"

    published = manager.as_dict()["sources"][0]
    assert published["reported_by"] == second.id
    # The identity must not move with the reporter, or the cluster's entities
    # would be recreated — losing their history exactly during an outage.
    assert published["id"] == first.id
    assert published["available"] is True


async def test_actions_are_routed_to_a_host_that_is_up(cluster) -> None:
    _, manager, first, second = cluster
    assert manager.runner(first.id) is first

    first.error = "Connection refused"
    assert manager.runner(first.id) is second


async def test_the_group_still_reports_when_every_member_is_down(cluster) -> None:
    """Better to surface one unavailable cluster than to vanish silently."""
    _, manager, first, second = cluster
    first.error = second.error = "Connection refused"

    published = manager.as_dict()["sources"]
    assert len(published) == 1
    assert published[0]["available"] is False
    assert published[0]["error"] == "Connection refused"


async def test_reporting_returns_to_the_first_member_when_it_recovers(
    cluster,
) -> None:
    _, manager, first, second = cluster
    first.error = "Connection refused"
    assert manager.as_dict()["sources"][0]["reported_by"] == second.id

    first.error = None
    assert manager.as_dict()["sources"][0]["reported_by"] == first.id
