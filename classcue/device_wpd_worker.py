"""COM lives only in this disposable worker, never in the web server thread."""

from __future__ import annotations

import base64
import ctypes as c
import hashlib
import json
import sys

from classcue.device_protocol import BLOCK_BYTES, ROOT_NAME
from classcue.device_wpd import relative_path


class PortableDevices:
    def __init__(self, *, qa=False):
        self.root_name = "ClassCue-Link-QA" if qa else ROOT_NAME
        import comtypes
        import comtypes.client
        self.com = comtypes
        self.client = comtypes.client
        self.api = self.client.GetModule("PortableDeviceApi.dll")
        self.types = self.client.GetModule("PortableDeviceTypes.dll")
        self.manager = self.client.CreateObject(self.api.PortableDeviceManager, interface=self.api.IPortableDeviceManager)
        self.known = {}
        self.device = None
        self.device_token = None
        self.root = None
        self.property_guid = "{EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C}"
        self.folder_guid = self.com.GUID("{27E2E392-A111-48E0-AB0C-E17705A05F85}")
        self.ole = c.OleDLL("ole32")
        self.ole.CoTaskMemFree.argtypes = [c.c_void_p]

    def key(self, pid, guid=None):
        return self.api._tagpropertykey(self.com.GUID(guid or self.property_guid), pid)

    def values(self):
        return self.client.CreateObject(self.types.PortableDeviceValues, interface=self.api.IPortableDeviceValues)

    def devices(self):
        self.manager.RefreshDeviceList()
        count = c.c_ulong(0)
        # comtypes' generated [in,out] wrappers do not preserve this count.
        self.manager._IPortableDeviceManager__com_GetDevices(None, c.byref(count))
        if not count.value:
            self.known = {}
            return []
        slots = (c.c_void_p * count.value)()
        self.manager._IPortableDeviceManager__com_GetDevices(c.cast(slots, c.POINTER(c.c_wchar_p)), c.byref(count))
        output = []
        known = {}
        for index in range(count.value):
            raw = c.wstring_at(slots[index])
            self.ole.CoTaskMemFree(slots[index])
            token = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            known[token] = raw
            buffer = (c.c_ushort * 512)()
            length = c.c_ulong(512)
            try:
                self.manager._IPortableDeviceManager__com_GetDeviceFriendlyName(raw, buffer, c.byref(length))
                name = bytes(buffer).decode("utf-16-le").split("\0", 1)[0]
            except Exception:
                name = "Android 设备"
            output.append({"id": token, "name": name})
        self.known = known
        return output

    def open(self, token):
        if token not in self.known:
            self.devices()
        raw = self.known.get(token)
        if raw is None:
            raise OSError("设备已断开")
        if self.device_token == token and self.device is not None:
            return
        self.device = self.client.CreateObject(self.api.PortableDeviceFTM, interface=self.api.IPortableDevice)
        info = self.values()
        info.SetStringValue(self.key(2, "{204D9F0C-2292-4080-9F42-40664E70F859}"), "ClassCue")
        self.device.Open(raw, info)
        self.device_token = token
        self.content = self.device.Content()
        self.properties = self.content.Properties()
        self.resources = self.content.Transfer()
        self.root = None

    def children(self, parent):
        enumerator = self.content.EnumObjects(0, parent, None)
        output = []
        while True:
            slot = c.c_wchar_p()
            fetched = c.c_ulong()
            enumerator._IEnumPortableDeviceObjectIDs__com_Next(1, c.byref(slot), c.byref(fetched))
            if not fetched.value:
                break
            object_id = slot.value
            pointer = c.cast(slot, c.c_void_p)
            self.ole.CoTaskMemFree(pointer)
            properties = self.properties.GetValues(object_id, None)
            try:
                name = properties.GetStringValue(self.key(12))
            except Exception:
                name = properties.GetStringValue(self.key(4))
            try:
                folder = str(properties.GetGuidValue(self.key(7))).upper() == str(self.folder_guid).upper()
            except Exception:
                folder = False
            try:
                size = int(properties.GetUnsignedLargeIntegerValue(self.key(11)))
            except Exception:
                size = 0
            output.append({"object": object_id, "name": name, "folder": folder, "size": size})
        return output

    def find(self, parent, name):
        matches = [entry for entry in self.children(parent) if entry["name"] == name]
        if len(matches) > 1:
            raise OSError("设备内有重名传输文件，请重新授权接收目录")
        return matches[0] if matches else None

    def link_root(self):
        # Standard Documents names are object metadata, never translated display names.
        for storage in self.children("DEVICE"):
            documents = self.find(storage["object"], "Documents")
            if documents:
                root = self.find(documents["object"], self.root_name)
                if root:
                    self.root = root["object"]
                    return self.root
        raise FileNotFoundError("请在平板“连接电脑”中授权 Documents/ClassCue-Link 目录")

    def resolve(self, path, create_parents=False):
        parts = relative_path(path).split("/") if path else []
        parent = self.root or self.link_root()
        for name in parts:
            child = self.find(parent, name)
            if child is None:
                if not create_parents:
                    raise FileNotFoundError(path)
                properties = self.values()
                properties.SetStringValue(self.key(3), parent)
                properties.SetStringValue(self.key(4), name)
                properties.SetGuidValue(self.key(7), self.folder_guid)
                properties.SetGuidValue(self.key(6), self.com.GUID("{30010000-AE6C-4804-98BA-C57B46965FE7}"))
                created = c.c_wchar_p()
                self.content._IPortableDeviceContent__com_CreateObjectWithPropertiesOnly(properties, c.byref(created))
                parent = created.value
                self.ole.CoTaskMemFree(c.cast(created, c.c_void_p))
            else:
                parent = child["object"]
        return parent

    def read(self, path, maximum):
        object_id = self.resolve(path)
        key = self.key(0, "{E81E79BE-34F0-41BF-B53F-F1A06AE87842}")
        optimal = c.c_ulong()
        stream = c.POINTER(self.api.IStream)()
        self.resources._IPortableDeviceResources__com_GetStream(object_id, c.byref(key), 0, c.byref(optimal), c.byref(stream))
        output = bytearray()
        buffer = (c.c_ubyte * (256 * 1024))()
        while True:
            amount = c.c_ulong()
            stream._ISequentialStream__com_RemoteRead(buffer, len(buffer), c.byref(amount))
            if not amount.value:
                break
            output.extend(bytes(buffer[:amount.value]))
            if len(output) > maximum:
                raise ValueError("设备文件超过限制")
        return bytes(output)

    def delete(self, path):
        try:
            object_id = self.resolve(path)
        except FileNotFoundError:
            return
        collection = self.client.CreateObject(self.types.PortableDevicePropVariantCollection,
                                              interface=self.api.IPortableDevicePropVariantCollection)
        variant = self.api.tag_inner_PROPVARIANT()
        variant.vt = 31
        getattr(variant, self.api.tag_inner_PROPVARIANT._fields_[-1][0]).pwszVal = object_id
        collection.Add(c.byref(variant))
        results = c.POINTER(self.api.IPortableDevicePropVariantCollection)()
        self.content._IPortableDeviceContent__com_Delete(0, collection, c.byref(results))

    def write(self, path, content):
        parts = relative_path(path).split("/")
        if len(content) > BLOCK_BYTES:
            raise ValueError("数据块过大")
        parent = self.resolve("/".join(parts[:-1]), create_parents=True)
        if self.find(parent, parts[-1]):
            self.delete(path)
        properties = self.values()
        properties.SetStringValue(self.key(3), parent)
        properties.SetStringValue(self.key(4), parts[-1])
        properties.SetStringValue(self.key(12), parts[-1])
        properties.SetUnsignedLargeIntegerValue(self.key(11), len(content))
        properties.SetGuidValue(self.key(7), self.com.GUID("{0085E0A6-8D34-45D7-BC5C-447E59C73D48}"))
        properties.SetGuidValue(self.key(6), self.com.GUID("{30000000-AE6C-4804-98BA-C57B46965FE7}"))
        stream = c.POINTER(self.api.IStream)()
        optimal = c.c_ulong()
        cookie = c.c_wchar_p()
        self.content._IPortableDeviceContent__com_CreateObjectWithPropertiesAndData(
            properties, c.byref(stream), c.byref(optimal), c.byref(cookie))
        try:
            for offset in range(0, len(content), 256 * 1024):
                piece = content[offset:offset + 256 * 1024]
                buffer = (c.c_ubyte * len(piece)).from_buffer_copy(piece)
                count = c.c_ulong()
                stream._ISequentialStream__com_RemoteWrite(buffer, len(piece), c.byref(count))
                if count.value != len(piece):
                    raise OSError("设备未完整接收数据")
            stream.Commit(0)
        finally:
            if cookie:
                self.ole.CoTaskMemFree(c.cast(cookie, c.c_void_p))

    def execute(self, request):
        operation = request["op"]
        if operation == "devices":
            return self.devices()
        self.open(request["device"])
        path = relative_path(request.get("path", ""))
        if operation == "list":
            return [{key: value for key, value in entry.items() if key != "object"} for entry in self.children(self.resolve(path))]
        if operation == "read":
            return base64.b64encode(self.read(path, min(int(request.get("maximum", BLOCK_BYTES)), BLOCK_BYTES))).decode("ascii")
        if operation == "write":
            self.write(path, base64.b64decode(request["content"], validate=True))
        elif operation == "delete":
            self.delete(path)
        else:
            raise ValueError("未知设备操作")
        return None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    devices = PortableDevices(qa=sys.argv[1:] == ["--qa"])
    for line in sys.stdin:
        try:
            if len(line) > 48 * 1024 * 1024:
                raise ValueError("设备请求过大")
            result = devices.execute(json.loads(line))
            response = {"ok": True, "result": result}
        except FileNotFoundError as error:
            response = {"ok": False, "error": str(error), "missing": True}
        except Exception as error:
            # Device errors must not expose COM device identifiers or local paths.
            devices.device = None
            devices.device_token = None
            response = {"ok": False, "error": "设备操作失败，请确认已解锁并启用文件传输"}
        print(json.dumps(response, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
