import json
from unittest.mock import Mock

import pytest

from proxmox_mcp.tools.cluster import ClusterTools
from proxmox_mcp.tools.disks import DiskTools


def _payload(response):
    return json.loads(response[0].text)


def test_resize_vm_disk_puts_resize_and_reports_previous_size():
    proxmox = Mock()
    vm_api = proxmox.nodes.return_value.qemu.return_value
    vm_api.config.get.return_value = {"scsi0": "local-lvm:vm-107-disk-0,size=32G"}
    vm_api.resize.put.return_value = "UPID:resize"

    payload = _payload(DiskTools(proxmox).resize_vm_disk("pve", "107", "scsi0", "+10G"))

    vm_api.resize.put.assert_called_once_with(disk="scsi0", size="+10G")
    assert payload["previous"] == "local-lvm:vm-107-disk-0,size=32G"
    assert payload["task_id"] == "UPID:resize"


@pytest.mark.parametrize("disk, size", [("scsi0", "10G;rm"), ("scsi0", "-5G"), ("efidisk0", "+1G"), ("net0", "+1G")])
def test_resize_vm_disk_rejects_bad_input(disk, size):
    proxmox = Mock()
    with pytest.raises(ValueError):
        DiskTools(proxmox).resize_vm_disk("pve", "107", disk, size)
    proxmox.nodes.return_value.qemu.return_value.resize.put.assert_not_called()


def test_resize_vm_disk_refuses_missing_disk():
    proxmox = Mock()
    vm_api = proxmox.nodes.return_value.qemu.return_value
    vm_api.config.get.return_value = {"scsi0": "local-lvm:vm-107-disk-0,size=32G"}

    with pytest.raises(ValueError, match="no disk scsi1"):
        DiskTools(proxmox).resize_vm_disk("pve", "107", "scsi1", "+1G")
    vm_api.resize.put.assert_not_called()


def test_move_disk_container_uses_move_volume_and_keeps_source_by_default():
    proxmox = Mock()
    ct_api = proxmox.nodes.return_value.lxc.return_value
    ct_api.config.get.return_value = {"rootfs": "local-lvm:vm-120-disk-0,size=8G"}
    ct_api.move_volume.post.return_value = "UPID:move"

    payload = _payload(DiskTools(proxmox).move_disk("pve", "120", "rootfs", "local-zfs", vm_type="lxc"))

    ct_api.move_volume.post.assert_called_once_with(storage="local-zfs", delete=0, volume="rootfs")
    proxmox.nodes.return_value.qemu.assert_not_called()
    assert payload["delete_source"] is False


def test_move_disk_vm_uses_move_disk_with_delete():
    proxmox = Mock()
    vm_api = proxmox.nodes.return_value.qemu.return_value
    vm_api.config.get.return_value = {"scsi0": "local-lvm:vm-107-disk-0,size=45G"}
    vm_api.move_disk.post.return_value = "UPID:move"

    DiskTools(proxmox).move_disk("pve", "107", "scsi0", "local-zfs", delete_source=True)

    vm_api.move_disk.post.assert_called_once_with(storage="local-zfs", delete=1, disk="scsi0")


@pytest.mark.parametrize("disk, storage, vm_type", [("scsi0", "local", "lxc"), ("rootfs", "local", "qemu"), ("rootfs", "../x", "lxc"), ("rootfs", "local", "openvz")])
def test_move_disk_rejects_bad_input(disk, storage, vm_type):
    proxmox = Mock()
    with pytest.raises(ValueError):
        DiskTools(proxmox).move_disk("pve", "120", disk, storage, vm_type=vm_type)
    proxmox.nodes.return_value.lxc.return_value.move_volume.post.assert_not_called()
    proxmox.nodes.return_value.qemu.return_value.move_disk.post.assert_not_called()


def test_delete_unused_disk_sends_digest_and_reports_volume():
    proxmox = Mock()
    ct_api = proxmox.nodes.return_value.lxc.return_value
    ct_api.config.get.return_value = {"unused0": "local-lvm:vm-120-disk-0", "digest": "abc"}

    payload = _payload(DiskTools(proxmox).delete_unused_disk("pve", "120", "unused0", vm_type="lxc"))

    ct_api.config.put.assert_called_once_with(delete="unused0", digest="abc")
    assert payload["deleted"] == {"unused0": "local-lvm:vm-120-disk-0"}


@pytest.mark.parametrize("volume", ["scsi0", "rootfs", "unused0,scsi0"])
def test_delete_unused_disk_refuses_attached_volumes(volume):
    proxmox = Mock()
    with pytest.raises(ValueError):
        DiskTools(proxmox).delete_unused_disk("pve", "120", volume, vm_type="lxc")
    proxmox.nodes.return_value.lxc.return_value.config.put.assert_not_called()


def test_delete_unused_disk_refuses_missing_volume():
    proxmox = Mock()
    vm_api = proxmox.nodes.return_value.qemu.return_value
    vm_api.config.get.return_value = {"scsi0": "local-lvm:vm-107-disk-0"}

    with pytest.raises(ValueError, match="no unused1"):
        DiskTools(proxmox).delete_unused_disk("pve", "107", "unused1")
    vm_api.config.put.assert_not_called()


def test_get_token_permissions_lists_every_granted_privilege():
    proxmox = Mock()
    proxmox.access.permissions.get.return_value = {
        "/vms/107": {"VM.PowerMgmt": 0, "VM.Audit": 1},
        "/": {"Sys.Audit": 1},
    }

    payload = _payload(ClusterTools(proxmox).get_token_permissions())

    proxmox.access.permissions.get.assert_called_once_with()
    assert payload == {"/": ["Sys.Audit"], "/vms/107": ["VM.Audit", "VM.PowerMgmt"]}


def test_get_token_permissions_filters_by_path():
    proxmox = Mock()
    proxmox.access.permissions.get.return_value = {}

    ClusterTools(proxmox).get_token_permissions(path="/vms/107")

    proxmox.access.permissions.get.assert_called_once_with(path="/vms/107")
