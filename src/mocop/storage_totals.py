"""Fleet storage totals that count every volume once.

Each host's probe already reports its own filesystems once per device. Across
hosts, a shared filesystem (NFS, Lustre, GPFS, FUSE, ...) appears on every host
that mounts it, so adding hosts together would repeat it. Local filesystems are
therefore summed per host and device, and shared ones once per device for the
whole fleet. Local device names such as ``/dev/nvme0n1p2`` repeat across hosts
and are distinct disks.

``static/fleet-state.js`` applies the same rule in the browser;
``tests/fixtures/storage_totals.json`` pins both to one result.
"""

from __future__ import annotations

SHARED_FILESYSTEMS = frozenset(
    {
        "nfs", "nfs4", "cifs", "smb3", "smbfs", "lustre", "gpfs", "ceph",
        "cephfs", "glusterfs", "beegfs", "9p", "afs", "ocfs2", "gfs2",
        "orangefs", "pvfs2",
    }
)  # fmt: skip


def is_shared_filesystem(filesystem_type: str) -> bool:
    """Whether a filesystem type is network or cluster storage several hosts mount."""
    kind = filesystem_type.lower()
    return kind in SHARED_FILESYSTEMS or kind.startswith(("fuse.", "nfs"))


def storage_totals(servers: list[dict[str, object]]) -> dict[str, dict[str, float]]:
    """Local and shared storage over the online hosts' serialized disks."""
    local_used = local_total = 0.0
    shared: dict[str, dict[str, object]] = {}
    for server in servers:
        system = server.get("system")
        if server.get("status") != "online" or not isinstance(system, dict):
            continue
        seen: set[str] = set()
        for disk in system.get("disks") or ():
            device = str(disk["device"])
            if is_shared_filesystem(str(disk["filesystem_type"])):
                shared.setdefault(device, disk)
            elif device not in seen:
                seen.add(device)
                local_used += float(disk["used_mib"])
                local_total += float(disk["total_mib"])
    return {
        "local": {"usedMiB": local_used, "totalMiB": local_total},
        "shared": {
            "volumes": len(shared),
            "usedMiB": sum(float(disk["used_mib"]) for disk in shared.values()),
            "totalMiB": sum(float(disk["total_mib"]) for disk in shared.values()),
        },
    }
