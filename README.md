# pve-gpu-stats

NVIDIA GPU charts in the **Summary** page of a Proxmox VE node, right after the
built-in "CPU Usage" chart: usage, video memory, temperature and power draw,
with one curve per GPU. The charts follow the Summary's timeframe selector
(Hour / Day / Week / Month / Year, Average / Maximum) like the built-in ones.

Both GPUs driven by the host (including those shared with LXC containers) and
GPUs passed through to VMs are supported.

## Installation

On the Proxmox VE node, get the files (for example with
`git clone https://github.com/olivdt/pve-gpu-stats.git`, or download the
repository from GitHub), then run the installer as root from that folder:

```bash
bash install.sh
```

At the end the installer prints what the collector detects (host GPUs, VMs with
a GPU passed through, and why a VM has no statistics if that is the case).
Then reload the web interface (Ctrl+Shift+R): the charts show up after about a
minute, once the first values are in.

Running the installer again updates an existing installation.

Requirements: a Proxmox VE node (written against the 9.x web interface), the
NVIDIA driver and `nvidia-smi` on the host for host GPUs, and `python3` (the
installer tells you if it is missing). Only the Python standard library is
used, nothing else is installed.

## How it works

| Part | Location | Role |
|---|---|---|
| Collector | `/usr/local/sbin/pve-gpu-stats` (service `pve-gpu-stats`) | Reads the GPUs, keeps the history, writes JSON files to `/run/pve-gpu-stats/` |
| Web extension | `/usr/share/pve-manager/js/pve-gpu-stats.js` | Adds the charts to the Summary |
| Hook | one `<script>` line in `/usr/share/pve-manager/index.html.tpl` | Loads the extension |
| APT hook | `/etc/apt/apt.conf.d/99-pve-gpu-stats` | Puts the hook back after a `pve-manager` upgrade |

- **Host GPUs** (including those shared with LXC containers) are read with
  `nvidia-smi` on the host, every 10 s. `nvidia-smi` is only called when the
  nvidia driver is already loaded, so it never grabs a card reserved for
  passthrough.
- **Passthrough GPUs**: the host cannot see their activity. The collector runs
  `nvidia-smi` *inside the VM* through the QEMU guest agent (`qm guest exec`),
  every 30 s. Each VM concerned therefore needs:
  - the QEMU guest agent installed in the guest and enabled in the VM options;
  - the NVIDIA drivers installed in the guest (so that `nvidia-smi` exists).

  A stopped VM, or one without a working agent, simply has no curve for as
  long as that lasts.

Samples are averaged per minute and consolidated into the same timeframes as
the built-in charts. The history survives restarts (`/var/lib/pve-gpu-stats`).

A GPU is tracked by its UUID: it keeps its curve and its color if it moves from
the host to a VM, or from one VM to another.

## Settings

`/etc/default/pve-gpu-stats` (sampling intervals, switch to stop querying VMs,
path of `nvidia-smi` inside the guests), then:

```bash
systemctl restart pve-gpu-stats
```

Which charts are shown, their order, and where they are inserted (after "CPU
Usage" or at the end) are set at the top of `pve-gpu-stats.js`.

## Troubleshooting

```bash
pve-gpu-stats check
```

```bash
journalctl -u pve-gpu-stats -n 30
```

- *No charts*: check that the service is running and reload the page without
  cache. Charts only appear once at least one GPU can be read.
- *A VM has no curve*: `pve-gpu-stats check` tells why (agent not enabled,
  `nvidia-smi` not found in the guest, ...). On RHEL-like guests, the agent's
  `guest-exec` command is typically blocked by default and has to be allowed.

## Limitations

- The JSON files are served by `pveproxy` **without authentication**, like the
  other static files of the web interface: anyone who can reach port 8006 can
  read the GPU names, their statistics and the IDs of the VMs that have a GPU
  (not the VM names).
- In a cluster, the charts only show for the node the browser is connected to
  (install on every node that has GPUs).
- Querying a VM spawns a short-lived `qm guest exec` process on the host every
  30 s. Set `PVE_GPU_VM_POLLING=0` to turn it off.
- Only NVIDIA GPUs are supported.

## Status

Tested on Proxmox VE 9.2.11. Reports from other setups and Proxmox VE versions
are welcome.

## Uninstall

```bash
bash install.sh --uninstall
```

Add `--purge` to also delete the history and the settings. As a last resort,
`apt install --reinstall pve-manager` restores the original web interface.

## License

[MIT](LICENSE). This project is independent of Proxmox Server Solutions GmbH.
It plugs into the Proxmox VE web interface at runtime but contains none of its
code.
