"""What a file's Windows access list lets other local users do (review S5).

deploy/install.ps1 closes data\\, .certs\\ and logs\\ to other local users and
stops them changing the app, because the keys are machine-scope DPAPI: anyone
who can read a key file can unwrap it. Nothing else would show that lock going
away (a restore from backup, a hand-started copy, an edit in Explorer), so the
admin console's self-test asks Windows what the access lists say today.
"""
from __future__ import annotations

import ctypes
import re
from pathlib import Path

_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x4
_SDDL_REVISION_1 = 1

_GENERIC_ALL = 0x10000000
_GENERIC_WRITE = 0x40000000
_GENERIC_READ = 0x80000000
_FILE_READ_DATA = 0x1
_FILE_WRITE_DATA = 0x2
_FILE_APPEND_DATA = 0x4
_DELETE = 0x10000
_WRITE_DAC = 0x40000
_WRITE_OWNER = 0x80000

READ = _FILE_READ_DATA | _GENERIC_READ | _GENERIC_ALL
CHANGE = (_FILE_WRITE_DATA | _FILE_APPEND_DATA | _DELETE | _WRITE_DAC | _WRITE_OWNER
          | _GENERIC_WRITE | _GENERIC_ALL)

# SDDL writes an access mask as these two-letter rights when it can, and as hex
# otherwise. The directory-service names mean the file bits of the same value:
# a lone read right comes out as "CC".
_SDDL_RIGHTS = {
    "GA": _GENERIC_ALL, "GW": _GENERIC_WRITE, "GR": _GENERIC_READ, "GX": 0x20000000,
    "FA": 0x1F01FF, "FR": 0x120089, "FW": 0x120116, "FX": 0x1200A0,
    "CC": 0x1, "DC": 0x2, "LC": 0x4, "SW": 0x8, "RP": 0x10, "WP": 0x20,
    "DT": 0x40, "LO": 0x80, "CR": 0x100,
    "SD": _DELETE, "RC": 0x20000, "WD": _WRITE_DAC, "WO": _WRITE_OWNER,
}

# (ace type;flags;rights;object guid;inherited object guid;account;...)
_ACE = re.compile(r"\(([A-Z]+);[^;]*;([^;]*);[^;]*;[^;]*;([^;)]+)")

_BROAD_GROUPS = {
    "WD": "Everyone", "AU": "Authenticated Users", "BU": "Users",
    "IU": "Interactive users", "BG": "Guests", "AN": "Anonymous",
    "DU": "Domain Users", "DG": "Domain Guests",
}
# A domain other than the machine's own has no SDDL alias.
_DOMAIN_GROUP = re.compile(r"S-1-5-21-\d+-\d+-\d+-(513|514)$")


def _rights_mask(rights: str) -> int:
    if rights.lower().startswith("0x"):
        return int(rights, 16)
    mask = 0
    for start in range(0, len(rights), 2):
        mask |= _SDDL_RIGHTS.get(rights[start:start + 2], 0)
    return mask


def broad_grants(sddl: str, wanted: int) -> list[str]:
    """The groups covering other local users that a DACL gives any `wanted` right.

    Inherit-only entries count, since they decide what new files get. Deny
    entries are ignored: this reports what the list hands out, and a lock is not
    built on a deny entry.
    """
    if "NO_ACCESS_CONTROL" in sddl:  # a NULL DACL, as on FAT: no access control at all
        return ["Everyone"]
    groups: set[str] = set()
    for kind, rights, account in _ACE.findall(sddl):
        if kind not in ("A", "XA") or not _rights_mask(rights) & wanted:
            continue
        if account in _BROAD_GROUPS:
            groups.add(_BROAD_GROUPS[account])
        elif _DOMAIN_GROUP.match(account):
            groups.add(f"domain group {account}")
    return sorted(groups)


def dacl_sddl(path: Path) -> str:
    """The access list of `path` as an SDDL string. Windows only; raises OSError."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.GetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_int, ctypes.c_ulong,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = ctypes.c_ulong
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_void_p,
    ]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = ctypes.c_int

    descriptor = ctypes.c_void_p()
    error = advapi32.GetNamedSecurityInfoW(
        str(path), _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
        None, None, None, None, ctypes.byref(descriptor),
    )
    if error:
        raise ctypes.WinError(error)
    try:
        text = ctypes.c_wchar_p()
        converted = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, _SDDL_REVISION_1, _DACL_SECURITY_INFORMATION, ctypes.byref(text), None,
        )
        if not converted:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value or ""
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(descriptor)
