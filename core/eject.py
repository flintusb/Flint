import ctypes
import re
from ctypes import wintypes
from typing import Any

_GUID_DEVCLASS_DISKDRIVE = (
    0x4D36E967, 0xE325, 0x11CE,
    0xBF, 0xC1, 0x08, 0x00, 0x2B, 0xE1, 0x03, 0x18,
)
_DIGCF_PRESENT = 0x00000002
_SPDRP_PHYSICAL_DEVICE_OBJECT_NAME = 14
_CR_SUCCESS = 0

# Failures that usually mean Windows has not finished re-enumerating the
# device - which happens right after a write rewrites the partition table -
# rather than that the drive is truly gone. Callers may retry these after
# a short delay before reporting an error to the user.
TRANSIENT_FAILURES = frozenset({
    "drive not found in device list",
    "device not found",  # CM error 0x0D
})


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", ctypes.c_ubyte * 16),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.c_size_t),
    ]


def _guid_bytes(guid: tuple[int, ...]) -> Any:
    data = ctypes.c_ubyte * 16
    guid_bytes = _encode_guid(guid)
    return data.from_buffer_copy(guid_bytes)


def _encode_guid(guid: tuple[int, ...]) -> bytes:
    import struct

    data1, data2, data3, *rest = guid
    return (
        struct.pack("<IHH", data1, data2, data3)
        + bytes(rest)
    )


def _prep_setupapi() -> tuple[Any, Any]:
    setupapi = ctypes.windll.setupapi
    cfgmgr = ctypes.windll.cfgmgr32
    _SP_DEVINFO_PTR = ctypes.POINTER(_SP_DEVINFO_DATA)
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
    ]
    setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
    setupapi.SetupDiEnumDeviceInfo.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, _SP_DEVINFO_PTR,
    ]
    setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        wintypes.HANDLE, _SP_DEVINFO_PTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    cfgmgr.CM_Request_Device_EjectW.restype = wintypes.DWORD
    cfgmgr.CM_Request_Device_EjectW.argtypes = [
        wintypes.DWORD, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD,
        wintypes.DWORD,
    ]
    return setupapi, cfgmgr


def eject_drive(drive_path: str) -> tuple[bool, str]:
    """Safely eject a physical drive (\\\\.\\PHYSICALDRIVEn) using the
    standard Windows device-eject path. Returns (ok, message)."""
    prefix = "\\\\.\\PHYSICALDRIVE"
    upper = drive_path.upper()
    if not upper.startswith(prefix):
        return False, "eject is only supported for physical drives"
    number = upper[len(prefix):]
    if not number.isdigit():
        return False, "eject is only supported for physical drives"
    target_index = int(number)

    setupapi, cfgmgr = _prep_setupapi()
    class_guid = _guid_bytes(_GUID_DEVCLASS_DISKDRIVE)
    device_set = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(class_guid),
        None,
        None,
        _DIGCF_PRESENT,
    )
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    if device_set == _INVALID_HANDLE_VALUE:
        return False, "could not enumerate disk devices"
    try:
        index = 0
        while True:
            data = _SP_DEVINFO_DATA()
            data.cbSize = ctypes.sizeof(data)
            if not setupapi.SetupDiEnumDeviceInfo(
                device_set, index, ctypes.byref(data)
            ):
                break
            index += 1
            buffer = ctypes.create_unicode_buffer(512)
            required = wintypes.DWORD()
            if not setupapi.SetupDiGetDeviceRegistryPropertyW(
                device_set,
                ctypes.byref(data),
                _SPDRP_PHYSICAL_DEVICE_OBJECT_NAME,
                None,
                buffer,
                ctypes.sizeof(buffer),
                ctypes.byref(required),
            ):
                continue
            parsed = re.match(
                r"\\Device\\Harddisk(\d+)\\DR", buffer.value
            )
            if not parsed or int(parsed.group(1)) != target_index:
                continue

            name_buf = ctypes.create_unicode_buffer(512)
            config_ret = cfgmgr.CM_Request_Device_EjectW(
                data.DevInst,
                None,
                name_buf,
                len(name_buf),
                0,
            )
            if config_ret == _CR_SUCCESS:
                return True, "ejected"
            _CR_MESSAGES = {
                0x16: "device in use — close programs accessing the drive",
                0x17: "access denied — run as administrator",
                0x0D: "device not found",
                0x0E: "invalid device",
            }
            msg = _CR_MESSAGES.get(
                config_ret, f"eject failed (error 0x{config_ret:X})"
            )
            return False, msg
        return False, "drive not found in device list"
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(device_set)
