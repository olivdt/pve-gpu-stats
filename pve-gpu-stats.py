#!/usr/bin/env python3
"""
pve-gpu-stats - NVIDIA GPU statistics for the Proxmox VE node Summary.

  * GPUs driven by the host (including the ones shared with LXC containers)
    are read with the host's nvidia-smi.
  * GPUs passed through to a VM are read by running nvidia-smi inside the
    guest through the QEMU guest agent (qm guest exec).

Samples are averaged per minute, consolidated into the same timeframes as the
built-in charts (hour, day, week, month, year; average and maximum) and
written as JSON files that the web interface extension reads.

Only the Python standard library is used.

Usage: pve-gpu-stats [daemon|check]
"""

import csv
import glob
import io
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque

VERSION = '1.0.0'


def env_int(name, default, low, high):
    try:
        return max(low, min(high, int(os.environ.get(name) or default)))
    except ValueError:
        return default


def env_flag(name, default):
    value = (os.environ.get(name) or '').strip().lower()
    if not value:
        return default
    return value not in ('0', 'no', 'off', 'false')


# Settings, see /etc/default/pve-gpu-stats
HOST_INTERVAL = env_int('PVE_GPU_HOST_INTERVAL', 10, 2, 300)
VM_INTERVAL = env_int('PVE_GPU_VM_INTERVAL', 30, 5, 600)
VM_POLLING = env_flag('PVE_GPU_VM_POLLING', True)
VM_NVIDIA_SMI = os.environ.get('PVE_GPU_VM_NVIDIA_SMI') or 'nvidia-smi'

# Paths (overridable for testing)
OUT_DIR = os.environ.get('PVE_GPU_OUT_DIR') or '/run/pve-gpu-stats'
STATE_DIR = os.environ.get('PVE_GPU_STATE_DIR') or '/var/lib/pve-gpu-stats'
SYSFS_PCI = os.environ.get('PVE_GPU_SYSFS_PCI') or '/sys/bus/pci/devices'
NVIDIA_MODULE = os.environ.get('PVE_GPU_NVIDIA_MODULE') or '/sys/module/nvidia'
QEMU_CONF_DIR = os.environ.get('PVE_GPU_QEMU_CONF_DIR') or '/etc/pve/qemu-server'
QEMU_RUN_DIR = os.environ.get('PVE_GPU_QEMU_RUN_DIR') or '/run/qemu-server'
PCI_MAPPING = os.environ.get('PVE_GPU_PCI_MAPPING') or '/etc/pve/mapping/pci.cfg'

# Timeframes of the Summary: (name, seconds per point, number of points)
TIERS = (
    ('hour', 60, 70),
    ('day', 300, 288),
    ('week', 1800, 336),
    ('month', 7200, 372),
    ('year', 43200, 732),
)
CONSOLIDATIONS = ('AVERAGE', 'MAX')
METRICS = ('util', 'mem', 'temp', 'power')

LIVE_SECONDS = 180          # a GPU seen this recently is reported as live
LISTED_SECONDS = 31 * 86400 # and one seen this recently is listed in every timeframe
FORGET_SECONDS = 400 * 86400
SAVE_INTERVAL = 900

SMI_QUERY = 'uuid,pci.bus_id,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw'
SMI_ARGS = ['--query-gpu=' + SMI_QUERY, '--format=csv,noheader,nounits']
MIB = 1024 * 1024
NVIDIA_VENDOR = '0x10de'


# --- helpers ---------------------------------------------------------------

_logged = {}


def log(message):
    print(message, file=sys.stderr, flush=True)


def log_once(topic, message):
    """Log a message for a topic only when it changes (None resets it)."""
    if _logged.get(topic) == message:
        return
    _logged[topic] = message
    if message:
        log('%s: %s' % (topic, message))


def read_text(path):
    try:
        with open(path, 'r', errors='replace') as handle:
            return handle.read()
    except OSError:
        return ''


def write_atomic(path, text, mode=0o644, sync=False):
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)
        if sync:
            handle.flush()
            os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def run(cmd, timeout):
    """Run a command, return (returncode or None, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, universal_newlines=True,
            errors='replace',
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return None, '', 'timed out after %ss' % timeout
    except OSError as err:
        return None, '', str(err)


def first_line(text, limit=200):
    for line in (text or '').splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ''


# --- nvidia-smi ------------------------------------------------------------

def number(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None      # "[N/A]", "[Not Supported]", ...
    return value if math.isfinite(value) else None


def normalize_pci(text):
    """'00000000:01:00.0' (nvidia-smi) -> '0000:01:00.0' (sysfs)."""
    match = re.match(r'^([0-9a-f]+):([0-9a-f]{2}):([0-9a-f]{2})\.([0-7])$', text.strip(), re.I)
    if not match:
        return None
    domain, bus, dev, fn = match.groups()
    return ('%s:%s:%s.%s' % (domain[-4:].rjust(4, '0'), bus, dev, fn)).lower()


def parse_smi(text):
    """Parse the CSV printed by nvidia-smi into a list of readings."""
    readings = []
    for row in csv.reader(io.StringIO(text), skipinitialspace=True):
        row = [cell.strip() for cell in row]
        if len(row) < 8:
            continue
        util, used, total, temp, power = (number(cell) for cell in row[-5:])
        uuid = row[0] if re.match(r'^[A-Za-z]+-[0-9a-fA-F-]{8,}$', row[0]) else None
        readings.append({
            'uuid': uuid,
            'pci': normalize_pci(row[1]),
            'name': ', '.join(row[2:-5]) or 'GPU',
            'util': util,
            'mem': used * MIB if used is not None else None,
            'mem_total': int(total * MIB) if total else None,
            'temp': temp,
            'power': power,
        })
    return readings


def sample_host():
    """Read the GPUs driven by the host. Returns (readings or None, error)."""
    # nvidia-smi can load the kernel module on its own, which would grab GPUs
    # that are waiting for a VM, so only call it when the module is in use.
    if not os.path.isdir(NVIDIA_MODULE):
        return None, 'the nvidia driver is not loaded on the host, no host GPU to read'
    exe = shutil.which('nvidia-smi')
    if not exe:
        return None, 'nvidia-smi not found on the host'
    code, out, err = run([exe] + SMI_ARGS, timeout=20)
    if code != 0:
        return None, 'nvidia-smi failed: %s' % (first_line(err) or first_line(out) or 'exit code %s' % code)
    return parse_smi(out), None


def sample_vm(vmid):
    """Read the GPUs of a VM through its guest agent. Returns (readings or None, error)."""
    exe = shutil.which('qm')
    if not exe:
        return None, 'qm not found'
    cmd = [exe, 'guest', 'exec', str(vmid), '--timeout', '15', '--', VM_NVIDIA_SMI] + SMI_ARGS
    code, out, err = run(cmd, timeout=30)
    if code != 0:
        return None, first_line(err) or first_line(out) or 'qm guest exec failed (exit code %s)' % code
    try:
        result = json.loads(out)
    except ValueError:
        return None, 'unexpected answer from qm guest exec'
    if not isinstance(result, dict) or not result.get('exited'):
        return None, 'nvidia-smi did not finish in the guest'
    if result.get('exitcode') != 0:
        detail = first_line(result.get('err-data')) or first_line(result.get('out-data'))
        return None, 'nvidia-smi failed in the guest: %s' % (detail or 'exit code %s' % result.get('exitcode'))
    return parse_smi(result.get('out-data') or ''), None


# --- VM discovery ----------------------------------------------------------

PCI_ADDR = re.compile(r'^(?:([0-9a-f]{4}):)?([0-9a-f]{2}):([0-9a-f]{2})(?:\.([0-7]))?$', re.I)


def nvidia_pci_devices():
    """NVIDIA display devices of the host: [(address, device id, driver)]."""
    devices = []
    for path in sorted(glob.glob(os.path.join(SYSFS_PCI, '*'))):
        if read_text(os.path.join(path, 'vendor')).strip().lower() != NVIDIA_VENDOR:
            continue
        if not read_text(os.path.join(path, 'class')).strip().lower().startswith('0x03'):
            continue
        driver = os.path.join(path, 'driver')
        devices.append((
            os.path.basename(path),
            read_text(os.path.join(path, 'device')).strip().lower().replace('0x', ''),
            os.path.basename(os.path.realpath(driver)) if os.path.exists(driver) else None,
        ))
    return devices


def is_nvidia_gpu(address):
    match = PCI_ADDR.match(address.strip())
    if not match:
        return False
    domain, bus, dev, fn = match.groups()
    pattern = '%s:%s:%s.%s' % (domain or '0000', bus, dev, fn if fn is not None else '*')
    for path in glob.glob(os.path.join(SYSFS_PCI, pattern.lower())):
        if (read_text(os.path.join(path, 'vendor')).strip().lower() == NVIDIA_VENDOR
                and read_text(os.path.join(path, 'class')).strip().lower().startswith('0x03')):
            return True
    return False


def nvidia_mappings():
    """Names of the PCI resource mappings that contain an NVIDIA device."""
    names = set()
    current = None
    for line in read_text(PCI_MAPPING).splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if not line[0].isspace():
            current = line.split()[-1].rstrip(':')
        elif current and re.match(r'^map\s', line.strip()) and re.search(r'[\s,]id=10de:', line, re.I):
            names.add(current)
    return names


def hostpci_is_nvidia(value, mappings):
    host = None
    for index, option in enumerate(value.split(',')):
        key, sep, val = option.strip().partition('=')
        if not sep:
            if index == 0:
                host = key
        elif key == 'host':
            host = val
        elif key == 'mapping':
            return val in mappings
    return bool(host) and any(is_nvidia_gpu(addr) for addr in host.split(';'))


def agent_enabled(value):
    for index, option in enumerate((value or '').split(',')):
        key, sep, val = option.strip().partition('=')
        if not sep and index == 0:
            return key == '1'
        if key == 'enabled':
            return val == '1'
    return False


def read_vm_config(path):
    config = {}
    for line in read_text(path).splitlines():
        if line.startswith('['):       # snapshot and pending sections follow
            break
        if line.startswith('#') or ':' not in line:
            continue
        key, _, value = line.partition(':')
        config[key.strip()] = value.strip()
    return config


def vm_pid(vmid):
    try:
        pid = int(read_text(os.path.join(QEMU_RUN_DIR, '%s.pid' % vmid)).strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, OSError):
        return None


def find_gpu_vms():
    """VMs of this node that have an NVIDIA GPU passed through."""
    mappings = nvidia_mappings()
    vms = []
    for path in glob.glob(os.path.join(QEMU_CONF_DIR, '*.conf')):
        vmid = os.path.basename(path)[:-5]
        if not vmid.isdigit():
            continue
        config = read_vm_config(path)
        if not any(re.match(r'^hostpci\d+$', key) and hostpci_is_nvidia(value, mappings)
                   for key, value in config.items()):
            continue
        pid = vm_pid(vmid)
        vms.append({
            'vmid': int(vmid),
            'name': config.get('name', ''),
            'pid': pid,
            'running': pid is not None,
            'agent': agent_enabled(config.get('agent')),
        })
    return sorted(vms, key=lambda vm: vm['vmid'])


# --- history ---------------------------------------------------------------

def new_accumulator():
    return [[0.0, 0, None] for _ in METRICS]     # sum, count, max per metric


def accumulate(slot, value, peak):
    slot[0] += value
    slot[1] += 1
    slot[2] = peak if slot[2] is None else max(slot[2], peak)


def consolidate(accumulators):
    """{gpu: accumulator} -> {gpu: [[average, maximum] or None per metric]}."""
    values = {}
    for gid, acc in accumulators.items():
        point = [[slot[0] / slot[1], slot[2]] if slot[1] else None for slot in acc]
        if any(item is not None for item in point):
            values[gid] = point
    return values


class Tier:
    """Ring of consolidated points for one timeframe."""

    def __init__(self, name, step, size):
        self.name = name
        self.step = step
        self.size = size
        self.points = deque(maxlen=size)     # (start, values)
        self.cur_start = None
        self.cur = {}

    def feed(self, minute, values):
        start = minute - minute % self.step
        if self.cur_start is not None and start != self.cur_start:
            self.flush()
        self.cur_start = start
        for gid, point in values.items():
            acc = self.cur.setdefault(gid, new_accumulator())
            for index, pair in enumerate(point):
                if pair is not None:
                    accumulate(acc[index], pair[0], pair[1])

    def flush(self):
        values = consolidate(self.cur)
        if values:
            self.points.append((self.cur_start, values))
        self.cur_start = None
        self.cur = {}

    def all_points(self):
        """Finished points plus the one still being filled."""
        points = list(self.points)
        if self.cur_start is not None:
            values = consolidate(self.cur)
            if values:
                points.append((self.cur_start, values))
        return points


def short_name(name):
    name = re.sub(r'^nvidia\s+', '', name or '', flags=re.I)
    name = re.sub(r'^geforce\s+', '', name, flags=re.I)
    return name.strip() or 'GPU'


def describe(gpus, now):
    """Public description of the GPUs, with labels made unique."""
    items = []
    count = {}
    for gpu in gpus:
        where = 'host' if gpu['where'] == 'host' else 'VM %s' % gpu['vmid']
        base = '%s (%s)' % (short_name(gpu['name']), where)
        count[base] = count.get(base, 0) + 1
        items.append({
            'id': gpu['id'],
            'label': base,
            'name': gpu['name'],
            'where': gpu['where'],
            'vmid': gpu['vmid'],
            'mem_total': gpu.get('mem_total'),
            'live': now - gpu.get('seen', 0) <= LIVE_SECONDS,
        })
    rank = {}
    for item in items:
        base = item['label']
        if count[base] > 1:
            rank[base] = rank.get(base, 0) + 1
            item['label'] = '%s #%d' % (base, rank[base])
    return items


def rounded(metric, value):
    return int(round(value)) if metric == 'mem' else round(value, 1)


class Store:
    def __init__(self, node):
        self.node = node
        self.lock = threading.Lock()
        self.gpus = {}           # key (uuid) -> description
        self.next_id = 0
        self.minute = None       # start of the minute being filled
        self.bucket = {}         # gpu id -> accumulator
        self.tiers = [Tier(*tier) for tier in TIERS]

    def add(self, where, vmid, readings, now):
        with self.lock:
            self._roll(now)
            for index, reading in enumerate(readings):
                scope = 'host' if where == 'host' else 'vm%s' % vmid
                key = reading['uuid'] or '%s/%s' % (scope, reading['pci'] or index)
                gpu = self.gpus.get(key)
                if gpu is None:
                    gpu = self.gpus[key] = {'id': 'g%d' % self.next_id, 'key': key}
                    self.next_id += 1
                gpu.update(name=reading['name'], where=where, vmid=vmid, seen=int(now))
                if reading['mem_total']:
                    gpu['mem_total'] = reading['mem_total']
                acc = self.bucket.setdefault(gpu['id'], new_accumulator())
                for position, metric in enumerate(METRICS):
                    if reading[metric] is not None:
                        accumulate(acc[position], reading[metric], reading[metric])

    def _roll(self, now):
        minute = int(now) - int(now) % 60
        if self.minute is not None and minute != self.minute:
            values = consolidate(self.bucket)
            if values:
                for tier in self.tiers:
                    tier.feed(self.minute, values)
            self.bucket = {}
        self.minute = minute

    def tick(self, now):
        """Close the minute that just ended."""
        with self.lock:
            self._roll(now)

    def render(self, now):
        """{file name: JSON text} for every timeframe and consolidation."""
        with self.lock:
            gpus = sorted((dict(gpu) for gpu in self.gpus.values()), key=lambda gpu: int(gpu['id'][1:]))
            tiers = [(tier, tier.all_points()) for tier in self.tiers]
        files = {}
        for tier, points in tiers:
            last = int(now) - int(now) % tier.step
            first = last - (tier.size - 1) * tier.step
            by_time = {start: values for start, values in points if first <= start <= last}
            present = set()
            for values in by_time.values():
                present.update(values)
            # the same GPUs in every timeframe keeps legends and colors steady
            shown = [gpu for gpu in gpus
                     if gpu['id'] in present or now - gpu.get('seen', 0) <= LISTED_SECONDS]
            listed = set(gpu['id'] for gpu in shown)
            for position, cf in enumerate(CONSOLIDATIONS):
                data = []
                for start in range(first, last + 1, tier.step):
                    record = {'time': start}
                    for gid, point in by_time.get(start, {}).items():
                        if gid not in listed:
                            continue
                        for index, metric in enumerate(METRICS):
                            if point[index] is not None:
                                record['%s_%s' % (gid, metric)] = rounded(metric, point[index][position])
                    data.append(record)
                files['gpu-stats-%s-%s.json' % (tier.name, cf)] = json.dumps({
                    'node': self.node,
                    'version': VERSION,
                    'updated': int(now),
                    'timeframe': tier.name,
                    'cf': cf,
                    'gpus': describe(shown, now),
                    'data': data,
                }, separators=(',', ':'))
        return files

    def write_outputs(self, now):
        for name, text in self.render(now).items():
            write_atomic(os.path.join(OUT_DIR, name), text)

    def save(self, now):
        with self.lock:
            for key in [key for key, gpu in self.gpus.items() if now - gpu.get('seen', 0) > FORGET_SECONDS]:
                del self.gpus[key]
            text = json.dumps({
                'version': 1,
                'next_id': self.next_id,
                'gpus': list(self.gpus.values()),
                'tiers': {tier.name: {
                    'points': [[start, values] for start, values in tier.points],
                    'cur_start': tier.cur_start,
                    'cur': tier.cur,
                } for tier in self.tiers},
            }, separators=(',', ':'))
        write_atomic(os.path.join(STATE_DIR, 'state.json'), text, mode=0o600, sync=True)

    def load(self):
        path = os.path.join(STATE_DIR, 'state.json')
        if not os.path.exists(path):
            return
        try:
            with open(path) as handle:
                state = json.load(handle)
            if state.get('version') != 1:
                raise ValueError('unknown version')
            gpus = {}
            for gpu in state['gpus']:
                for field in ('id', 'key', 'name', 'where', 'vmid'):
                    gpu[field]       # KeyError when incomplete
                gpus[gpu['key']] = gpu
            loaded = []
            for tier in self.tiers:
                saved = state['tiers'].get(tier.name) or {}
                points = [(int(start), values) for start, values in saved.get('points', [])
                          if isinstance(values, dict)]
                loaded.append((tier, points, saved.get('cur_start'), saved.get('cur') or {}))
            next_id = int(state['next_id'])
        except (OSError, ValueError, KeyError, TypeError) as err:
            log('ignoring unreadable history %s: %s' % (path, err))
            return
        with self.lock:
            self.gpus = gpus
            self.next_id = next_id
            for tier, points, cur_start, cur in loaded:
                tier.points = deque(points, maxlen=tier.size)
                tier.cur_start = cur_start
                tier.cur = cur if cur_start is not None else {}


# --- daemon ----------------------------------------------------------------

def host_loop(store, stop):
    while not stop.is_set():
        started = time.time()
        delay = HOST_INTERVAL
        try:
            readings, error = sample_host()
            if readings:
                store.add('host', None, readings, time.time())
                log_once('host', 'reading %d GPU(s) with nvidia-smi' % len(readings))
            else:
                log_once('host', error or 'nvidia-smi lists no GPU')
                delay = 60
        except Exception as err:      # keep the thread alive whatever happens
            log_once('host', 'unexpected error: %r' % err)
            delay = 60
        stop.wait(max(1.0, delay - (time.time() - started)))


def vm_loop(store, stop):
    retry = {}      # vmid -> (failures, not before, pid)
    while not stop.is_set():
        started = time.time()
        try:
            vms = find_gpu_vms()
        except Exception as err:
            log_once('VMs', 'cannot list the VMs: %r' % err)
            vms = []
        for vm in vms:
            if stop.is_set():
                break
            vmid = vm['vmid']
            topic = 'VM %s' % vmid
            if not vm['running']:
                retry.pop(vmid, None)
                log_once(topic, None)
                continue
            if not vm['agent']:
                log_once(topic, 'NVIDIA GPU passed through but the QEMU guest agent is not enabled, no statistics')
                continue
            failures, not_before, pid = retry.get(vmid, (0, 0, vm['pid']))
            if pid != vm['pid']:
                failures, not_before = 0, 0       # the VM was restarted
            if time.time() < not_before:
                continue
            try:
                readings, error = sample_vm(vmid)
            except Exception as err:
                readings, error = None, 'unexpected error: %r' % err
            if readings:
                store.add('vm', vmid, readings, time.time())
                retry.pop(vmid, None)
                log_once(topic, 'reading %d GPU(s) through the guest agent' % len(readings))
            else:
                failures += 1
                wait = min(600, VM_INTERVAL * 2 ** min(failures, 6))
                retry[vmid] = (failures, time.time() + wait, vm['pid'])
                log_once(topic, error or 'nvidia-smi lists no GPU in the guest')
        stop.wait(max(1.0, VM_INTERVAL - (time.time() - started)))


def daemon():
    for path in (OUT_DIR, STATE_DIR):
        os.makedirs(path, exist_ok=True)
    store = Store(socket.gethostname().split('.')[0])
    store.load()
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())

    log('pve-gpu-stats %s started (host every %ss, VMs %s)' % (
        VERSION, HOST_INTERVAL, 'every %ss' % VM_INTERVAL if VM_POLLING else 'disabled'))
    threading.Thread(target=host_loop, args=(store, stop), daemon=True).start()
    if VM_POLLING:
        threading.Thread(target=vm_loop, args=(store, stop), daemon=True).start()

    store.write_outputs(time.time())
    saved = time.time()
    while True:
        now = time.time()
        if stop.wait(61.0 - now % 60):      # one second into the next minute
            break
        now = time.time()
        try:
            store.tick(now)
            store.write_outputs(now)
            if now - saved >= SAVE_INTERVAL:
                store.save(now)
                saved = now
        except OSError as err:
            log_once('output', 'cannot write: %s' % err)
    try:
        store.save(time.time())
    except OSError as err:
        log('cannot save the history: %s' % err)
    log('pve-gpu-stats stopped')


# --- check -----------------------------------------------------------------

def show_reading(reading):
    def fmt(value, pattern):
        return pattern % value if value is not None else 'n/a'
    total = reading['mem_total']
    return '%s: usage %s, memory %s / %s MiB, %s, %s' % (
        reading['name'],
        fmt(reading['util'], '%.0f%%'),
        fmt(reading['mem'] / MIB if reading['mem'] is not None else None, '%.0f'),
        fmt(total / MIB if total else None, '%.0f'),
        fmt(reading['temp'], '%.0f C'),
        fmt(reading['power'], '%.1f W'),
    )


def check():
    """Print what the collector can see right now."""
    print('Node: %s' % socket.gethostname().split('.')[0])
    print('\nNVIDIA devices on the PCI bus:')
    devices = nvidia_pci_devices()
    for address, device, driver in devices:
        print('  %s  [10de:%s]  driver: %s' % (address, device, driver or 'none'))
    if not devices:
        print('  none found')

    print('\nHost (nvidia-smi):')
    readings, error = sample_host()
    for reading in readings or []:
        print('  ' + show_reading(reading))
    if not readings:
        print('  ' + (error or 'nvidia-smi lists no GPU'))

    print('\nVMs with an NVIDIA GPU passed through:')
    vms = find_gpu_vms()
    found = bool(readings)
    for vm in vms:
        title = '  VM %s%s: ' % (vm['vmid'], ' (%s)' % vm['name'] if vm['name'] else '')
        if not vm['running']:
            print(title + 'stopped')
        elif not vm['agent']:
            print(title + 'running, QEMU guest agent not enabled -> no statistics')
        elif not VM_POLLING:
            print(title + 'running, VM polling disabled in the settings')
        else:
            readings, error = sample_vm(vm['vmid'])
            if readings:
                found = True
                print(title + 'running, read through the guest agent')
                for reading in readings:
                    print('    ' + show_reading(reading))
            else:
                print(title + 'running, no statistics: %s' % (error or 'nvidia-smi lists no GPU in the guest'))
    if not vms:
        print('  none found')
    return 0 if found else 1


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else 'daemon'
    if command == 'daemon':
        daemon()
    elif command == 'check':
        sys.exit(check())
    elif command in ('-V', '--version', 'version'):
        print(VERSION)
    else:
        print(__doc__.strip())
        sys.exit(0 if command in ('-h', '--help', 'help') else 2)


if __name__ == '__main__':
    main()
