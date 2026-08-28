# Changelog

## 0.11.1

- **One Check for updates button on MikroTik, not two.** The button was
  registered twice under two different keys — once beside the reboot button and
  once beside the update entities it belongs with — so Home Assistant created
  two entities with the same name that did the same thing. The one next to the
  updates is what survives; the older `check_updates` entity will show as
  unavailable until it is deleted from the entity registry, because a different
  unique ID is a new entity rather than a rename.

## 0.11.0

- **MikroTik switches are a device type.** SwOS and SwOS Lite have no REST API: the web
  interface reads a handful of `.b` files whose numbers are hex and whose strings are
  hex-encoded ASCII, behind HTTP digest authentication that aiohttp does not speak. All
  three are implemented here, so a CRS or CSS switch is added the same way as anything
  else — its web address and the user you log into it with.

  Both dialects are covered. SwOS names its fields (`lnk`, `spd`, `temp`) and SwOS Lite
  numbers them (`i06`, `i08`, `i22`); `sys.b` says which one is answering and one table
  maps both onto the same readings. Every field meaning, scale factor and option list
  was taken from the switch's own web UI, which carries a definition of each page — the
  alternative was guessing at what a hex number means, and a voltage read at the wrong
  scale is worse than no voltage at all.

  You get ports with their configured names, link state, negotiated speed and duplex,
  and 64-bit byte counters; per-port PoE status, power, voltage and current, with
  overload, short circuit and controller errors raised as problems; SFP temperature,
  voltage, bias and optical power in dBm; PSU voltages, fans, board temperatures and
  total draw. It is read-only apart from a reboot button, which ships disabled: applying
  anything to a SwOS switch means POSTing a whole endpoint back, and a monitor should
  not be rewriting a switch's port configuration to do its job.

- **SwOS firmware updates are checked the way the switch checks them.** SwOS cannot look
  for its own updates — its web interface makes your *browser* ask MikroTik. The add-on
  now makes that same request on the deep poll, reading the product code out of the
  switch's own web UI first, because it is not derivable from the model: a CRS310-8G+2S+
  is published as `css310g`. A switch with no route out reports its installed version and
  nothing more, and the whole check can be turned off per device.

- **A device says on its card when it has updates waiting.** Pending updates were only
  visible by opening a device and reading down its monitor list, which is the one place
  you do not look when you are wondering whether anything needs attention. Each card now
  carries an "n updates pending" badge naming them on hover, the device page repeats it,
  and a tile above the list counts them across everything configured. It counts real
  upgrades only — a backend that can merely read a version back publishes it with
  installed and available equal, and that is not an update waiting.

## 0.10.3

- **The add-on has an icon.** Three stacked rack units on a Home Assistant blue
  tile, one lit status LED each — three because the add-on talks to three kinds
  of box, stacked because that is the rack they live in. Interior detail is
  deliberately sparse: the store shows the icon at 128px but the add-on list
  renders it far smaller, and anything finer collapses into mud at 32px.

  It is drawn by `tools/make_icon.py` rather than hand-pixelled, so it can be
  re-rendered at any size, and a dark variant matching the web UI is one
  argument away. Pillow is a development-time dependency only — the image still
  ships nothing but aiohttp.

## 0.10.2

- **A guest is no longer narrower than the node it hangs off.** Nesting in the
  device view was an indent, so every level lost 20px of width and the reading
  tables stepped out of alignment with each other. Depth is now a rule down the
  left edge and every box keeps the full width.

- **The page is wider** — 1280px rather than 1000px, so a reading and its value
  stay on one line on a node carrying dozens of disks.

- The 403 message on a node's status now leads with the cause seen in the wild:
  a permission on `/nodes` *replaces* the one inherited from `/` rather than
  adding to it, so adding a row there to be specific silently removes the
  access a grant on `/` was providing. `pveum acl list` shows those rows. The
  README carries the same warning.

## 0.10.1

- **A Proxmox node that cannot be read now says so.** `/nodes/{node}/status`
  supplies CPU, memory, swap, root filesystem, load average and kernel, and it
  was treated as an optional subsystem: a `403` became six blank readings with
  nothing in the log. It is now only allowed to fail for transient reasons — a
  wedged or unimplemented endpoint still degrades quietly, and a timeout still
  keeps the rest of the node working — while `401`, `403` and `404` fail the
  poll and name the cause on the device card.

  The 403 message spells out the usual causes, because the role is rarely the
  thing that is wrong: a permission that is not set to **Propagate** applies to
  the path `/` literally and never reaches `/nodes/…` or `/vms/…`, and a token
  with **Privilege Separation** enabled gets the *intersection* of its own
  rights and its user's, so both sides need the grant.

- **Test now tests something.** Validation used to call `/version` and a soft
  `/cluster/status`, which answer for a token with almost no privileges, so
  credentials that could not produce a single reading reported success. It now
  reads the node's status as well, and fails when that is refused.

- **A node reported offline is no longer polled.** In node scope the target was
  forced online whenever `/cluster/status` listed no *online* node, which meant
  a downed node had its endpoints polled every cycle.

- The Proxmox permission steps in the README now cover Propagate and privilege
  separation, and show how to ask Proxmox what a token can actually see.

## 0.10.0

- **Proxmox monitors one node by default, not the whole cluster.** An entry now
  reports the node you point it at — its interfaces, storages and the guests
  running on it — and nothing about the rest of the cluster. Add one entry per
  node you care about. The node is detected from the `local` flag in
  `/cluster/status`, and **Node name** overrides it when you connect through a
  VIP that can answer as any member.

  Set **Monitor** to *The whole cluster* to keep the previous behaviour: one
  entry discovers every node and publishes a cluster device with quorum and
  node-count sensors. Standby election and cluster deduplication apply only to
  that scope — in node scope each entry reports only itself, so two nodes of one
  cluster both publish, as they should.

  In node scope the node **is** the top-level device rather than hanging off a
  cluster device, and its guests hang off it directly.

- **Proxmox node network interfaces.** Physical NICs, bridges and bonds from
  `/nodes/{node}/network` now report link state, with type, address/CIDR,
  gateway, bridge members, bond mode and autostart as attributes. Loopback is
  skipped. Turn it off with **Monitor interfaces**.


## 0.9.0

- **An event log in the add-on's web UI.** The add-on has been detecting
  changes since 0.5.0, but the only way to see one was to catch the Home
  Assistant automation it fired. **Event log** in the header now lists them —
  newest first, with what changed, which device it happened on, and why —
  behind a filter box.

  These are the same events the integration puts on the bus, so it is where to
  confirm a threshold behaves as intended *before* building an automation on
  it, and where to look when one fires unexpectedly. It holds the most recent
  500 and is cleared when the add-on restarts.

## 0.8.2

- **Fixes an updated add-on still running its old web UI.** `app.js` and
  `style.css` were served from unversioned URLs with an ETag but no
  `Cache-Control`. A browser given no `Cache-Control` applies heuristic
  freshness and serves its cached copy *without revalidating*, so the ETag was
  never consulted — the page's markup updated while its JavaScript did not.

  This failed silently and looked like a broken feature rather than a delivery
  problem: monitor rows that would not open their settings, buttons that did
  nothing, all while the header showed the new version.

  Asset URLs now carry the version, so a release is a new URL; the page itself
  is `no-cache`, so those URLs are actually seen; and static assets revalidate
  rather than being assumed fresh.

## 0.8.1

- The running version is now shown in the web UI header and returned by
  `/api/health`. An add-on store whose repository URL has stopped resolving
  keeps serving the previously installed build without saying so, which makes
  "am I actually running the new code?" impossible to answer from the UI. A
  test keeps `config.yaml`, `manifest.json` and the app's own constant in step,
  since the image ships only `app/` and cannot read its own config.

## 0.8.0

- **Full Redfish firmware inventory.** Only the BIOS was reported before;
  every component the BMC inventories — NICs, backplanes, CPLDs, drives — now
  gets its own update entity, recording whether the BMC considers it
  updateable. They are created disabled, because a server can list dozens.

  These report rather than offer an install: there is no public feed for what
  the latest version of a given board is, and Redfish `SimpleUpdate` needs a
  firmware image URI that only an operator can supply.

- **Buttons in the add-on's UI.** Update rows that can actually be installed
  now have an Install button, and each device's actions — reboot, identify,
  power control — are real buttons rather than labels. Previously the add-on
  could perform these but only Home Assistant could ask it to.

- **Check for updates** on MikroTik, forcing the update check that otherwise
  only runs on the slow tier, and **Refresh package list** per Proxmox node,
  which runs the `apt update` half that Proxmox does expose. The upgrade
  itself still has no API and stays a shell job.

## 0.7.0

- **Tunnel monitoring** for MikroTik. WireGuard interfaces and each of their
  peers, OpenVPN / L2TP / SSTP / PPTP clients, and IPsec peers and phase-2
  policies all become connectivity entities, and say why when they are down.

  WireGuard has no session to inspect, so a peer is judged by its last
  handshake: peers rekey roughly every two minutes, and silence for more than
  three counts as down. A peer that has never handshaked reads as down rather
  than unknown. Inbound dial-in sessions are counted per service instead of
  being given an entity each, which would churn the entity registry as users
  connect and disconnect.

- **Netwatch** entries are read. Each enabled `/tool/netwatch` host becomes a
  connectivity entity — using its comment as the name where it has one — with
  round-trip time and packet loss for ICMP probes, and the time it went down.

- Both are on by default and can be turned off per device. Every endpoint is
  optional, so a router without the WireGuard package, or an older RouterOS
  missing one of these menus, keeps polling normally.

## 0.6.0

- **Problems now say what is wrong.** A `problem` binary sensor reported only
  that something was unhappy. Each one now carries a `reason`: the BMC's own
  wording from Redfish `Status.Conditions` where the firmware provides it,
  which nodes are missing when a Proxmox cluster loses quorum, the vzdump exit
  status behind a failed backup, the SMART verdict on a failing disk, the ZFS
  pool state, and RouterOS's raw state word for a failed PSU. It shows in the
  add-on UI under the monitor's name, reaches Home Assistant as the `reason`
  attribute, and travels with the event.

- **A Proxmox cluster is published once.** `/cluster/resources` and
  `/cluster/status` answer identically from every node, so a second configured
  host in the same cluster used to duplicate every node, guest, storage and the
  quorum sensor. Hosts reporting the same cluster are now grouped, and one is
  elected to report it.

  Adding the other nodes is now worthwhile rather than harmful: if the
  reporting host becomes unreachable another takes over automatically, so the
  cluster keeps reporting instead of going dark with it. The group keeps the
  identity of the first-configured member, so a handover does not recreate the
  entities and their history survives. Actions aimed at the cluster are routed
  to whichever member is currently up.

## 0.5.0

- **Change events.** The add-on now compares each poll with the last one and
  records what moved: problems raised and cleared, thresholds crossed, updates
  becoming available, and sources going up or down. The integration republishes
  these on the Home Assistant bus as `monitorha_event`, so an automation can
  trigger on them:

  ```yaml
  triggers:
    - trigger: event
      event_type: monitorha_event
      event_data:
        kind: threshold_critical
  ```

  A restart does not replay history: the integration adopts the add-on's
  current position on its first poll rather than firing for changes it has
  already handled.

- **Thresholds.** Any numeric monitor can carry warning and critical bounds,
  above and below. An event fires when a value moves into a *different band*,
  so a figure hovering either side of a bound reports once rather than on every
  poll. The current band and the bound that caused it are exposed on the entity
  as the `severity` and `reason` attributes.

- **Ignore.** A monitor can be muted. It keeps its Home Assistant entity and
  its real state, but raises no events and is left out of the problem rollup —
  so muting and unmuting never churns the entity registry.

- **Clickable monitors.** Every line in a device's detail view opens a modal
  for its mute and threshold settings. Muted and thresholded lines are marked
  in the list.

  Per-line settings are stored separately from the device's credentials, so
  changing one no longer restarts that device's poller.

## 0.4.2

- Fixes the same-origin check added in 0.4.1 wrongly refusing a redirect that
  drops the scheme's default port. Clients always build URLs with an explicit
  `:443`, while devices omit it in `Location`, so `https://host:443/x` ->
  `https://host/x` was treated as a different host and refused.

## 0.4.1

- Redirects are now followed without changing the request. aiohttp rewrites
  POST to GET on a 301 and discards the body, and some Supermicro firmware
  301s every collection URI to its trailing-slash form. A session login
  therefore arrived with no credentials ("Credentials rejected" despite a
  working web-UI login), and a power action would have been downgraded to a
  no-op GET. Method and body are now preserved across the hop.
- A redirect to a different origin is refused rather than replaying
  credentials to another host.

## 0.4.0

- Redfish now falls back to **session authentication** when a BMC refuses HTTP
  Basic. Some older Supermicro firmware serves Redfish but only accepts a
  token from `SessionService/Sessions`, which looked like "credentials
  rejected" despite the same user working in the web UI.
- One session is opened and reused, and released on shutdown. BMCs cap
  concurrent sessions and expire them slowly, so a session per request would
  eventually lock the account out of its own API.
- An expired token is renewed automatically, and the session path is taken
  from the service root rather than assumed.

## 0.3.1

- The Host field now accepts a URL pasted from a browser (`https://bmc.example/`)
  as well as a bare hostname or `host:port`. A port given there wins over the
  prefilled port box. Bare IPv6 addresses are not mistaken for `host:port`.

## 0.3.0

- New **View monitors** page: everything a device is currently reporting, grouped by
  device and following the real hierarchy, with live values and a filter box.
  Readings that Home Assistant hides by default are marked, so they can be found
  before enabling them.
- A slow or wedged subsystem no longer fails the whole source. Proxmox's disk
  inventory shells out to smartctl and is proxied when it targets another node, so
  it can time out on a healthy cluster; the deep-tier endpoints now get a 90s
  timeout and keep their previous values when a cycle fails.
- New `GET /api/sources/{id}/snapshot` endpoint behind the detail page.

## 0.2.1

- A `403` on an optional Proxmox endpoint no longer fails the whole source.
  Proxmox requires `Sys.Modify` to read `/nodes/{node}/apt/update` even though
  it is a read-only call, so a token with only `PVEAuditor` used to go dark
  entirely. It now degrades to losing just the update entities.
- `401` and `403` are reported separately, and a `403` names the endpoint whose
  privilege is missing.
- The web UI shows that message instead of a generic "Authentication failed".

## 0.2.0

- Split into an add-on plus a companion Home Assistant integration. The add-on
  owns the polling, the credentials and this configuration UI; the integration
  reads its snapshot and publishes native Home Assistant entities.
- Fixed a blank API token on a fresh install, which let `Bearer ` authenticate.

## 0.1.0

- Initial release: MikroTik (RouterOS v7 REST), Proxmox VE and
  Supermicro/Redfish monitoring, with power control.
