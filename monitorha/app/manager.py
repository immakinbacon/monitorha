"""Source lifecycle and the polling loops.

Each configured source gets its own task polling on its own tiered schedule.
One slow or broken device therefore cannot stall the others, and the HTTP API
always answers immediately from the last good snapshot.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from .api import AuthenticationError, MonitorError, build_client, make_session
from .events import EventLog, apply_overrides, availability_event, evaluate
from .models import Snapshot
from .serialize import snapshot_to_dict
from .store import Store

_LOGGER = logging.getLogger(__name__)

# How long to wait after a failure before trying again, and the ceiling for
# the backoff so a dead device is still retried periodically.
_RETRY_BASE = 15
_RETRY_MAX = 300


class SourceRunner:
    """Polls one device and holds its latest snapshot."""

    def __init__(
        self,
        config: dict[str, Any],
        events: EventLog | None = None,
        store: Store | None = None,
    ) -> None:
        self.config = config
        self.snapshot: Snapshot | None = None
        self.last_update: datetime | None = None
        self.error: str | None = None
        self.auth_failed = False
        self._session = make_session(bool(config.get("verify_ssl", False)))
        self._client = build_client(config, self._session)
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._tick = 0
        self._failures = 0
        self._events = events if events is not None else EventLog()
        # Read through to the store on each poll rather than copied in, so
        # editing a threshold takes effect without restarting the poller.
        self._store = store
        self._previous: Snapshot | None = None
        self._was_available: bool | None = None

    @property
    def overrides(self) -> dict[str, Any]:
        return self._store.overrides_for(self.id) if self._store else {}

    @property
    def cluster_id(self) -> str | None:
        """Cluster this source belongs to, if the backend reports one."""
        if self.snapshot is None:
            return None
        value = self.snapshot.meta.get("cluster_id")
        return str(value) if value else None

    @property
    def id(self) -> str:
        return self.config["id"]

    @property
    def available(self) -> bool:
        return self.snapshot is not None and self.error is None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"poll-{self.id}")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        try:
            # Hand back anything held on the device (a Redfish session slot)
            # before dropping the transport it would need to do so.
            await self._client.async_close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            _LOGGER.debug("Error closing client for %s", self.config["name"])
        await self._session.close()

    def request_refresh(self) -> None:
        """Poll now instead of waiting out the interval."""
        self._wake.set()

    @property
    def _polls_per_slow_cycle(self) -> int:
        fast = max(1, int(self.config.get("scan_interval", 60)))
        slow = max(fast, int(self.config.get("slow_scan_interval", 900)))
        return max(1, round(slow / fast))

    async def _run(self) -> None:
        while True:
            await self._poll_once()
            delay = int(self.config.get("scan_interval", 60))
            if self.error:
                # Back off on a broken device rather than hammering it.
                delay = min(_RETRY_MAX, _RETRY_BASE * (2 ** min(self._failures, 5)))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass
            self._wake.clear()

    def _record_availability(self) -> None:
        """Raise an event when the source itself changes state.

        No snapshot can describe this: a device that stops answering simply
        stops producing readings.
        """
        available = self.available
        if self._was_available is not None and self._was_available != available:
            self._events.append(
                [
                    availability_event(
                        source_id=self.id,
                        source_name=self.config["name"],
                        available=available,
                        error=self.error,
                    )
                ]
            )
        self._was_available = available

    async def _poll_once(self) -> None:
        slow = self._tick == 0
        try:
            self.snapshot = await self._client.async_fetch(slow=slow)
        except AuthenticationError as err:
            # Credentials will not fix themselves; stop advancing the tier and
            # flag it clearly for the UI.
            self.error = str(err)
            self.auth_failed = True
            self._failures += 1
            _LOGGER.warning("Auth failed for %s: %s", self.config["name"], err)
        except MonitorError as err:
            self.error = str(err)
            self._failures += 1
            _LOGGER.warning("Poll failed for %s: %s", self.config["name"], err)
        except Exception:  # noqa: BLE001 - a backend bug must not kill the loop
            self.error = "Internal error while polling; see the add-on log"
            self._failures += 1
            _LOGGER.exception("Unexpected error polling %s", self.config["name"])
        else:
            self.error = None
            self.auth_failed = False
            self._failures = 0
            self._tick = (self._tick + 1) % self._polls_per_slow_cycle
            self.last_update = datetime.now(UTC)

            overrides = self.overrides
            # Stamp bands on before diffing, so what Home Assistant sees as an
            # attribute and what the event reports cannot disagree.
            apply_overrides(self.snapshot, overrides)
            self._events.append(
                evaluate(
                    self._previous,
                    self.snapshot,
                    overrides,
                    source_id=self.id,
                    source_name=self.config["name"],
                )
            )
            self._previous = self.snapshot
        self._record_availability()

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "type": self.config["type"],
            "name": self.config["name"],
            "host": self.config["host"],
            "port": self.config["port"],
            "enabled": self.config.get("enabled", True),
            "available": self.available,
            "error": self.error,
            "auth_failed": self.auth_failed,
            "last_update": self.last_update.isoformat() if self.last_update else None,
            "scan_interval": self.config.get("scan_interval"),
            "slow_scan_interval": self.config.get("slow_scan_interval"),
            # Carried with the readings so the UI can render a line's mute and
            # threshold state without a second request.
            "overrides": self.overrides,
        }
        payload.update(
            snapshot_to_dict(self.snapshot)
            if self.snapshot is not None
            else {
                "devices": [],
                "sensors": [],
                "binary_sensors": [],
                "updates": [],
                "buttons": [],
                "switches": [],
            }
        )
        return payload

    async def act(self, kind: str, key: str, value: bool | None) -> None:
        """Invoke a button, switch or update action by key."""
        if self.snapshot is None:
            raise MonitorError(f"{self.config['name']} has not been polled yet")

        if kind == "button":
            spec = self.snapshot.buttons.get(key)
            if spec is None:
                raise MonitorError(f"No such button: {key}")
            await spec.press()
        elif kind == "switch":
            spec = self.snapshot.switches.get(key)
            if spec is None:
                raise MonitorError(f"No such switch: {key}")
            await spec.turn(bool(value))
        elif kind == "update":
            spec = self.snapshot.updates.get(key)
            if spec is None or spec.install is None:
                raise MonitorError(f"{key} cannot be installed")
            spec.in_progress = True
            try:
                await spec.install()
            finally:
                spec.in_progress = False
        else:
            raise MonitorError(f"Unknown action kind: {kind}")

        self.request_refresh()


class Manager:
    """Owns every runner and keeps them in step with the store."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.runners: dict[str, SourceRunner] = {}
        # One log across every source, so the integration needs a single
        # cursor rather than one per device.
        self.events = EventLog()

    async def start(self) -> None:
        await self.sync()

    async def stop(self) -> None:
        await asyncio.gather(*(r.stop() for r in self.runners.values()))
        self.runners.clear()

    async def sync(self) -> None:
        """Reconcile running pollers with the stored source list."""
        configured = {s["id"]: s for s in self.store.sources if s.get("enabled", True)}

        for source_id in list(self.runners):
            current = configured.get(source_id)
            if current is None or current != self.runners[source_id].config:
                # Removed, disabled, or edited: tear down and rebuild so the
                # new credentials and options take effect.
                await self.runners.pop(source_id).stop()

        for source_id, config in configured.items():
            if source_id not in self.runners:
                runner = SourceRunner(config, self.events, self.store)
                self.runners[source_id] = runner
                runner.start()

    # -- cluster deduplication --------------------------------------------
    #
    # Proxmox answers `/cluster/resources` and `/cluster/status` identically
    # from every node, so two configured hosts in one cluster each report the
    # whole cluster. Publishing both would double every node, guest, storage
    # and the quorum sensor, so one member is elected to report for the group.

    def _groups(self) -> list[list[SourceRunner]]:
        """Runners bucketed by the cluster they report, in configuration order.

        Order comes from the store rather than the runner dict because the
        first-configured member names the group, and that name becomes the
        published source id. Anything less stable would re-key the group's
        Home Assistant entities as members are added or go offline.
        """
        buckets: dict[str, list[SourceRunner]] = {}
        for source in self.store.sources:
            runner = self.runners.get(source["id"])
            if runner is None:
                continue
            cluster = runner.cluster_id
            key = f"{runner.config['type']}:{cluster}" if cluster else runner.id
            buckets.setdefault(key, []).append(runner)
        return list(buckets.values())

    def _elections(self) -> list[tuple[str, SourceRunner, list[SourceRunner]]]:
        """(published id, reporting runner, members) for each group.

        The identity is the earliest-configured member and never moves. The
        reporter is the earliest-configured member that is currently up, so a
        host going down hands over without the entities changing identity.
        """
        result = []
        for members in self._groups():
            identity = members[0].id
            owner = next((m for m in members if m.available), members[0])
            result.append((identity, owner, members))
        return result

    def cluster_status(self, source_id: str) -> dict[str, Any] | None:
        """How this source relates to its cluster group, or None if it is alone."""
        for identity, owner, members in self._elections():
            if len(members) < 2 or source_id not in [m.id for m in members]:
                continue
            return {
                "identity": identity,
                "reporting": owner.id == source_id,
                "reported_by": owner.config["name"],
                "members": len(members),
            }
        return None

    def as_dict(self) -> dict[str, Any]:
        sources = []
        for identity, owner, members in self._elections():
            payload = owner.as_dict()
            # Published under the group's identity, not the reporter's, so
            # failover does not create a second set of entities.
            payload["id"] = identity
            if len(members) > 1:
                payload["cluster_members"] = [m.id for m in members]
                payload["reported_by"] = owner.id
            sources.append(payload)
        return {"sources": sources}

    def runner(self, source_id: str) -> SourceRunner:
        # A group identity resolves to whichever member is currently up, so an
        # action aimed at the cluster reaches a host that can perform it.
        for identity, owner, _ in self._elections():
            if identity == source_id:
                return owner
        runner = self.runners.get(source_id)
        if runner is None:
            raise MonitorError(f"Source {source_id} is not running")
        return runner
