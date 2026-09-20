#!/usr/bin/env python3
"""
machines.py  —  the saved list of DNC machines (name + IP address).

Stored as JSON in the per-user config directory so it survives app updates and
is not part of the repo.  QS Explorer keeps the same information in a plain
`Device.dat` of "<name> <ip>" lines, which `import_device_dat` reads.
"""

import ipaddress
import json
import os
import sys
from dataclasses import dataclass, asdict
from typing import List, Optional

APP_DIR_NAME = 'FanucToHurco'
FILE_NAME = 'machines.json'


@dataclass
class Machine:
    name: str
    ip: str

    def label(self) -> str:
        return f'{self.name}  ({self.ip})'


# ─────────────────────────────── validation ─────────────────────────────────
def validate(name: str, ip: str, existing: Optional[List[Machine]] = None,
             editing: Optional[Machine] = None) -> Optional[str]:
    """Returns an error message, or None when the pair is usable."""
    name = name.strip()
    ip = ip.strip()
    if not name:
        return 'Give the machine a name.'
    if not ip:
        return 'Give the machine an IP address.'
    try:
        ipaddress.IPv4Address(ip)
    except ipaddress.AddressValueError:
        return f'"{ip}" is not a valid IPv4 address.'
    for other in existing or []:
        if other is editing:
            continue
        if other.name.strip().lower() == name.lower():
            return f'There is already a machine named "{other.name}".'
        if other.ip == ip:
            return f'{other.name} already uses {ip}.'
    return None


# ──────────────────────────────── storage ───────────────────────────────────
def config_dir() -> str:
    """Per-user config directory, following the platform convention."""
    if sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support')
    elif os.name == 'nt':
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
    else:
        base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, APP_DIR_NAME)


def config_path() -> str:
    return os.path.join(config_dir(), FILE_NAME)


def load(path: Optional[str] = None) -> List[Machine]:
    """Read the saved machines.  A missing or damaged file yields an empty list."""
    path = path or config_path()
    try:
        with open(path, encoding='utf-8') as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return []
    machines: List[Machine] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and item.get('name') and item.get('ip'):
            machines.append(Machine(str(item['name']), str(item['ip'])))
    return machines


def save(machines: List[Machine], path: Optional[str] = None) -> None:
    """Write the machine list, creating the config directory if needed."""
    path = path or config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump([asdict(m) for m in machines], fh, indent=2)
        fh.write('\n')
    os.replace(tmp, path)          # never leave a half-written list behind


def import_device_dat(path: str) -> List[Machine]:
    """Read QS Explorer's `Device.dat`: one "<name> <ip>" per line."""
    found: List[Machine] = []
    with open(path, encoding='latin-1', errors='replace') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            name, _, ip = line.rpartition(' ')
            name, ip = name.strip(), ip.strip()
            if not name or validate(name, ip) is not None:
                continue
            found.append(Machine(name, ip))
    return found
