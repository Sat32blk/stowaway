<p align="center"><img src="docs/icon.svg" width="112" alt="Stowaway icon"></p>

# Stowaway

[![Docker image](https://github.com/Sat32blk/Stowaway/actions/workflows/docker-image.yml/badge.svg)](https://github.com/Sat32blk/Stowaway/actions/workflows/docker-image.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-support-FFDD00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/sat32blk)

**Stowaway is the middleman between your favorite dashboard or bookmark and your containers' web interfaces.**

Plenty of containers only get used now and then: a photo editor you open once a week, a game server for the weekend, the media tools you reach for occasionally. There's no reason for them to run 24/7, using memory and CPU while nobody's looking. With Stowaway, your link still opens the app as usual. If the app is asleep, Stowaway starts it and shows a short "starting" page, then sends you straight in. Once nobody has used it for a while, Stowaway puts it back to sleep.

It's also great for trying things out. Install as many containers as you like without worrying about them eating up your server's resources: the ones you're not using just sleep.

Everything is managed from a web dashboard that lists every container on your server in one list. Each app's panel shows how it sleeps, whether an update is waiting, its CPU and memory use, and what opened it last. Click **Let Stowaway manage** on the ones it should put to sleep, and the top of the page shows how much memory and CPU it's saving.

![Stowaway dashboard](docs/screenshots/dashboard.png)

- **Wake on demand:** a start page while the app wakes, then straight into the app. Media apps on TVs, API clients and live connections (WebSockets) work too.
- **Never sleeps a busy app:** CPU and network activity count as use. Keep apps awake on demand or during set hours.
- **Resources saved:** see how much memory and CPU sleeping apps are freeing.
- **Updates:** see at a glance which containers have a newer version, install it with one click or automatically (when the app wakes, or at a scheduled restart), and go back to the old one if the new one won't start.
- **Scheduled restarts:** restart (and optionally update) any container daily, weekly or monthly.
- **Companion containers:** helpers like Tdarr's nodes wake and sleep with their app.
- **Status on your dashboard:** In Use / Sleeping in 8 min / Ready to Sleep / Sleeping labels and awake/asleep dots for Homarr, Homepage, Dashy, Glance and Heimdall. Each app's settings can add it to Homarr, Homepage, Glance or Heimdall with one click, including dashboards on macvlan.
- **Fits your setup:** macvlan/ipvlan containers, HTTPS with Let's Encrypt, and a [Home Assistant integration](https://github.com/Sat32blk/Stowaway-homeassistant).
- **Light:** about 40 MB of memory and under 0.1% of one CPU core while idle.

| | |
|---|---|
| ![App settings](docs/screenshots/app-settings.png) | ![Updates and restarts](docs/screenshots/updates.png) |
| ![Dashboards tab](docs/screenshots/dashboards.png) | ![Start page](docs/screenshots/start-page.png) |

```
link / Heimdall / Homarr tile ──► stowaway ──► asleep? ── yes ──► docker start, show "Starting…" page
                                               │                    (reloads by itself when ready)
                                               └── awake ──► pass the request through, reset the idle timer

every 5s: anything idle longer than its timeout, with nothing in progress ──► docker stop
```

## Install

Stowaway runs on any Linux server or NAS with Docker: OpenMediaVault, Synology, Unraid, TrueNAS SCALE, QNAP, UGREEN, Asustor, CasaOS/ZimaOS, or a plain Debian/Ubuntu box, on Intel/AMD (amd64) or 64-bit ARM (arm64). See [NAS platforms](#nas-platforms) for step-by-step notes for each.

**With the ready-made image** (easiest; works in NAS compose screens that can't build images). Paste this as a compose project, changing the `/config` folder to one on your NAS:

```yaml
services:
  stowaway:
    image: ghcr.io/sat32blk/stowaway:latest
    container_name: stowaway
    restart: unless-stopped
    network_mode: host
    # Needed only for macvlan containers: uncomment cap_add/NET_ADMIN
    # and MACVLAN_HELPER_IP (a free address on your network).
    # cap_add:
    #   - NET_ADMIN
    environment:
      - DASHBOARD_PORT=8880
      - TZ=America/New_York
      # - MACVLAN_HELPER_IP=192.168.1.250
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /srv/stowaway:/config
```

**From the source** (clone the repository or unzip a release):

```bash
cd /opt
unzip stowaway.zip        # or: git clone https://github.com/Sat32blk/Stowaway.git stowaway
cd stowaway
docker compose up -d --build
```

Open **http://<server-ip>:8880** from a device on your home network. The first visit asks you to create a username and password; the password needs at least 8 characters including a letter, a number and a special character. After that you sign in with it.

**Forgot your password?** On the server run `docker exec stowaway python -m app.auth reset`, then open the dashboard again to create a new account. Your apps and settings are kept.

Keep the container named `stowaway` (so it doesn't list itself as an app), or set `STOWAWAY_SELF` to the name you use.

## Using it

1. Every container on the server is listed under **Apps on this server**. Click **Let Stowaway manage** on an app.
2. Check the settings and click **Let Stowaway manage it**:
   - **Sleep:** put it to sleep *automatically* after a number of idle minutes, or *only when told* (Home Assistant, the API or the Sleep now button).
   - **Address & opening:** the **link port** is the new address that wakes the app, e.g. `http://192.168.1.2:18096`. Put this in your dashboard or a bookmark. The **app port** is the port the app listens on inside its container; it's filled in for you.
3. The app moves to the top of the list. Its name is now the link that opens (and wakes) it, and the panel has **Wake** / **Sleep now** and **Settings**.

**Settings → Stop managing *app*** hands a container back. Stowaway offers to start it if it's asleep so it runs normally again.

Stowaway never modifies your containers (so OMV's Compose page won't undo anything). The app's own port keeps working while it's awake; only the link port wakes it.

## The app list

One list holds every container: the apps Stowaway manages first, then the rest. **All**, **Managed**, **Not managed** and **Updates** above the list filter it, and the search box finds an app by name, image or compose project. Each app's panel has four columns:

| Column | Shows |
|---|---|
| **Sleep** | How it sleeps: *Sleeps after 10 min* or *Sleeps only when told*, *Awake while busy*, its awake hours, and *Kept awake* or *Won't wake when opened* when those are on. Apps Stowaway doesn't manage say *Not managed, always on*. |
| **Updates** | *Update available* or *Up to date*, and whether auto-update is on (*Auto-update Sun 4:00 AM* at a scheduled restart, *Auto-update when it wakes*, or *Auto-update off*). |
| **Usage** | The app's CPU (100% = one core) and memory right now. |
| **Last opened by** | What opened or woke it last, and when: a device (its network name if your router provides one, otherwise its browser and IP address), Home Assistant, another API client, the Wake button or its awake hours. Not tracked for apps Stowaway doesn't manage, since visits don't pass through it. |

The status label next to the name is the same one dashboards get: **In Use**, **Sleeping in 8 min**, **Ready to Sleep**, **Sleeping** and so on (see [Status labels](#dashboards-homarr-homepage-dashy-glance-heimdall)). Apps with companion containers have an arrow that opens a list of them, each with its own status, CPU and memory.

**Settings** on a panel opens the app's settings window with tabs: **Sleep**, **Staying awake**, **Address & opening**, **Companions**, **Updates & restarts**, **Dashboards** and **Home Assistant**. Apps Stowaway doesn't manage have only **Updates & restarts**, since restarts and updates work for any container.

**System Settings** (top right) holds everything that isn't about one app: start page and time zone, Homarr connection, Home Assistant tokens, the macvlan helper, HTTPS, diagnostics and your account.

## Companion containers

Some apps come with helper containers that are useless on their own, like Tdarr and its `tdarr-node-cpu`, `tdarr-node-intel` and `tdarr-node-nvidia` nodes. Let Stowaway manage the main app only, and in its **Settings → Companions** tick the helpers. Containers from the same compose project are listed first.

- **Waking:** companions start right after the app wakes.
- **Sleeping:** they go to sleep with it.
- **Activity counts:** if a companion is busy (say, a node transcoding), the app stays awake even when its own web interface is idle.
- **In the app list:** companions are listed under their app (click the arrow next to its name) instead of on their own.

## Resources saved

The top of the dashboard shows what sleeping apps save:

- **Memory freed now** and **CPU freed now:** what the apps asleep right now would be using if they were awake and idle, and how much of the server's RAM and CPU that is.
- **CPU time saved** over the last 7 days or all time, also shown as CPU cycles, and **Average memory freed** over the same period.

The note under the tiles also shows what **Stowaway itself** uses, so you can see it's worth it. It's built to be light: about 40 MB of memory and under 0.1% of one CPU core while idle (measured with five apps, three of them awake). It follows Docker's event stream instead of repeatedly asking Docker about every app, checks CPU and network use of awake apps every 15 seconds (and once more right before putting one to sleep), and only loads its HTTPS code when HTTPS is turned on.

These are estimates. A stopped app uses nothing, so Stowaway learns what each app uses while it's awake but idle (not busy, and at least a minute after starting), and counts that as saved for every second the app sleeps. An app shows as *still learning* until it has run idle for a couple of minutes. Figures are kept in `config/savings.json`.

## Start page, ready delay and error page

**System Settings → General** controls what visitors see while a sleeping app starts:

- **Style:** Please Wait, Starting Service, Loading *app name*, Ready in 3, 2, 1 (a countdown based on how long the app took last time), or a **Custom message** where `%name%` becomes the app's name.
- **Ready delay:** seconds to wait after the app answers before opening it. Useful for apps that accept connections a moment before they're fully ready.
- **Preview start page / Preview error page** show them without touching any container.

Each app can override the style and delay under its **Settings → Address & opening**.

If an app fails to start, the start page turns into an error page showing the reason, with **Try again** and **Open dashboard** buttons.

## Updates

**Update checks.** Stowaway asks the image's registry (e.g. Docker Hub) about a newer version of each app it manages once a day, without installing anything, and the app's panel shows **Update available** when there is one. Change how often (or turn it off) under the app's **Settings → Updates & restarts → Update checks**; **Check now** checks straight away. Apps Stowaway doesn't manage can have update checks too: set a number of hours in their Settings.

**Update now** (on the *Update available* banner in that tab) installs it right away: the new version downloads while the app keeps running, then the container is swapped. An app that's asleep is started to check the new version works, then put back to sleep.

**Auto-update** installs new versions without you, in one of two ways:
- **At its scheduled restart** (see *Scheduled restarts* below), with *Auto-update: install the newest version when it restarts* ticked.
- **When it wakes**, described next.

### Auto-update when it wakes

Turn it on per app under **Settings → Updates & restarts → Auto-update when it wakes**. When the app is woken, Stowaway asks the image's registry (e.g. Docker Hub) whether the tag it uses, such as `jellyfin/jellyfin:latest`, now points to a newer version. If so:

1. Visitors see an **Updating** page with a progress bar while the new version downloads.
2. Stowaway swaps the container for one built from the new image, keeping its name, environment, labels, restart policy, ports, volumes (including anonymous ones), networks, IP addresses and MAC address. Settings that came from the old image itself are left to the new image, so its new defaults apply.
3. The page hands over to your chosen start page and then opens the app.

If the new version won't start, Stowaway puts the previous container back, points the image name back at the working version (so a `docker compose up` doesn't pick up the broken one), shows a note on the app's panel, and won't try that version again. **Try again next time** on the panel clears that.

- **At most every** (default 24 hours) limits how often the registry is asked when it wakes; 0 checks on every wake.
- If the registry can't be reached, the app just starts on its current version.
- Updates on wake only happen while an app is asleep, never while it's running. (Scheduled restarts and **Update now** can also update apps that are running.)
- Pinned images (`image@sha256:…`) and images built on the server are never updated.
- **Private registries:** uncomment the `config.json` line in `docker-compose.yml` so Stowaway can use the server's `docker login`.
- **Be choosy:** `:latest` can bring big version jumps. For apps where that matters, use a tag like `jellyfin/jellyfin:10.10` so updates stay within that version line, or leave updating off.
- Docker Hub limits anonymous downloads (checks don't count), so daily checks are fine.

## Scheduled restarts

Any container, including ones Stowaway doesn't put to sleep, can be restarted on a schedule. Open its **Settings → Updates & restarts → Scheduled restart**:

- **Restart:** every day, every week (pick the day) or every month (pick a date from the 1st to the 28th, or *Last day of the month*), at a time you choose. Monthly is the longest interval. Times use the time zone in **System Settings → General**.
- **Auto-update: install the newest version when it restarts:** uses the same update process as *Auto-update when it wakes*. The new version downloads while the container keeps running, so it's only down for the swap. Settings, volumes, networks and IP addresses are kept. If the new version doesn't stay up, the previous version is put back and that version isn't tried again (**Allow that version next time** clears this).
- **If it's busy, wait up to** (default 6 hours): if the container is busy at the scheduled time (CPU or network above the limits in System Settings, or someone using it through Stowaway), Stowaway checks again every 10 minutes. If it's still busy when the time runs out, that run is skipped until next time. Set it to 0 to restart regardless.

After a restart, Stowaway checks that the container **stays up**. If the container has a Docker health check, it waits up to 5 minutes for *healthy*. Otherwise the container must keep running for 20 seconds without crashing or restarting itself.

The tab shows the next run and the result of the last one: **Restarted**, **Updated**, **Put back**, **Skipped**, **Missed** or **Failed**, with the reason. **Restart now** does it straight away, skipping the busy wait. On the app list, an app Stowaway doesn't manage shows *Restarts Sun 4:30 AM* under Sleep.

Good to know:

- Jobs run one at a time.
- A container that isn't running is left alone. The exception is an app asleep under Stowaway with auto-update on: it's updated, started to check that the new version works, then put back to sleep.
- If Stowaway wasn't running at the scheduled time (for example, the server was off), the job still runs when Stowaway comes back, as long as that's within the busy wait (at least 1 hour). Later than that, it's recorded as **Missed** and waits for the next scheduled time.
- A new schedule's first run is the next matching time, not right away.
- Stowaway can't restart itself, so it isn't listed.
- Schedules and results are saved in `config/config.yaml`.

## HTTPS

Turn it on in **System Settings → HTTPS & access**. Stowaway then serves every enabled app over HTTPS on one port (default 8443), each under its own name: with the domain `myhome.duckdns.org`, Jellyfin is at `https://jellyfin.myhome.duckdns.org:8443` and the dashboard at `https://stowaway.myhome.duckdns.org:8443`. The plain HTTP link ports keep working alongside.

Choose where the certificate comes from:

| Certificate | What you need | Notes |
|---|---|---|
| **Self-signed** | Nothing | Works immediately. Browsers warn the first time and some TV apps refuse it. |
| **Let's Encrypt with DuckDNS** | A free name at duckdns.org and its token | Wildcard certificate, renewed automatically. No ports need opening for the certificate. |
| **Let's Encrypt with Cloudflare** | Your own domain on Cloudflare and an API token (Edit zone DNS template) | Wildcard certificate, renewed automatically. No ports need opening for the certificate. |
| **Let's Encrypt with port 80 forwarded** | Any domain; router forwards internet port 80 to Stowaway's HTTP check port | One certificate listing each app's name, updated when you enable apps. |
| **My own certificate files** | `fullchain.pem` and `privkey.pem` in `config/certs/custom/` | You handle renewal. |

Click **Save and get certificate** and watch the status box. Tick **Test certificates** while experimenting; Let's Encrypt limits how many real certificates you can request per week.

### DuckDNS step by step

1. Sign in at [duckdns.org](https://www.duckdns.org), add a name such as `myhome`, and make sure it shows your home's public IP.
2. In Stowaway's System Settings → HTTPS & access: tick **Serve apps over HTTPS**, domain `myhome.duckdns.org`, certificate **Let's Encrypt with DuckDNS**, paste the token from the top of the DuckDNS page, and click **Save and get certificate**. It takes about a minute.
3. **From outside your home:** forward a port on your router to the server's HTTPS port. Using the same number on both sides (e.g. 8443 → 192.168.1.2:8443) keeps the links in the dashboard correct everywhere. If port 443 is free on the server, set the HTTPS port to 443 and forward 443 → 443 for links without a port.
4. **At home:** `*.myhome.duckdns.org` points to your public IP, which works if your router supports "NAT loopback". If it doesn't, add a local DNS entry (router, Pi-hole or AdGuard) pointing `*.myhome.duckdns.org` to `192.168.1.2`.

### Opening Stowaway to the internet

- The dashboard only answers on your home network by default (**Only allow this dashboard from my home network**). Your apps' links work from anywhere; the controls don't.
- After 10 wrong passwords in 10 minutes, an address is blocked for 10 minutes.
- Anyone who can reach an app's link can wake it. That's the point, but it's worth knowing.
- **Check each app's own login before exposing it.** See *Security* below.

## Busy apps, Keep awake and awake hours

**Busy detection.** Before putting an app to sleep, Stowaway checks its CPU and network use every few seconds. While either is above the threshold (default 5% CPU or 50 KB/s), the app counts as busy and its countdown restarts. This covers transcoding, library scans, downloads, and people using the app through its own port instead of the link. The app shows **In Use** while it's busy. Change the defaults in **System Settings → General**, or per app under **Settings → Staying awake → Awake while busy** (untick it to ignore activity for that app). CPU is measured like `docker stats`: 100% = one full core. Network use can't be measured for containers on the host network.

**Keep awake.** **Settings → Sleep → Keep awake** holds the app awake for 1, 4 or 12 hours, or until you release it (**Release** on its panel or in that tab). If it's asleep, it's woken. This survives Stowaway restarts.

**Awake hours.** Under **Settings → Staying awake → Awake hours**, pick days and a time range (e.g. Mon–Fri 18:00–23:00; ranges past midnight work). Stowaway starts the app when the range begins and keeps it awake until it ends, then the normal idle countdown takes over. Times use the time zone in **System Settings → General**.

**Sleep now** always wins: it ends a Keep awake and skips the rest of the current awake hours.

## Media apps on TVs and phones

Point the app at the link port, e.g. `http://192.168.1.2:18096` for Jellyfin. The first connection to a sleeping server may time out while it starts; retrying works. Awake hours avoid that by having the server up before you sit down. Playback keeps the server awake, and live features that use WebSockets (remote control, SyncPlay) work through the link.

Plex isn't a good fit: its apps find the server through plex.tv, which only knows about it while it's running, so a sleeping Plex server is never woken. Leave Plex always on.

## Containers on macvlan networks

Linux blocks a server from talking to its own macvlan (and ipvlan) containers, so Stowaway needs a way to reach them.

**If the server can already reach them**, there's nothing to do: Stowaway notices and uses that path. That's the case with Unraid's *Settings → Docker → Host access to custom networks*, or if you followed a guide that added a macvlan "shim" interface on the host.

Otherwise Stowaway sets up a small helper by itself; you only pick its address:

1. Make sure `cap_add: - NET_ADMIN` is uncommented in `docker-compose.yml` (it's commented out by default, since only macvlan setups need it), then run `docker compose up -d`.
2. Open **System Settings → Network** and fill in **Macvlan helper IP**: a free address on your network, outside your router's DHCP range and not used by any device or container (e.g. `192.168.1.250`). Or set it in `docker-compose.yml` with `MACVLAN_HELPER_IP=192.168.1.250`, which then takes precedence and locks the field.
3. Save. Any managed macvlan app with a warning on its panel should clear.

The helper is a macvlan interface (ipvlan for ipvlan networks) named `sw-<network card>` with a route to each running macvlan container's IP. That includes containers Stowaway doesn't manage, so a dashboard on macvlan (Homarr, Homepage…) can reach Stowaway at the helper address for its status checks. Stowaway sets the helper and the network card it sits on to answer ARP only for their own addresses (`arp_ignore=1`, `arp_announce=2`); without that, the helper would also answer for the server's IP, which security software such as ESET reports as ARP spoofing. It's removed when the server reboots and rebuilt when Stowaway starts. This is why macvlan setups need `cap_add: NET_ADMIN` in the compose file.

A macvlan app's own address (e.g. `http://192.168.1.60:8443`) keeps working while it's awake, but only the link port wakes it.

## NAS platforms

Stowaway only needs standard Docker features: host networking and the Docker socket, plus the `NET_ADMIN` capability if you have macvlan containers. It asks Docker which API version it speaks, so it works with older engines like Synology's Docker 24 as well as Docker 29.

| Platform | Status | Install with |
|---|---|---|
| OpenMediaVault 7 | Tested (developed on it) | Compose plugin, or the command line |
| Plain Debian / Ubuntu | Tested on Docker 29, including the older API levels of Docker 20.10 and 24 | Command line |
| Raspberry Pi OS 64-bit | Expected to work (arm64 image) | Command line |
| Synology DSM 7.2+ | Expected to work | Container Manager → Project |
| Unraid 6.12 / 7.x | Expected to work | Template in `unraid/stowaway.xml` |
| TrueNAS SCALE 24.10+ | Expected to work, with limits | Apps → Custom App → Install via YAML |
| UGREEN UGOS Pro | Expected to work | Docker → Project |
| QNAP QTS / QuTS hero | Expected to work, least checked | Container Station → Applications |
| Asustor ADM | Expected to work (x86 models) | Docker Engine app, or Portainer |
| CasaOS / ZimaOS | Expected to work | Custom install → Import Docker Compose |
| Podman, Docker Desktop, 32-bit ARM | Not supported | |

*Expected to work* means the platform's Docker version and compose support match what Stowaway needs and the API versions were tested, but it hasn't been run on that NAS yet. Reports are welcome.

In every case, use the ready-made image compose from [Install](#install) with the folders below. Only `/config` needs a real folder; create it first.

**Synology (Container Manager).** Create `docker/stowaway` in File Station, then Container Manager → Project → Create, set the path to that folder, and paste the compose with `- /volume1/docker/stowaway:/config`. DSM uses ports 5000/5001 and 80/443, so keep the dashboard on 8880. Synology's macvlan networks must be created over SSH. If Virtual Machine Manager is installed, the network card is called `ovs_eth0`, and the macvlan network must use that as its parent. Works on Plus (Intel/AMD) and on ARM j/value models that have Container Manager.

**Unraid.** Unraid doesn't use compose files by default. Copy `unraid/stowaway.xml` to `/boot/config/plugins/dockerMan/templates-user/my-stowaway.xml`, then Docker → Add Container → pick *stowaway*. Settings go in `/mnt/user/appdata/stowaway`. Custom networks default to **ipvlan** on Unraid. If *Host access to custom networks* is on, Stowaway uses it and needs no helper IP; otherwise set a helper IP (see macvlan above). With the Compose Manager plugin you can paste the compose instead.

**TrueNAS SCALE.** Create a dataset such as `apps/stowaway`, then Apps → Discover Apps → Custom App → **Install via YAML** and paste the compose with `- /mnt/<pool>/apps/stowaway:/config`. Apps installed from the TrueNAS catalog (containers named `ix-<app>-…`) are marked *TrueNAS app*. Stowaway can put them to sleep, wake them and restart them on a schedule, but **leaves updates to TrueNAS**, because TrueNAS recreates those containers itself. TrueNAS may show a sleeping app as *Stopped* or *Crashed*; that's expected. The web UI uses 80/443.

**UGREEN (UGOS Pro).** Docker → Project → Create, config folder e.g. `/volume1/docker/stowaway`. The NAS web UI uses 9999/9443, so there's no clash with 8880/8443.

**QNAP (Container Station 3).** Applications → Create, paste the compose with e.g. `- /share/Container/stowaway:/config`. QTS uses 8080 and 443. Containers given their own LAN address with QNAP's *qnet* driver are reached like normal containers and don't need the macvlan helper.

**Asustor.** Install *Docker Engine* from App Central (Intel/AMD models), then use Portainer (Stacks → Add stack) or the command line. ADM uses 8000/8001.

**CasaOS / ZimaOS.** App Store → Custom Install → Import, paste the compose with e.g. `- /DATA/AppData/stowaway:/config`. After importing, check that *Network* is `host` (and, for macvlan containers, that `NET_ADMIN` is listed); add them back if the importer dropped them. The dashboard uses port 80.

**Not supported:**
- **Podman.** Starting and stopping probably works, but it can't check registries for new versions.
- **Docker Desktop** (Windows/Mac). Containers run inside a hidden virtual machine, so host networking and the macvlan helper don't work like they do on a server.
- **32-bit ARM** (Raspberry Pi OS 32-bit, old ARM NAS models). Docker 29 dropped it, and no image is built for it.

**Ports to avoid** when choosing link ports or the HTTPS port: whatever your NAS's own web UI uses (80/443 on most; 5000/5001 Synology; 8080 QNAP; 8000/8001 Asustor; 9999/9443 UGREEN). Stowaway checks that a port is free before using it.

## Dashboards: Homarr, Homepage, Dashy, Glance, Heimdall

Use each app's link port as the tile's URL; clicking it wakes the app. An app's **Settings → Dashboards** tab adds it to your dashboard with one click, or shows the ready-made settings to copy (**Set it up by hand**). In short:

| Address | What it gives |
|---|---|
| `http://<server>:8880/_stowaway/status/<app>` | JSON: `state`, the status label (`indicator`, see below) with its colour and icon, a one-line `summary`, `sleeps_at`/`sleeps_in`, keep-awake, "don't wake" and next/last scheduled restart. Never wakes the app. |
| `…/status/<app>?code=1` | The same, but **HTTP 503 while the app is asleep**, so a dashboard's status dot shows green when awake and red when asleep. |
| `…/_stowaway/status` | Every app in one list (dashboard port only, home network only by default). |
| `…/_stowaway/summary` | Stowaway itself: apps awake and asleep, memory and CPU freed, CPU time saved and updates waiting (dashboard port, home network). Always 200 while Stowaway runs. |

**Status labels.** Dashboards that can show text get one clear label per app:

| Label | Colour | When |
|---|---|---|
| **In Use** | green | someone is connected, or the app is busy (a transcode, a download) |
| **Sleeping in 8 min** | yellow | awake and idle; counts down to sleep |
| **Ready to Sleep** | orange | awake with nothing to do: an "Only when I say so" app, or the timer has just run out |
| **Sleeping** | grey | asleep; opening it wakes it |
| *Waking up*, *Going to sleep*, *Updating* | blue | for the moments in between |
| *Kept awake* | teal | you asked Stowaway to keep it awake (or it's within its awake hours) |
| *Failed to start* | red | the last wake didn't work |

The status JSON carries it as `indicator`, `indicator_color` (a colour name), `indicator_hex` and `indicator_icon` (a Tabler icon name), so any dashboard or script can show the same thing.

- **Homarr:** the app tile's *Ping URL* is the `?code=1` address (green/red dot). For the label, import the **Stowaway status** custom widget once (**Download widget** in an app's Dashboards tab or in System Settings → Dashboards, then *Manage → Custom widgets → Import*), add it to a board and set **App name**. It shows the label in colour, with a bar counting down to sleep.
- **Homepage:** `siteMonitor` with the `?code=1` address (green/red), plus a `customapi` widget showing the label. It refreshes every 15 seconds, so the countdown keeps up.
- **Dashy:** `statusCheckUrl` with the `?code=1` address. Dashy can only show a dot: green while awake, red while asleep.
- **Glance:** a `monitor` widget with `check-url` (green/red), and a `custom-api` widget listing every app with its label in colour. Glance updates it when the page is loaded.
- **Heimdall:** has no status dot for ordinary tiles. The **Stowaway** tile type shows the label in colour instead and refreshes itself. In an app's **Settings → Dashboards**, **Add to Heimdall** creates the tile with the app's own icon (from the [dashboard-icons](https://github.com/homarr-labs/dashboard-icons) collection), its Stowaway link and the status switched on; if you already have a tile for the app, that one is converted and keeps its title and icon. Works with the linuxserver Heimdall image; by hand, see `heimdall/README.md`. Clicking **Add to Heimdall** (or **Update tile**) also updates the tile type in Heimdall after a Stowaway update. Next to the button, pick which category (or folder or tag) the tile goes in. If Heimdall is set to show tags as categories, it only shows tiles that are in a category, so Stowaway picks one for you and doesn't offer Home dashboard there.

**Let Stowaway set it up.** Nothing changes in a dashboard until you click its button in an app's **Settings → Dashboards**. The tab also shows which dashboards the app is already on.

- **Homarr:** save Homarr's address and an API key once under **System Settings → Dashboards** (create the key in Homarr under *Manage → Tools → API* as an admin; Homarr 1.0+). Then click **Add to Homarr** in an app's Dashboards tab. If Homarr already has that app, it's updated instead: its name and icon are kept, and its link and Ping URL are set. New apps land in Homarr's app list; place them on a board with an App widget. The key stays on the Stowaway server and isn't shown again or included in diagnostic reports.
- **Homepage:** writes a *Stowaway* group with the apps you've added to `services.yaml`, between two marker comments. Adding another app rewrites the group with it included. Only that block is ever rewritten; the rest of the file stays as it is, and the first time your original is saved as `services.yaml.before-stowaway`.
- **Glance:** writes `stowaway.yml` next to `glance.yml` (a status monitor for the apps you've added plus a list of all apps). Add it to a page once with `- $include: stowaway.yml` in a column's `widgets:`; after that Glance reloads it by itself.

### Stowaway itself on your dashboard

Besides each app's status, your dashboard can show a tile for **Stowaway itself**: how many apps are awake and asleep, and how much memory and CPU sleeping them frees. Set it up in **System Settings → Dashboards → Stowaway on your dashboard**:

![Stowaway on your dashboard](docs/screenshots/stowaway-tile.png)

- **What the tile shows:** apps awake and asleep, memory freed and CPU freed are on by default; CPU time saved this week and updates waiting can be added. A preview shows the result.
- **Homarr:** **Add to Homarr** adds a Stowaway app that opens the dashboard, with a green dot while Stowaway runs. For the numbers, **Download summary widget** and import it once (*Manage → Custom widgets → Import*), then add *Stowaway summary* to a board and pick **Small** (2×2) or **Wide** (4×2) in its settings.
- **Heimdall:** **Add to Heimdall** adds a Stowaway tile showing three of the numbers (e.g. *Awake 3 of 9 · Freed 5.1 GB · CPU 0.1%*). It uses the same Stowaway tile type as your apps.
- **Homepage:** adds Stowaway to the *Stowaway* group in `services.yaml`, with Awake / Asleep / Mem freed / CPU freed boxes (Homepage shows up to four). After changing the fields, click **Update Homepage**.
- **Glance:** adds a Stowaway widget to `stowaway.yml`, above the list of apps.
- **Dashy and others:** Dashy gets a green dot from the summary address; anything else can read its JSON.

Homarr, Heimdall and Glance follow the field choices by themselves; adding an app to Homepage or Glance later keeps the Stowaway tile.

**Dashboard on a macvlan network?** It can't reach the server's own address, so its status checks fail ("fetch failed"). Use the macvlan helper IP instead, e.g. `http://192.168.1.60:8880/_stowaway/status/<app>?code=1`. Stowaway gives every container on a macvlan network a route through the helper once a helper IP is set, and the Dashboards tab fills in the helper address for you when it sees your dashboard on macvlan. Tile links you click keep using the usual address.

Containers that only have a restart schedule can be shown too: their status address gives running/stopped and the next restart.

If a dashboard pings the app's link directly instead, add its user agent (e.g. `Homarr`) or IP address under **System Settings → Dashboards → Dashboard status checks**. Those requests never wake an app or reset its timer.

## Home Assistant

**Apps only Home Assistant should control.** In the app's **Settings → Sleep**, choose *Only when told*. Stowaway then never puts it to sleep by itself: Home Assistant (for example when nobody is home), the API, a scheduled restart or **Sleep now** decide. The link port is optional in this mode; without one, the app is woken from Home Assistant or the dashboard rather than by opening a link.

The **Stowaway** integration for Home Assistant lives in its own repository (`stowaway-homeassistant`), installed through HACS. Each app gets:
- an **Awake** switch;
- a **Don't wake** switch;
- **Status**, **Sleeps at** and **Next restart** sensors;
- **Maintenance running** and **Update ready** sensors;
- **Restart now** and **Keep awake 1 hour** buttons.

It also adds actions to keep an app awake or put it to sleep (optionally switching waking off).

Setup: **System Settings → Home Assistant → Create token** in Stowaway, then add the integration in Home Assistant with Stowaway's address and the token. Each app's **Settings → Home Assistant** tab lists what Home Assistant gets for it, and has copy-paste REST switch/sensor configuration for use without HACS.

**Away from home.** An automation can put media servers to sleep when everyone leaves (`zone.home` drops to 0). You then choose what happens if someone connects while you're away:
- **Sleep only:** Stowaway wakes the app as usual, so remote streaming still works.
- **Sleep and switch waking off:** visitors see "*app* is switched off" until it's switched back on, for example by an automation when someone comes home.

## Switched off ("don't wake")

An app can be switched off: it sleeps, and visitors can't wake it.
- **What visitors see:** browsers get a "switched off" page that opens the app by itself once it's switched back on. Apps and API clients get HTTP 503.
- **What else is paused:** awake hours and scheduled updates of a sleeping app are paused too.
- **How to switch it off:** from Home Assistant, the API, or tick **Don't wake it when its link is opened** in the app's **Settings → Sleep**. The panel then shows *Won't wake when opened*.
- **How to switch it back on:** untick it again. **Wake** and **Keep awake** in the dashboard also switch it back on.

## API

Home Assistant and scripts use API tokens (**System Settings → Home Assistant → API tokens**). Requests send `Authorization: Bearer <token>`.

A token can see and control apps, but can't change settings, the account or tokens. It works only from the home network unless the dashboard is allowed from outside. Tokens are stored hashed in `config/tokens.json`, and resetting the password doesn't remove them; revoke them in the dashboard.

| Request | |
|---|---|
| `GET /_stowaway/api/v1/info` | Version, apps awake/asleep, memory freed |
| `GET /_stowaway/api/v1/apps` | Every app and scheduled container, with full status |
| `GET /_stowaway/api/v1/apps/<app>` | One app |
| `POST …/apps/<app>/wake` | Wake it (also switches waking back on) |
| `POST …/apps/<app>/sleep` `{"block": true}` | Put it to sleep; `block` also switches waking off |
| `POST …/apps/<app>/block` `{"on": true}` | Switch waking off (`false`: back on) |
| `POST …/apps/<app>/power` `{"on": false}` | Wake or sleep in one endpoint (for Home Assistant's REST switch) |
| `POST …/apps/<app>/keep-awake` `{"minutes": 60}` | Keep awake; `{"forever": true}`, or `{}` to release |
| `POST …/apps/<app>/restart` | Restart it now (with its update setting) |

## Behavior notes

- Browsers opening a sleeping app get a "Starting…" page that refreshes every 2 seconds.
- API clients and non-GET requests wait until the app is ready (up to 60s), then go through.
- An app is never stopped while a request is still in progress, so long downloads aren't cut off.
- **Your browser stays on Stowaway's address** (the link) while you use an app. That's how Stowaway sees the app is in use, and it's why the link can wake the app later. If an app redirects to its own IP and port, Stowaway points the redirect back at the link.
- **Go to the app's own address** (Settings → Address & opening → When opened) is available for apps that misbehave behind Stowaway: after waking, browsers are sent to the app's own IP and port, while apps and API clients keep using the link. Stowaway then only knows the app is in use from its CPU and network activity, so keep *Stay awake while busy* on. It needs an address the browser can reach: a macvlan IP, a published port, or host networking.
- **Live connections (WebSockets) are passed through**, so browser-based desktops (HandBrake and other noVNC apps), Home Assistant and code-server work through the link. An open live connection counts as the app being in use, so leaving such a tab open keeps the app awake; close the tab (or click **Sleep now**) to let it sleep.
- Starting or stopping a container outside Stowaway (Portainer, the command line, your NAS's Docker page) shows up in the dashboard right away.
- After a server reboot, apps that Docker starts on its own are put back to sleep once their idle time passes.
- Stop after idle `0` means start on demand but never stop.

## Diagnostics and reporting problems

**System Settings → Diagnostics** helps when something isn't working:

- **Detailed logging** records each decision Stowaway makes: every request and whether it woke an app, Docker events, CPU/network samples, and network-helper commands. It switches itself off after 24 hours. Turn it on, make the problem happen again, then get the report.
- **View report / Download report** gives a snapshot: Stowaway and Docker versions, your NAS's system, settings, each app's state and recent errors, scheduled maintenance results, containers and networks, the macvlan helper, the certificate status and the recent log. The download is a zip with the report and the log files.
- **Report a problem on GitHub** opens a new issue with the versions filled in; attach the downloaded report.

Privacy:
- **Nothing is sent anywhere by Stowaway.** You see the report first and decide whether to share it.
- **Passwords, tokens, API keys** (for example `?apikey=` in a link, or a Plex token) and session cookies are removed from every log line and every report.
- **With "Hide my domain, email, username and public IP" ticked** (the default), those are replaced too. Home-network addresses like `192.168.1.60` stay, because they're often what a problem is about.
- **Container names and images are included.** Look the report over before posting it publicly.

Logs are also kept in `config/logs/` (up to about 3 MB, readable only by root), so they survive restarts. `docker logs stowaway` shows the same lines. Setting `LOG_LEVEL=DEBUG` in `docker-compose.yml` keeps detailed logging on permanently.

## Settings (environment variables in docker-compose.yml)

| Variable | Default | |
|---|---|---|
| `ALLOWED_HOSTS` | empty | Extra names the dashboard may be opened by, comma separated |
| `DASHBOARD_PORT` | `8880` | Dashboard port |
| `REAP_INTERVAL` | `5` | Seconds between idle checks |
| `STOWAWAY_DEMO` | unset | `1` runs with fake containers, no Docker needed |
| `STOWAWAY_SELF` | `stowaway` | Stowaway's own container name, so it doesn't list itself |
| `LOG_LEVEL` | `INFO` | `DEBUG` keeps detailed logging on permanently |
| `MACVLAN_HELPER_IP` | empty | Address for the macvlan helper; overrides System Settings → Network |
| `STATS_INTERVAL` | `15` | Seconds between CPU/network samples of awake apps (busy detection) |
| `USAGE_INTERVAL` | `20` | Seconds between CPU/memory samples of the other running containers, for the app list |
| `DOCKER_HOST` | local socket | Another way to reach Docker, e.g. `tcp://socket-proxy:2375` |

## Security

What Stowaway does to protect you:

- **Sign-in required.** You create the account on first visit (only possible from your home network). Passwords need 8+ characters with a letter, a number and a special character, and are stored as scrypt hashes in `config/auth.json`, never in plain text. Sessions last 30 days in an HttpOnly, SameSite=Strict cookie; changing the password signs out every other device.
- **Home network only** by default (see *Opening Stowaway to the internet*).
- **Wrong-password lockout:** 10 tries in 10 minutes blocks that address for 10 minutes.
- **Other websites can't use your dashboard:** changes must come from the dashboard page itself (CSRF protection), it can't be embedded in other pages (clickjacking), and it only answers to IP addresses, single-word names (like `omv`) and local names (`.local`, `.lan`, `.home.arpa`…), which blocks DNS-rebinding attacks. To use another name, add it to `ALLOWED_HOSTS` in `docker-compose.yml`, e.g. `- ALLOWED_HOSTS=nas.example.com`.
- **The controls only exist on the dashboard port** (and `stowaway.<domain>` over HTTPS), not on your apps' link ports.
- **Visitors can't fake their address:** Stowaway replaces any `X-Forwarded-For` / `X-Real-IP` a visitor sends with their real address.
- **Your secrets are kept private:** `config/config.yaml` and `config/auth.json` are readable only by root, tokens never appear in the dashboard's certificate logs, and Stowaway's session cookie is never passed on to your apps.

What you should know:

- **Stowaway controls Docker, which means it controls the server.** Anyone who gets into the dashboard can start and stop any container. Treat the password like the server's root password.
- **Apps see Stowaway as the visitor's address unless you tell them otherwise.** Some apps skip their login for "local" visitors, like Sonarr/Radarr's *Authentication: Disabled for Local Addresses*. Through Stowaway, internet visitors can look local to them. Before opening an app to the internet, set its authentication to always required, or tell the app to trust Stowaway as a proxy so it reads the real address (in Jellyfin: *Dashboard → Networking → Known proxies*, add `127.0.0.1` and your server's IP).
- **The plain-HTTP dashboard (port 8880) sends your password and session unencrypted** across your home network. With HTTPS on, use `https://stowaway.<domain>` instead.
- **If you put Stowaway behind another reverse proxy**, every visitor appears to come from that proxy, so the home-network-only check and the lockout can't tell visitors apart.

## Limits in this version

- **Security:** Stowaway has full control of Docker. Keep a strong password, and leave the dashboard limited to your home network unless you need it remotely.

## Support

If Stowaway saves you some resources (or some hassle), you can buy me a coffee. Thank you!

<a href="https://buymeacoffee.com/sat32blk"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me a Coffee" height="60"></a>

## License

MIT. See [LICENSE](LICENSE).
