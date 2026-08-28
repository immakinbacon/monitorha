"""HTTP API and Ingress web UI.

Two kinds of client reach this server:

* The **Ingress UI**, proxied by the Supervisor, which has already
  authenticated the Home Assistant user. Those requests carry `X-Ingress-Path`.
* The **integration**, which connects over the Supervisor's internal Docker
  network and presents the add-on's API token as a bearer credential.

The port is never published to the host by default, so it is only reachable
from other containers on that network.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any

from aiohttp import web

from .api import AuthenticationError, ConnectionFailed, build_client, make_session
from .const import VERSION
from .manager import Manager
from .store import ConfigError, Store, redact, validate_source

_LOGGER = logging.getLogger(__name__)

WEB_ROOT = Path(__file__).parent / "web"

routes = web.RouteTableDef()


def _store(request: web.Request) -> Store:
    return request.app["store"]


def _manager(request: web.Request) -> Manager:
    return request.app["manager"]


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Allow Ingress traffic; require the API token for everything else."""
    if request.path.startswith("/api/") and request.path != "/api/health":
        if "X-Ingress-Path" not in request.headers:
            expected = _store(request).api_token
            supplied = request.headers.get("Authorization", "")
            # Refuse rather than accepting "Bearer " if the token is somehow
            # missing, so a broken store cannot open the API up.
            if not expected or not secrets.compare_digest(
                supplied, f"Bearer {expected}"
            ):
                return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


@web.middleware
async def error_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Turn expected failures into clean JSON instead of 500 pages."""
    try:
        return await handler(request)
    except ConfigError as err:
        return web.json_response({"error": str(err)}, status=400)
    except AuthenticationError as err:
        return web.json_response({"error": str(err)}, status=401)
    except ConnectionFailed as err:
        return web.json_response({"error": str(err)}, status=502)
    except web.HTTPException:
        raise
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Unhandled error serving %s", request.path)
        return web.json_response({"error": str(err)}, status=500)


# -- API -----------------------------------------------------------------


@routes.get("/api/health")
async def get_health(request: web.Request) -> web.Response:
    """Unauthenticated liveness probe.

    Reports the version so "which build is actually running?" is answerable
    without shell access — the add-on store silently keeps serving an old
    build if its repository URL stops resolving.
    """
    return web.json_response(
        {
            "ok": True,
            "version": VERSION,
            "sources": len(_manager(request).runners),
        }
    )


@routes.get("/api/snapshot")
async def get_snapshot(request: web.Request) -> web.Response:
    """Everything the integration needs to build its entities."""
    return web.json_response(_manager(request).as_dict())


@routes.get("/api/sources/{source_id}/snapshot")
async def get_source_snapshot(request: web.Request) -> web.Response:
    """One source's readings, for the web UI's detail page."""
    runner = _manager(request).runner(request.match_info["source_id"])
    return web.json_response(runner.as_dict())


@routes.get("/api/sources")
async def list_sources(request: web.Request) -> web.Response:
    """Stored configuration, with secrets redacted, plus live status."""
    manager = _manager(request)
    result = []
    for source in _store(request).sources:
        entry = redact(source)
        runner = manager.runners.get(source["id"])
        entry["status"] = {
            "running": runner is not None,
            "available": bool(runner and runner.available),
            "error": runner.error if runner else None,
            "auth_failed": bool(runner and runner.auth_failed),
            "last_update": (
                runner.last_update.isoformat()
                if runner and runner.last_update
                else None
            ),
            "entities": (
                len(runner.snapshot.sensors)
                + len(runner.snapshot.binary_sensors)
                + len(runner.snapshot.switches)
                + len(runner.snapshot.updates)
                if runner and runner.snapshot
                else 0
            ),
            # Named, not just counted: the card says how many updates are
            # waiting and which ones without a second request per device.
            "pending_updates": (
                [u.name for u in runner.snapshot.pending_updates()]
                if runner and runner.snapshot
                else []
            ),
            # Set only for a host that shares a cluster with another source,
            # so the UI can show which one is actually reporting it.
            "cluster": manager.cluster_status(source["id"]),
        }
        result.append(entry)
    return web.json_response({"sources": result, "api_token": _store(request).api_token})


@routes.post("/api/sources")
async def add_source(request: web.Request) -> web.Response:
    payload = await request.json()
    source = _store(request).add(payload)
    await _manager(request).sync()
    return web.json_response(redact(source), status=201)


@routes.put("/api/sources/{source_id}")
async def update_source(request: web.Request) -> web.Response:
    payload = await request.json()
    source = _store(request).update(request.match_info["source_id"], payload)
    await _manager(request).sync()
    return web.json_response(redact(source))


@routes.delete("/api/sources/{source_id}")
async def delete_source(request: web.Request) -> web.Response:
    _store(request).remove(request.match_info["source_id"])
    await _manager(request).sync()
    return web.json_response({"ok": True})


@routes.post("/api/sources/test")
async def test_source(request: web.Request) -> web.Response:
    """Validate credentials without saving, so the UI can report problems."""
    payload = await request.json()
    existing = None
    if payload.get("id"):
        existing = _store(request).get(payload["id"])
    config = validate_source(payload, existing=existing)

    session = make_session(bool(config.get("verify_ssl", False)))
    try:
        client = build_client(config, session)
        info = await client.async_validate()
    finally:
        await session.close()
    return web.json_response({"ok": True, "info": info})


@routes.post("/api/sources/{source_id}/refresh")
async def refresh_source(request: web.Request) -> web.Response:
    _manager(request).runner(request.match_info["source_id"]).request_refresh()
    return web.json_response({"ok": True})


@routes.get("/api/events")
async def get_events(request: web.Request) -> web.Response:
    """Events newer than `since`, for the integration to put on the HA bus.

    The reply always carries `head`, so a caller starting up can jump straight
    to the current position instead of replaying history it has already acted
    on — or, on a first run, acted on before it existed.
    """
    raw = request.query.get("since", "0")
    try:
        since = int(raw)
    except ValueError:
        raise ConfigError(f"since must be a number, got {raw!r}") from None

    log = _manager(request).events
    return web.json_response(
        {"head": log.head, "events": [e.as_dict() for e in log.since(since)]}
    )


@routes.get("/api/sources/{source_id}/overrides")
async def get_overrides(request: web.Request) -> web.Response:
    source_id = request.match_info["source_id"]
    if _store(request).get(source_id) is None:
        raise ConfigError(f"No such source: {source_id}")
    return web.json_response({"overrides": _store(request).overrides_for(source_id)})


@routes.put("/api/sources/{source_id}/overrides/{entity_key}")
async def put_override(request: web.Request) -> web.Response:
    """Mute a monitor line or set its thresholds.

    Deliberately does not call `Manager.sync()`: overrides live outside the
    source config precisely so changing one does not restart the poller.
    """
    payload = await request.json()
    override = _store(request).set_override(
        request.match_info["source_id"], request.match_info["entity_key"], payload
    )
    return web.json_response(override)


@routes.delete("/api/sources/{source_id}/overrides/{entity_key}")
async def delete_override(request: web.Request) -> web.Response:
    _store(request).clear_override(
        request.match_info["source_id"], request.match_info["entity_key"]
    )
    return web.json_response({"ok": True})


@routes.post("/api/action")
async def post_action(request: web.Request) -> web.Response:
    """Invoke a button, switch or update on behalf of the integration."""
    payload = await request.json()
    runner = _manager(request).runner(payload["source_id"])
    await runner.act(payload["kind"], payload["key"], payload.get("value"))
    return web.json_response({"ok": True})


# -- Ingress UI ----------------------------------------------------------


@routes.get("/")
async def get_index(request: web.Request) -> web.Response:
    """The single page, with its asset URLs stamped with the version.

    Without the stamp the browser keeps whatever `static/app.js` it cached:
    aiohttp sends an ETag but no `Cache-Control`, so a browser applies
    heuristic freshness and serves the old file *without revalidating*. The
    page's markup would update while its behaviour did not.
    """
    return web.Response(
        text=_index_html(),
        content_type="text/html",
        # The document itself must never be held, or the new asset URLs
        # inside it are never seen either.
        headers={"Cache-Control": "no-cache"},
    )


def _index_html() -> str:
    """Read and stamp index.html, caching the result for the process."""
    global _INDEX_CACHE
    if _INDEX_CACHE is None:
        _INDEX_CACHE = (WEB_ROOT / "index.html").read_text().replace(
            "__VERSION__", VERSION
        )
    return _INDEX_CACHE


_INDEX_CACHE: str | None = None


@web.middleware
async def cache_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Make static assets revalidate rather than be assumed fresh.

    The URLs are version-stamped, so this only costs a conditional request
    per release; in return a rebuilt add-on can never be paired with a stale
    script, which presents as features silently not working.
    """
    response = await handler(request)
    if request.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


def create_app(store: Store, manager: Manager) -> web.Application:
    app = web.Application(
        middlewares=[error_middleware, auth_middleware, cache_middleware]
    )
    app["store"] = store
    app["manager"] = manager
    app.add_routes(routes)
    # Served under the Ingress path prefix, so the UI uses relative URLs.
    app.router.add_static("/static/", WEB_ROOT, name="static")
    return app
