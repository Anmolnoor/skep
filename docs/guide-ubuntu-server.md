# Guide: run skep as an always-on assistant on Ubuntu 24.04

<!-- Built from a real install on 2026-11-19: an Ubuntu 24.04.5 LTS cloud image in a
     Proxmox VE VM (4 cores, 8 GB RAM, 64 GB disk), skep installed from main at
     6b0095a, the ollama provider, the builder template, bubblewrap, Codex CLI 0.171.0
     on Ubuntu's Node 18, and a Telegram channel. Addresses, VM ids and user names are
     placeholders. Two steps are marked "(not run as written)": the tested install used
     the service account as the cloud-init user and removed its sudo afterwards (the fix
     is in "Already gave the service account sudo?"), and the Telegram user id was read
     from the web UI rather than with the getUpdates call shown. The tested install also
     copied llm-secret from another install instead of using serve.env, and bound
     0.0.0.0; the guide says so where it matters. -->

This guide is for someone who wants skep on a machine that never sleeps, such
as a home lab VM or a small server, so schedules fire on time and you can reach
it from your phone. At the end you will have:

- `skep serve` running as a systemd service that starts at boot,
- the bubblewrap sandbox working under Ubuntu 24.04's AppArmor rules,
- a service account that cannot become root, and a separate admin login for you,
- optionally, a Telegram channel that answers only you.

Schedules only fire while `skep serve` runs (see
[`daily-driver-setup.md`](daily-driver-setup.md)), which is why an always-on
host matters.

## What you need first

- An Ubuntu 24.04 machine or VM. 4 cores, 8 GB of RAM and about 64 GB of disk
  were enough for the assistant plus sandboxed workers.
- A model provider. The tested install used Ollama Cloud. See
  [`onboarding.md`](onboarding.md) for the choices.
- Two logins, which this guide keeps apart on purpose:
  - **your admin login**, with sudo, for installing packages and fixing the box;
  - **a service account** (here `skep`), with no sudo, that runs skep.
- Optional: a ChatGPT plan for Codex workers, and a Telegram bot token from
  @BotFather.

## 1. Create the VM (Proxmox, optional)

Skip this section on an existing machine.

Download the cloud image on a Proxmox node and check it against Ubuntu's
published checksums:

```sh
B=https://cloud-images.ubuntu.com/noble/current
curl -fsSLO $B/noble-server-cloudimg-amd64.img
curl -fsSL $B/SHA256SUMS | grep " \*noble-server-cloudimg-amd64.img$" | sha256sum -c -
```

You should see `noble-server-cloudimg-amd64.img: OK`.

Pick a static IP outside your DHCP pool, and check it before you create the VM.
Some devices drop ping, so read the neighbour table too:

```sh
ping -c1 -W1 192.168.1.50; ip neigh show | grep "192.168.1.50 "
```

A line with a MAC address (`REACHABLE`, `STALE`) means something is using the
address. No line, or `FAILED`/`INCOMPLETE`, means nothing answered just now. It
doesn't prove the address is free, so also check your router's leases.

Create the VM. Set `VMID`, `STORAGE`, the IP and the gateway for your network.
The cloud-init user (`--ciuser`) becomes **your admin login**:

```sh
qm create $VMID --name skep --memory 8192 --cores 4 --cpu host \
  --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-pci --agent 1 --ostype l26 --onboot 1
qm set $VMID --scsi0 $STORAGE:0,import-from=/root/noble-server-cloudimg-amd64.img,discard=on
qm resize $VMID scsi0 64G
qm set $VMID --ide2 $STORAGE:cloudinit --boot order=scsi0 --serial0 socket --vga serial0
qm set $VMID --ciuser admin --sshkeys /root/your-key.pub \
  --ipconfig0 ip=192.168.1.50/24,gw=192.168.1.1 --nameserver 192.168.1.1
qm start $VMID
```

**Do not make the service account the cloud-init user.** Cloud-init gives that
user passwordless sudo (`/etc/sudoers.d/90-cloud-init-users`).

## 2. Install the packages (as admin)

```sh
sudo apt-get update
sudo apt-get install -y qemu-guest-agent bubblewrap git curl ca-certificates pipx nodejs npm
sudo systemctl start qemu-guest-agent   # VM only; the unit starts itself on later boots
```

`qemu-guest-agent` only matters on a VM. `nodejs` and `npm` are only needed for
the Codex CLI.

## 3. Let bubblewrap run under Ubuntu 24.04's AppArmor rules

Ubuntu 24.04 stops unprivileged programs from creating user namespaces
(`kernel.apparmor_restrict_unprivileged_userns = 1`). bubblewrap, skep's Linux
sandbox, needs them. Test it:

```sh
bwrap --ro-bind / / --unshare-user --unshare-net true && echo BWRAP_OK || echo BWRAP_BLOCKED
```

If you see `BWRAP_BLOCKED`, give **only** `/usr/bin/bwrap` permission to
create user namespaces with a small AppArmor profile. First check that no
profile for bwrap exists (`grep -l /usr/bin/bwrap /etc/apparmor.d/*`). If one
does, add `userns,` to it instead of writing a second one:

```sh
sudo tee /etc/apparmor.d/bwrap >/dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>

# Lets bubblewrap (skep sandbox) create user namespaces under Ubuntu 24.04's userns restriction.
profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
EOF
sudo apparmor_parser -r /etc/apparmor.d/bwrap
```

If you edited an existing profile, reload that file with `apparmor_parser -r`
instead. Run the test again. You should see `BWRAP_OK`.

Don't turn the restriction off for the whole machine
(`sysctl kernel.apparmor_restrict_unprivileged_userns=0`). That would give every
program the permission that only bubblewrap needs.

## 4. Create the service account (as admin)

(Not run as written: the tested install had cloud-init create the service
account, then removed its sudo as shown in "Already gave the service account
sudo?".)

```sh
sudo adduser --disabled-password --gecos "" skep
sudo loginctl enable-linger skep
```

Linger keeps the account's systemd services running when nobody is logged in,
and starts them at boot.

To SSH in as the service account, copy your public key into
`/home/skep/.ssh/authorized_keys` (directory mode 700, file mode 600, owned by
`skep`). Don't add the account to `sudo`, `lxd` or `docker`. Each of those is a
way to become root.

## 5. Install skep and the Codex CLI (as `skep`)

Install the PyPI release:

```sh
pipx install skep
pipx ensurepath
```

Or pin an exact commit of `main` when you need fixes that haven't been released
yet. The tested install did this, because `main` was ahead of PyPI:

```sh
pipx install "git+https://github.com/Anmolnoor/skep.git@<commit>"
```

Pinning a commit makes the install reproducible. To upgrade, run the same
command with a newer commit and `--force`, since pipx won't replace an
existing install without it.

The Codex CLI goes in a user-owned npm prefix, so it needs no sudo:

```sh
mkdir -p ~/.npm-global
npm config set prefix ~/.npm-global
npm install -g @openai/codex
~/.npm-global/bin/codex --version
```

Ubuntu's Node 18 was new enough for the tested Codex version. To sign Codex in
to your ChatGPT plan on a machine with no browser, run
`~/.npm-global/bin/codex login --device-auth` and approve the code it prints in
any browser. The device code expires after a few minutes, so approve it
promptly. If it says device code sign-in is disabled, turn it on in your
ChatGPT security settings (or ask your workspace admin), then retry.
[`codex-chatgpt-login.md`](codex-chatgpt-login.md) explains how runs
use that login.

## 6. Onboard (as `skep`)

Onboarding makes a test call to the model, so the provider key must exist
first. Put it in `~/.skep/serve.env`. `skep serve` loads that file at every
start, and you load it into the shell for onboarding:

```sh
mkdir -p ~/.skep
( umask 077; read -rs -p "Ollama API key: " k; echo
  printf 'OLLAMA_API_KEY=%s\n' "$k" > ~/.skep/serve.env )
```

Then onboard. `skep onboard` runs without prompts when you pass every choice as
a flag:

```sh
export PATH=$HOME/.local/bin:$HOME/.npm-global/bin:$PATH
set -a; . ~/.skep/serve.env; set +a
skep onboard --provider ollama --model glm-5.3 --endpoint https://ollama.com \
  --api-key-env OLLAMA_API_KEY \
  --workdir ~/workspace --sandbox auto --template builder --role builder --yes --no-chat
skep onboard --check
```

`--check` should pass every line: model reachable, sandbox working, policy
baseline locked. If you copy the policy template from another machine, compare
the baseline's sha256 that onboarding prints. Both machines then share the same
rules.

`--api-key-env` takes the variable's *name*, never the key. (The tested install
copied the key file `~/.skep/supervisor/llm-secret` from another skep install
instead. If you do that, keep it mode 600, never print it, and remember that
revoking the key stops both machines.)

## 7. Run it as a systemd user service (as `skep`)

```sh
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/skep.service <<'EOF'
[Unit]
Description=skep serve

[Service]
Environment=PATH=%h/.local/bin:%h/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=%h/.local/bin/skep serve --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now skep.service
sleep 5
systemctl --user is-active skep.service
grep 'skep serve:' ~/.skep/serve.log | tail -n 1
```

You should see `active`, then a line starting `skep serve: http://127.0.0.1:8765`.
If the line is missing, wait a few more seconds and run the `grep` again.
The access token is not in the journal: skep prints it only to a terminal.
Read it from `~/.skep/supervisor/serve-token` when you need it.

- **PATH.** The `Environment=PATH` line is what lets workers find `codex`. A
  systemd service doesn't read your shell profile.
- **Binding.** `127.0.0.1` keeps the web UI on the box. Telegram still works,
  because skep polls Telegram rather than waiting for it to connect. Reach the
  UI with an SSH tunnel (`ssh -L 8765:127.0.0.1:8765 skep@<server>`) or bind to
  a VPN address such as Tailscale's. `--host 0.0.0.0`, which the tested install
  used, opens it to your whole LAN over plain HTTP. Every `/api/` call except
  Slack's signed webhooks needs the serve token
  (`~/.skep/supervisor/serve-token`), and skep doesn't restrict the Host
  header, so the token is the only lock. Never forward the port from the
  internet.
- **Approval links.** A link skep sends to Telegram names the bind address,
  such as `http://127.0.0.1:8765/`, which won't open on your phone. Open the UI
  at the address you actually reach it on.
- **Restarting.** Run `systemctl --user restart skep.service` as `skep`. A
  restart clears approvals given "for this session".

## 8. Set the timezone (as admin)

Cloud images start on UTC. skep stores schedules in UTC, and the assistant
reads the server's clock as your local time. It uses that to turn "every day
at 9 am" into a UTC start time. On a UTC server, "9 am" means 9 am UTC:

```sh
sudo timedatectl set-timezone Europe/Berlin   # your zone; list them with: timedatectl list-timezones
```

A running skep keeps the timezone it started with, so restart it afterwards
(as `skep`: `systemctl --user restart skep.service`).

## 9. Connect Telegram (optional)

1. Create a bot with @BotFather and store its token as the channel secret. Use
   the web UI's channel panel, or `PUT /api/channels/telegram` with
   `{"secret": "<bot token>"}`. The token is write-only. skep keeps it in a
   0600 file and never shows it again.
2. Find your numeric Telegram user id. In a private chat with the bot, your chat
   id is the same number. One way, before you enable the channel (not run as
   written): send the bot a message, then read `message.from.id` from
   `https://api.telegram.org/bot<token>/getUpdates`.
3. Enable the channel for you alone. Both lists are needed, and an empty
   `allowed_users` lets nobody in. (These commands use `127.0.0.1`; if you bound
   another address in section 7, use that.)

   ```sh
   printf 'Authorization: Bearer %s\n' "$(cat ~/.skep/supervisor/serve-token)" |
   curl -sS --fail-with-body -X PUT http://127.0.0.1:8765/api/channels/telegram \
     -H @- \
     -H "Content-Type: application/json" \
     -d '{"enabled": true, "allowed_identities": ["<your id>"], "allowed_users": ["<your id>"]}'
   ```

4. To let your phone confirm cards, set `{"channel_can_confirm": true}` the same
   way. Your phone then confirms only the four channel-confirmable classes
   listed in [`daily-driver-setup.md`](daily-driver-setup.md). HIGH cards still
   need the web UI or the CLI.
5. Check it:

   ```sh
   skep channel status
   ```

   You should see `telegram  enabled ...; secret: present; ... can_confirm: on`.

## 10. Check the lockdown

Run these before you rely on the box.

- **The service account can't become root.** As admin, `sudo -l -U skep` must
  say `User skep is not allowed to run sudo on <host>`, and `id skep` must not list
  `sudo`, `lxd` or `docker`.
- **The running service dropped any old groups.** A systemd user service gets
  its groups from the account's user manager, which keeps the groups it started
  with. After changing groups, restart the manager as admin, then check the
  process:

  ```sh
  sudo systemctl restart user@$(id -u skep).service                        # as admin
  grep ^Groups /proc/$(systemctl --user show -p MainPID --value skep.service)/status  # as skep
  ```

  The numbers are group ids. `getent group sudo lxd docker` shows which ids those three are.

- **No keys on the box reach other machines.** Look in `~skep/.ssh/`.
  `authorized_keys` is fine. A private key that logs in as root anywhere else
  undoes everything above.

### Already gave the service account sudo?

This happens when the service account was the cloud-init user. As your admin
login (create one first, and test that it can use sudo before you continue):

```sh
sudo rm /etc/sudoers.d/90-cloud-init-users   # check first that it only names the service account
sudo gpasswd -d skep sudo
sudo gpasswd -d skep lxd
sudo touch /etc/cloud/cloud-init.disabled
sudo systemctl restart user@$(id -u skep).service
```

Disabling cloud-init stops it re-creating the sudo rule. That could happen the
next time the VM's cloud-init settings change in Proxmox. Network settings
written at first boot stay in place.

## What goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `skep onboard --check` or `skep doctor` says the sandbox is unusable | AppArmor blocks bubblewrap's user namespaces | Section 3 |
| Codex workers fail with `codex` not found, but it works in your shell | The service's PATH doesn't include `~/.npm-global/bin` | The `Environment=PATH` line in section 7 |
| `id skep` no longer lists `sudo` or `lxd`, but the service still has the group | The user manager kept the old groups | Restart `user@<uid>.service` (section 10) |
| Schedules the assistant made fire hours early or late | The server is on UTC, so "9 am" became 9 am UTC | Section 8, then recreate the schedule |
| Telegram messages get no reply | The channel is disabled, or your id isn't in both lists | `skep channel status`, then section 9 |
| Telegram tells you to approve on the web | `channel_can_confirm` is off, or the card is HIGH | Section 9 step 4. HIGH cards need the web UI or the CLI by design. |
| The Codex device login code stopped working | It expired before you approved it | Run `codex login --device-auth` again and approve within a few minutes |
