"""Disk and volume tools for Proxmox MCP.

- Grow a QEMU VM disk
- Move a VM disk or container volume to another storage
- Destroy an unusedN volume left behind by a move or detach
"""
import json
import re
from typing import Any, Dict, List, Optional

from mcp.types import TextContent as Content

from proxmox_mcp.tools.base import ProxmoxTool

_VM_RESIZE_DISK = re.compile(r"(?:scsi|sata|virtio|ide)\d+")
_VM_MOVE_DISK = re.compile(r"(?:scsi|sata|virtio|ide|efidisk|tpmstate)\d+|unused\d+")
_CT_MOVE_VOLUME = re.compile(r"rootfs|mp\d+|unused\d+")
_UNUSED_VOLUME = re.compile(r"unused\d+")
_SIZE = re.compile(r"\+?\d+[KMGT]")
_STORAGE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")


def _guest_type(vm_type: str) -> str:
    if vm_type not in ("qemu", "lxc"):
        raise ValueError("vm_type must be 'qemu' or 'lxc'")
    return vm_type


class DiskTools(ProxmoxTool):
    """Disk resize, move and clean-up for VMs and containers."""

    def _json_fmt(self, data: Any) -> List[Content]:
        return [Content(type="text", text=json.dumps(data, indent=2, sort_keys=True))]

    def _guest(self, node: str, vmid: str, vm_type: str) -> Any:
        if _guest_type(vm_type) == "lxc":
            return self.proxmox.nodes(node).lxc(vmid)
        return self.proxmox.nodes(node).qemu(vmid)

    def resize_vm_disk(
        self, node: str, vmid: str, disk: str, size: str, approval_token: Optional[str] = None
    ) -> List[Content]:
        """Grow a QEMU VM disk (PUT /nodes/{node}/qemu/{vmid}/resize).

        Parameters:
            node: Host node name.
            vmid: VM ID.
            disk: Disk key, e.g. 'scsi0'.
            size: New absolute size ('45G') or increment ('+10G'). Proxmox never shrinks.
        """
        _ = approval_token
        if not _VM_RESIZE_DISK.fullmatch(disk):
            raise ValueError("disk must be scsiN, sataN, virtioN or ideN")
        if not _SIZE.fullmatch(size):
            raise ValueError("size must look like '45G' or '+10G' (units K, M, G, T)")
        vm_api = self.proxmox.nodes(node).qemu(vmid)
        config = vm_api.config.get()
        if not isinstance(config, dict) or not config.get(disk):
            raise ValueError(f"VM {vmid} has no disk {disk}")
        try:
            upid = vm_api.resize.put(disk=disk, size=size)
        except Exception as e:
            self._handle_error(f"resize disk {disk} of VM {vmid}", e)
        job = self._register_background_job(
            tool_name="resize_vm_disk",
            summary=f"Resize VM {vmid} {disk} to {size}",
            node=node,
            upid=upid,
            metadata={"vmid": vmid, "disk": disk, "size": size, "previous": config.get(disk)},
            retry_spec={"kind": "vm.resize", "params": {"node": node, "vmid": vmid, "disk": disk, "size": size}},
            retry_factory=lambda: vm_api.resize.put(disk=disk, size=size),
            cancel_factory=lambda task: self.proxmox.nodes(node).tasks(task).delete(),
        )
        result = {"node": node, "vmid": vmid, "disk": disk, "size": size, "previous": config.get(disk), "task_id": upid}
        return self._json_fmt(result) + self._submission_content(job, upid)

    def move_disk(
        self,
        node: str,
        vmid: str,
        disk: str,
        storage: str,
        vm_type: str = "qemu",
        delete_source: bool = False,
        approval_token: Optional[str] = None,
    ) -> List[Content]:
        """Move a VM disk or container volume to another storage.

        VMs use POST .../qemu/{vmid}/move_disk, containers POST .../lxc/{vmid}/move_volume.
        Without delete_source the old volume stays attached as unusedN.

        Parameters:
            node: Host node name.
            vmid: VM or container ID.
            disk: VM disk key ('scsi0') or container volume ('rootfs', 'mp0').
            storage: Destination storage ID.
            vm_type: 'qemu' or 'lxc'.
            delete_source: Destroy the source volume after a successful copy.
        """
        _ = approval_token
        pattern = _CT_MOVE_VOLUME if _guest_type(vm_type) == "lxc" else _VM_MOVE_DISK
        if not pattern.fullmatch(disk):
            raise ValueError(f"disk '{disk}' is not a movable {vm_type} volume")
        if not _STORAGE.fullmatch(storage):
            raise ValueError("storage must be a Proxmox storage ID")
        guest = self._guest(node, vmid, vm_type)
        config = guest.config.get()
        if not isinstance(config, dict) or not config.get(disk):
            raise ValueError(f"{vm_type} {vmid} has no volume {disk}")
        request: Dict[str, Any] = {"storage": storage, "delete": int(delete_source)}
        request["volume" if vm_type == "lxc" else "disk"] = disk
        endpoint = guest.move_volume if vm_type == "lxc" else guest.move_disk

        def submit() -> Any:
            return endpoint.post(**request)

        try:
            upid = submit()
        except Exception as e:
            self._handle_error(f"move {disk} of {vm_type} {vmid} to {storage}", e)
        job = self._register_background_job(
            tool_name="move_disk",
            summary=f"Move {vm_type} {vmid} {disk} to {storage}",
            node=node,
            upid=upid,
            metadata={"vmid": vmid, "vm_type": vm_type, "disk": disk, "source": config.get(disk), "request": request},
            retry_spec={"kind": "disk.move", "params": {"node": node, "vmid": vmid, "vm_type": vm_type, "request": request}},
            retry_factory=submit,
            cancel_factory=lambda task: self.proxmox.nodes(node).tasks(task).delete(),
        )
        result = {
            "node": node, "vmid": vmid, "vm_type": vm_type, "disk": disk, "source": config.get(disk),
            "storage": storage, "delete_source": delete_source, "task_id": upid,
        }
        return self._json_fmt(result) + self._submission_content(job, upid)

    def delete_unused_disk(
        self,
        node: str,
        vmid: str,
        volume: str,
        vm_type: str = "qemu",
        approval_token: Optional[str] = None,
    ) -> List[Content]:
        """Destroy an unusedN volume and remove it from the guest config.

        Parameters:
            node: Host node name.
            vmid: VM or container ID.
            volume: Config key, 'unused0' to 'unused255'.
            vm_type: 'qemu' or 'lxc'.
        """
        _ = approval_token
        if not _UNUSED_VOLUME.fullmatch(volume):
            raise ValueError("volume must be an unusedN key, e.g. 'unused0'")
        guest = self._guest(node, vmid, vm_type)
        config = guest.config.get()
        if not isinstance(config, dict) or not config.get(volume):
            raise ValueError(f"{vm_type} {vmid} has no {volume}")
        payload: Dict[str, Any] = {"delete": volume}
        if config.get("digest"):
            payload["digest"] = config["digest"]
        try:
            guest.config.put(**payload)
        except Exception as e:
            self._handle_error(f"delete {volume} of {vm_type} {vmid}", e)
        return self._json_fmt({"node": node, "vmid": vmid, "vm_type": vm_type, "deleted": {volume: config[volume]}})
