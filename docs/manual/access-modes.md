<!-- verified-against-UI: 2026-07-20 | routes: setup.sh (terminal chooser), /auth/verify, /settings?section=account -->

# Choose how to access JARVIS

`./setup.sh` asks where you want to use JARVIS. The choice controls which devices can reach it and whether sign-in and passkeys are safe to use.

Setup writes the matching JARVIS configuration. It does not silently create external accounts, change DNS, open router ports, or alter a firewall.

## Access choices

| Choice | Use it when | Address | Sign-in and passkeys |
|---|---|---|---|
| **1. This computer only** | You use JARVIS on its host | `http://localhost:3001` | Yes |
| **2. LAN diagnostics** | You need to check reachability from the same network | `http://<lan-ip>:3001/health/jarvis` | No; this address serves only the health check |
| **3. Private Tailscale network** | Family or team devices should have private, authenticated access | A named `https://…ts.net` address | Yes |
| **4. Cloudflare Tunnel** | You need a protected internet route without opening router ports | Your configured HTTPS hostname | Yes |
| **5. Own domain with Let's Encrypt** | Your domain points to this host and port 443 is reachable | Your HTTPS domain | Yes |

For most installations, start with choice 1. Choose 3 when other trusted devices need to sign in. Choices 4 and 5 publish a route beyond the local network and require more operator setup.

## What setup handles

For every choice, setup configures the repository-owned services, allowed hosts,
browser origins, and displayed links. It reports a route as ready only after
the route-specific check passes.

Some work remains outside JARVIS:

- Tailscale account creation and browser sign-in.
- Cloudflare account, tunnel, hostname, and access-policy setup.
- Domain purchase, DNS, router, and firewall changes.

Setup explains these requirements at the relevant choice. On supported Linux
hosts it previews the official Tailscale package commands and asks before
running them. It can also ask before `sudo tailscale up`. If installation or
sign-in is unavailable, declined, or fails, setup keeps localhost working.

## Choice 1: this computer only

Open `http://localhost:3001` on the machine running JARVIS. Setup and everyday sign-in work there because browsers treat localhost as a secure context.

This is also the recovery route if a remote access method stops working.

## Choice 2: LAN diagnostics

Setup prints the host's private network health address, for example
`http://10.0.0.17:3001/health/jarvis`, and probes it. A successful local probe
shows only that the listener answered on the JARVIS host. Test the same address
from another LAN device to check VM networking and firewall access.

That is the only page available to another device on the raw LAN route. Every
other path returns HTTP 403, including the dashboard, setup, sign-in, cookies,
and passkeys. Removing the `s` from an HTTPS dashboard link therefore does not
open the app.

If JARVIS runs in a VM, the address belongs to the VM. Other devices can reach it only when the VM network and host firewall allow that connection.

## Choice 3: private Tailscale network

Install Tailscale on each family device. On the JARVIS host, setup:

1. Uses an existing client or, on supported Linux hosts, previews the package
   commands and asks before installing it. Non-interactive installation requires
   `--install-prereqs`.
2. Detects whether the client is signed in. If not, interactive setup can ask
   permission to run `sudo tailscale up` so you can complete Tailscale's browser
   sign-in.
3. Configures persistent Tailscale Serve to forward HTTPS to the host-only
   JARVIS listener at `127.0.0.1:3003`, preserving a configured custom port.
4. Verifies the exact JARVIS health marker. Setup prints a remote setup link only
   after that check passes; otherwise it keeps the usable localhost link and
   tells you what to retry.

Automatic client installation covers Debian, Ubuntu, Linux Mint, Pop!_OS, and
Fedora hosts with systemd. macOS, unsupported distributions, and WSL without
systemd receive manual instructions. Any missing, declined, or failed step falls
back to localhost without exposing the dashboard on the LAN. Setup exits
nonzero until the selected Tailscale address passes its health check.

Run `./setup.sh --tailscale` to select this choice without the access chooser.
On an existing installation, also accept the overwrite prompt or add
`--overwrite-env` so the route actually changes.

`jarvis-research doctor` and update summaries inspect Tailscale Serve without
changing it. If the complete node-wide configuration is exactly one HTTPS 443
route to the pre-v1.2.0 raw listener, they print a targeted retarget command.
Run that command only after confirming the route belongs to this installation.
Shared, unreadable, or custom Serve state remains untouched; JARVIS never uses
`serve reset` as an upgrade repair.

`--public-origin https://host.example` remains the advanced route for an HTTPS
proxy you manage yourself. Setup does not configure that proxy; it checks the
exact JARVIS health marker and exits nonzero until the address works.

## Choice 4: Cloudflare Tunnel

You provide the Cloudflare tunnel token and HTTPS hostname after creating the
account and tunnel. Protect the main hostname with an Access **Allow** policy.
Then create a second, more-specific Access application for exactly
`your-hostname/health/jarvis` with **Bypass / Everyone**. Cloudflare evaluates
the specific path first. Bypass removes Access enforcement and logging for that
path, so do not widen it: the endpoint is safe to expose only because it returns
the fixed text `jarvis-rd-assistant` and no user, setup-token, or system details.
See Cloudflare's [application-path precedence](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/)
and [Bypass policy](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/common-policies/).
The token is not echoed while you paste it. Setup first checks that the tunnel
has an active Cloudflare connection, then checks the public hostname for the
exact marker.

For unattended setup, pass `--profile=tunnel`, `--tunnel-ack`,
`--tunnel-hostname`, and `--tunnel-token-file`. The token itself stays out of the
command line.

A Cloudflare Access page means the exact health-path Bypass application is
missing. A web-application firewall challenge means only that path still needs a
narrow challenge exception. Neither proves that the tunnel reaches JARVIS. A
wrong marker means the hostname points to another application; DNS and TLS
failures are reported separately. Setup does not print a remote setup link and
exits nonzero until the exact marker is verified.

## Choice 5: own domain with Let's Encrypt

You provide a domain and certificate email. Before running setup, point the domain at the host and make port 443 reachable. Setup waits for the selected HTTPS route before advertising it.

If you do not control DNS and router access, use Tailscale or Cloudflare instead.

## Sign-in links and passkeys

Email is optional. A signed-in administrator can create a 24-hour invite link or a 15-minute sign-in link and share it through a private channel.

Passkeys work on localhost and on the final named HTTPS address. Register them only after opening that final address. They do not work on a raw numeric LAN address.

Keep at least two recovery methods for the administrator account: another
passkey, working email, another signed-in administrator, or the instance
owner's operations API key. Passkeys remain bound to their hostname after a
restore or access-route change. See [Passkeys and account recovery](passkeys.md).

## Support tiers

| Tier | Routes |
|---|---|
| **Supported** | localhost HTTP, guided Tailscale HTTPS, Let's Encrypt, Cloudflare Tunnel |
| **Experimental** | local HTTPS with mkcert; Vulkan GPU acceleration |
| **Manual** | a reverse proxy you configure and verify yourself |
| **Diagnostics only** | raw-IP LAN HTTP |

The following table is checked against `route_claims` in `scripts/setup_lib.sh`:

<!-- route-claims:begin -->
| route | scheme | port | host_allowlist | setup_token_transport | cookie_policy | passkey_origin | cert_owner | tier |
|---|---|---|---|---|---|---|---|---|
| localhost-http | http | 3001 | localhost | fragment | secure | localhost | none | supported |
| raw-ip-lan | http | 3001 | lan-ip | none | none | none | none | diagnostics-only |
| named-private-https | https | 443 | origin-host | fragment | secure | origin-host | external | manual |
| tailscale-serve | https | 443 | tailnet-host | fragment | secure | tailnet-host | tailscale | supported |
| local-https | https | 3443 | localhost | fragment | secure | localhost | mkcert | experimental |
| letsencrypt | https | 443 | domain | fragment | secure | domain | letsencrypt | supported |
| tunnel | https | 443 | tunnel-host | fragment | secure | tunnel-host | cloudflare | supported |
<!-- route-claims:end -->

The raw LAN route is supported only for its exact health endpoint. Its `none`
values mean it cannot carry setup, the dashboard, a session, or a passkey
ceremony.

## Change the access method later

Run setup again and replace the selected access settings:

```bash
./setup.sh
```

When setup reports that `.env` already exists, choose **Overwrite**, then select
the new access route. For an unattended run, include `--overwrite-env` with the
route flags. Running setup without accepting overwrite starts the existing
configuration unchanged.

Papers, accounts, and unrelated operator settings remain in place. Setup
accepts a replacement route only after it verifies that exact endpoint. If a
later check fails, it restores and verifies the previous route; the next run
also resumes recovery after an interruption. If automated recovery cannot be
proved, setup stops and prints exact manual commands instead of claiming
success. A manually managed `--public-origin` is not carried to a different
route automatically; provide it again if you still want it. See [Changing
access routes safely](../DEPLOYMENT.md#changing-access-routes-safely) for the
operator procedure.

Continue with [First sign-in and setup](getting-started.md). Operator details are in [Deployment and remote access](../DEPLOYMENT.md).
