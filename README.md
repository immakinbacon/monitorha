# Infrastructure Monitor

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-support-FFDD00?style=flat-square&logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/immakinbacon)

Monitors homelab infrastructure over each vendor's own management API and surfaces it
in Home Assistant as native entities — no agents, no SNMP, no `ipmitool`, no MQTT broker.

| Source | Protocol | Requires |
| --- | --- | --- |
| MikroTik | RouterOS v7 `/rest` over HTTPS | RouterOS 7.x with the `www-ssl` service enabled |
| MikroTik switches | SwOS / SwOS Lite `.b` endpoints over HTTP | A CRS/CSS switch running SwOS or SwOS Lite |
| Proxmox VE | `/api2/json` over HTTPS | An API token (or username/password) |
| Supermicro / other BMCs | Redfish `/redfish/v1` over HTTPS | Supermicro X11 (recent FW), X12/X13, H12/H13 — also iDRAC, iLO, XCC |

## How it fits together

This repository ships **two halves**, and you install both:

```
Add-on  (Docker container)              Integration  (custom_components)
├─ polls the devices                    ├─ reads the add-on's snapshot
├─ holds the credentials                ├─ creates native HA entities
├─ web UI for configuration             ├─ device registry + config flow
└─ HTTP API on port 8099   ──────────►  └─ forwards power actions back
```

The add-on does all the talking to hardware, so credentials live in it rather than in
Home Assistant, and one slow BMC cannot stall anything else. The integration is a thin
client that turns the add-on's data into real HA entities — so you get add-on-store
installation *and* a proper device registry, config flow, update entities and history.

The add-on has no Python dependencies beyond `aiohttp`.

## Installation

**1. Add the repository to the Add-on Store.** In Home Assistant go to
**Settings → Add-ons → Add-on Store → ⋮ → Repositories**, and add:

```
https://github.com/immakinbacon/monitorha
```

**2. Install and start "Infrastructure Monitor".** Open its web UI (the **Infrastructure**
item in the sidebar, or **Open Web UI** on the add-on page) and add your devices there.

**3. Add the integration.** Home Assistant should offer it automatically once the add-on
is running — look for a discovered **Infrastructure Monitor** under
**Settings → Devices & Services**.

If it is not discovered, add it manually: **Add Integration → Infrastructure Monitor**,
then use the host, port and API token shown under *Connection details for the Home
Assistant integration* in the add-on's web UI. (Older Supervisor builds validate the
discovery service name against a fixed list and silently drop custom ones; manual entry
is the fallback.)

The integration is a normal custom component, so it also needs to be present in
`config/custom_components/`. Install it with HACS, or clone this repository and
symlink the component into place:

```bash
git clone https://github.com/immakinbacon/monitorha.git /config/monitorha-repo
ln -s /config/monitorha-repo/custom_components/monitorha /config/custom_components/monitorha
```

Restart Home Assistant after installing or updating the integration; Python integrations
are not hot-reloaded. The add-on updates independently through the Add-on Store.

## The add-on's web UI

Reachable from the sidebar (**Infrastructure**) or **Open Web UI** on the add-on page.

The **device list** shows every configured source with its connection state, last poll
time and entity count, and is where you add, test, edit, disable and remove devices.
"Test connection" validates credentials before saving. A device with a newer version
available anywhere in it carries an **"n updates pending"** badge on its card — hover it
to see which ones — and the tiles above the list count the outstanding updates across
everything configured. The same badge appears on the device's own page.

**View monitors** opens a per-device page listing everything currently being read,
grouped by device and following the real hierarchy — a Proxmox node shows its guests
indented beneath it (or, in cluster scope, the cluster shows its nodes and each node its
guests). Every sensor, health state,
control, pending update and available action appears with its live value, and a filter
box narrows a long list. Readings marked *hidden in HA* exist here but are disabled by
default in Home Assistant, so this is the place to discover them before enabling.

**Click any monitor** to open its settings: mute it, or give it thresholds.

## Thresholds, muting and events

Every numeric monitor can carry warning and critical bounds, above and below — leave a
box empty to not use that bound. The band a value currently sits in, and the bound that
put it there, reach Home Assistant as the `severity` and `reason` attributes, so a
`problem` state finally says *what* the problem is.

Muting a monitor keeps its entity and its real state but stops it raising events and
drops it from the problem rollup. Nothing is removed from Home Assistant, so muting and
unmuting never breaks history or a dashboard reference.

Per-line settings are stored separately from device credentials, so changing one does
not restart that device's poller.

### The event log

**Event log** in the add-on's header lists every change the add-on has detected — what
it was, which device it happened on, and why — newest first, with a filter box. These
are exactly the events the integration puts on the Home Assistant bus, so it is the
place to see what an automation would have fired on, and to check that a threshold you
set is behaving before you build anything on it.

It holds the most recent 500 events and is cleared when the add-on restarts.

### Automations

Each detected change is fired on the Home Assistant bus as a `monitorha_event`:

```yaml
triggers:
  - trigger: event
    event_type: monitorha_event
    event_data:
      kind: threshold_critical
actions:
  - action: notify.mobile_app
    data:
      message: "{{ trigger.event.data.name }}: {{ trigger.event.data.reason }}"
```

`kind` is one of `problem`, `recovery`, `threshold_warning`, `threshold_critical`,
`threshold_clear`, `state_change`, `update_available`, `source_available` or
`source_unavailable`. Omit `event_data` to catch everything, or narrow it by
`source_id`, `device_key` or `entity_key`. Alongside those, each event carries
`source_name`, `device_name`, `name`, `old_state`, `new_state`, `severity`, `reason`
and `timestamp`.

Thresholds only fire when a value moves into a *different* band, so a figure sitting on
a bound reports once rather than on every poll. A Home Assistant restart does not
replay history: the integration adopts the add-on's current position on its first poll.

## What it tracks

**Health & telemetry** — CPU, memory, disk and swap usage; temperatures, fan speeds,
voltages and PSU status; chassis power draw in watts; load averages; interface link
state and byte counters; last-boot timestamps.

**Switch ports** — on SwOS switches, every port's link state, negotiated speed and duplex
with its configured name, plus 64-bit RX/TX byte counters; per-port PoE status, power,
voltage and current, with a fault state (overload, short circuit, controller error)
raised as a problem; SFP module temperature, voltage, bias and optical TX/RX power in
dBm, with vendor, part and serial as attributes.

**Update status** — RouterOS version against the release channel, RouterBOARD firmware,
SwOS firmware against what MikroTik publishes for that model,
Proxmox pending apt packages and `pve-manager` version, BIOS and BMC firmware versions,
and every component in a BMC's Redfish firmware inventory (NICs, backplanes, CPLDs).
Inventory entries are created disabled, since a server can list dozens; enable the ones
you care about. RouterOS and RouterBOARD firmware can be installed from Home Assistant
or from the add-on's UI — everything else reports only, and says so.

**Backups & storage** — per-guest last-backup time, size and verification state; guests
with no backup at all; last backup job result per node; storage pool usage; ZFS pool
health and fragmentation; SMART health and SSD wear per disk; TLS certificate expiry.

**VM/container inventory** — every guest on the monitored node as its own device with
running state, CPU, memory and disk. Node network interfaces — physical NICs, bridges
and bonds — report link state with their address and bridge members as attributes. In
cluster scope you additionally get quorum and per-node online state.

**Tunnels** — WireGuard interfaces and per-peer state, OpenVPN, L2TP, SSTP and PPTP
clients, and IPsec peers and phase-2 policies. WireGuard has no session to inspect, so a
peer counts as up while it is still handshaking — silence for more than three minutes,
against a two-minute rekey interval, reads as down. Inbound dial-in tunnels are counted
per service rather than given an entity each, which would churn the registry as users
come and go.

**Netwatch** — every enabled `/tool/netwatch` entry becomes a connectivity entity, with
round-trip time and packet loss where the probe is ICMP. A host that is down reports
since when. The comment is used as the name if there is one.

## Power control

Chassis power runs through the standard Redfish `ComputerSystem.Reset` action, the same
path `ipmitool chassis power on/off` takes underneath.

- **`switch.<host>_power`** — on powers the machine up; off performs a graceful OS
  shutdown by default, or cuts power immediately if you set *Power switch off action* to
  "Cut power immediately" for that device in the add-on's UI.
- **Buttons** — Power on, Graceful shutdown, Force off, Graceful/Force restart, Power
  cycle and NMI. Only the actions your BMC advertises are created. Power on and Graceful
  shutdown are enabled by default; the abrupt ones ship **disabled** so they cannot be
  hit by accident — enable them per entity in the entity settings.
- **`switch.<host>_identify_led`** — the chassis identify LED, for finding the box in
  the rack.

The same machinery covers the other two sources: Proxmox guests get a power switch plus
Reboot and Force stop buttons, and MikroTik PoE-out ports get a switch each, so you can
power-cycle anything hanging off a PoE port.

Power state is only known by polling, so these switches use *assumed state*: after a
command the entity shows the requested state and re-polls at 8s and 30s to confirm.

## Per-source setup

### MikroTik

Enable the REST service and create a dedicated user:

```
/ip service enable www-ssl
/user group add name=homeassistant policy=api,read,test
/user add name=homeassistant group=homeassistant password=<secret>
```

Add `write,reboot` to the policy if you want the RouterOS update entity, the reboot
button or PoE switching to work. Leave *Verify SSL certificate* off unless you have
installed a trusted certificate — RouterOS generates a self-signed one.

*Monitor VPN tunnels* and *Monitor netwatch* are on by default and can be turned off per
device. Neither needs extra permissions, and a router without the WireGuard package or
an older RouterOS missing one of these menus simply reports nothing for it.

### MikroTik switches (SwOS and SwOS Lite)

SwOS is the switch firmware on CRS and CSS boxes; it shares nothing with RouterOS but the
vendor, so these are a separate device type. Point an entry at the switch's web address
and give it the same user you log into that web interface with — `admin` and its
password. Both field-naming dialects are handled: SwOS on the CRS/CSS3xx models and
SwOS Lite on the CSS1xx/CSS6xx ones.

There is nothing to enable on the switch. SwOS serves plain HTTP on port 80 and
authenticates with HTTP digest, which is why *Use HTTPS* and *Verify SSL certificate* do
not apply. *Monitor ports*, *Monitor PoE* and *Monitor SFP modules* are on by default and
can be turned off per device; a model without PoE or SFP cages simply reports neither.

**Firmware updates.** SwOS cannot check for its own updates — its web interface asks
MikroTik's server *from your browser*. With *Check for firmware updates* on (the
default), the add-on makes that same request on the deep poll: it reads the product code
out of the switch's own web UI, asks `upgrade.mikrotik.com` what is published for it, and
compares. A switch with no route out simply reports its installed version. Turn the
option off to stop the add-on talking to MikroTik at all.

Everything here is **read-only** apart from the reboot button, which ships disabled.
Changing anything on a SwOS switch means POSTing a whole endpoint's configuration back
to it, and a monitor has no business rewriting a switch's port configuration.

### Proxmox VE

By default an entry monitors **one node**: the node you point it at, its interfaces,
its storages and the guests running on it. Nothing about the wider cluster is reported.
Add one entry per node you care about and each gets its own device tree. An API token is
preferred: it never expires and can be read-only.

The node is detected automatically — Proxmox marks the one answering the request as
`local`. If you connect through a VIP or reverse proxy that can land on any member, set
**Node name** explicitly so the entry always reports the same node.

Set **Monitor** to *The whole cluster* for the older behaviour: one entry discovers every
node, publishes a cluster device carrying quorum and node-count sensors, and hangs each
node and guest beneath it.

In cluster scope you can add a second node as a **standby**, and it is worth doing:
Proxmox answers `/cluster/resources` and `/cluster/status` identically from every node,
so a single entry stops reporting entirely if that one host is down. Add the other nodes
and the add-on elects one to report the cluster; the rest sit in standby and take over
automatically. The cluster is published once either way, so nothing is duplicated.

That group keeps the identity of the **first** node you configured, so a handover does
not recreate the cluster's entities — the history survives the outage that caused it.
The device list marks whichever node is standing by. Node scope needs none of this: each
entry reports only itself, so nothing is deduplicated and nothing elects anything.

1. **Datacenter → Permissions → Users** — add a user, e.g. `monitoring@pve`.
2. **Datacenter → Permissions → API Tokens** — add a token for that user. Either clear
   *Privilege Separation* or give the token its own permissions.
3. **Datacenter → Permissions** — add a permission on path `/` with role **PVEAuditor**,
   and **tick *Propagate***. Without it the role applies to the path `/` literally and
   not to `/nodes/…` or `/vms/…`, so `/cluster/status` answers while the node's own
   status returns 403 and no guest is visible anywhere.
4. **If you left *Privilege Separation* enabled**, the token's rights are the
   *intersection* of the user's and the token's. Granting the token everything achieves
   nothing on paths where the user has no propagating grant, so add the permission on
   both — once as a *User Permission* and once as an *API Token Permission*.

The token ID is the full `user@realm!tokenid` string. For starting and stopping guests,
add **PVEVMAdmin** on `/vms` as well.

> **Grant on `/` and nothing else unless you mean it.** A permission on a deeper path
> *replaces* the one inherited from `/` for that subtree — it does not add to it. Adding
> a row on `/nodes` or `/vms` to "be specific" therefore takes away the audit privileges
> that were reaching them, and the result is a node with no CPU or memory readings and a
> cluster that appears to have no guests. `pveum acl list` shows every row; a subtree
> missing from `/access/permissions` is one that has been overridden this way.

To check what a token can actually see, ask Proxmox rather than reading the ACL table:

```
curl -sk -H "Authorization: PVEAPIToken=user@realm!tokenid=SECRET" \
  https://your-node:8006/api2/json/access/permissions
```

It returns the token's effective permissions per path. If only `/` appears, the grant is
not propagating.

Proxmox intentionally exposes no apt-upgrade endpoint, so its update entity reports
versions but cannot install. Run upgrades from the Proxmox shell. There is a *Refresh
package list* button per node, which is the `apt update` half that Proxmox does expose,
so you can re-check without waiting for the slow tier.

### Supermicro / Redfish

Point it at the **BMC's** IP, not the host OS. Use an IPMI user with at least *Operator*
privilege for power control, or *User* for monitoring only. Leave *Verify SSL certificate*
off for the stock self-signed certificate.

X9 and X10 boards predate Redfish and are not supported. Some X11 boards need recent BMC
firmware for Redfish sensor data.

Supermicro publishes no machine-readable "latest firmware" feed, so BIOS and BMC versions
are reported as diagnostic sensors rather than installable updates.

## Polling

The add-on polls each device on two tiers, set per device in its web UI:

- **Poll interval** (default 60s, 120s for BMCs) — health and telemetry.
- **Deep poll interval** (default 900s) — available updates, SMART and ZFS health, backup
  inventories, firmware and event logs.

The split exists because the deep data is expensive: RouterOS's update check makes the
router perform an outbound HTTP request, the SwOS firmware check makes the add-on make
one, and Proxmox backup inventories mean listing storage content. BMCs are slow and easily overloaded, so keep their interval generous.

The integration separately reads the add-on's cached snapshot every 30s by default. That
call is local and cheap, so it can stay short regardless of the device intervals.

## Security

The add-on's port is **not published to the host**. It is reachable only from other
containers on the Supervisor network, which is how the integration connects. Requests
arriving through Ingress are already authenticated by Home Assistant; everything else
must present the add-on's API token, compared in constant time.

Device credentials are stored in the add-on's `/data` volume and are never returned by
the API — editing a device and leaving a password blank keeps the stored one.

## Entity notes

- Many entities ship **disabled by default** to keep the entity list manageable —
  interface byte counters, individual voltages, swap, IO wait, kernel version and the
  abrupt power actions. Enable what you want per entity.
- `PROBLEM` binary sensors are **on when there is a problem**. An unknown SMART state
  (a disk behind a RAID controller) stays unknown rather than reporting a failure.
- Uptime is exposed as a **last boot timestamp**, smoothed so it does not drift by a
  second on every poll and flood the recorder.
- QEMU guest disk usage is disabled by default because it reads 0 unless the guest agent
  is running.
- SwOS port entities are keyed by port number, not by port name, so renaming a port in
  the switch relabels its entities instead of replacing them. An SFP cage with no module
  in it, or with a direct-attach copper cable, reports no optical diagnostics at all.
- A source the add-on cannot reach goes unavailable rather than freezing on stale values;
  other sources are unaffected. Devices that disappear can be deleted from the device page.
- Adding a device in the add-on's UI creates its entities on the integration's next read.
  No Home Assistant restart is needed.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest
.venv/bin/ruff check custom_components monitorha tests --select F,E9,UP,B
```

Run the add-on outside a container:

```bash
cd monitorha && MONITORHA_DATA=/tmp/monitorha-data python -m app
```

The suite covers each backend's parsing against realistic device payloads, the add-on's
store, HTTP API and auth, and boots a real Home Assistant to check the config flow,
entity creation, the device hierarchy, power actions and failure handling.

### Layout

```
repository.yaml                     add-on repository manifest
monitorha/                          the add-on
  config.yaml  Dockerfile  build.yaml
  app/
    api/                            device backends (no Home Assistant imports)
    manager.py  store.py  server.py  serialize.py  supervisor.py
    web/                            Ingress UI
custom_components/monitorha/        the integration
tests/
```

The backends deliberately import no Home Assistant code — `app/const.py` mirrors the HA
enum names and values so the same source reads identically on both sides, and
`serialize.py` is the only place that knows the wire format.
