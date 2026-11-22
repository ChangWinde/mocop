// Fleet state: how the dashboard classifies each GPU and totals storage.
// Pure projections of the snapshot with no DOM, storage, or network.
//
// A GPU is "compute" exactly when the server counts it in stats.busyGpus
// (utilization at or above gpu_busy_pct). Below that, a GPU is "occupied" when
// a process holds at least HOLDER_MIN_MIB of its memory: desktop helpers and
// codec probes hold a few MiB and leave the card schedulable. Storage totals
// count a shared filesystem (NFS, Lustre, GPFS, FUSE, ...) once per device,
// because every host that mounts it reports the same volume; mocop/storage_totals.py
// is the server twin, and tests/fixtures/storage_totals.json pins both.
(() => {
  "use strict";

  const HOLDER_MIN_MIB = 256;
  const SHARED_FILESYSTEMS = Object.freeze(new Set([
    "nfs", "nfs4", "cifs", "smb3", "smbfs", "lustre", "gpfs", "ceph", "cephfs",
    "glusterfs", "beegfs", "9p", "afs", "ocfs2", "gfs2", "orangefs", "pvfs2",
  ]));
  const ACTIVITY_KEYS = Object.freeze(["compute", "occupied", "free", "unknown", "offline"]);

  function finite(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  // A process whose memory is unknown counts as a holder: the card cannot be
  // shown as free on a reading that is missing.
  function holdsGpu(process) {
    return !finite(process?.used_memory_mib) || process.used_memory_mib >= HOLDER_MIN_MIB;
  }

  function holders(gpu) {
    return (gpu.processes || []).filter(holdsGpu);
  }

  function activity(gpu, { online, busyPct }) {
    if (!online) return "offline";
    if (!finite(gpu.utilization_gpu_pct)) return "unknown";
    if (gpu.utilization_gpu_pct >= busyPct) return "compute";
    if (holders(gpu).length) return "occupied";
    // Without a process sample the card may hold memory nobody can see.
    if (gpu.processes_available === false) return "unknown";
    return "free";
  }

  function breakdown(servers, busyPct) {
    const counts = Object.fromEntries(ACTIVITY_KEYS.map((key) => [key, 0]));
    for (const server of servers) {
      const online = server.status === "online";
      for (const gpu of server.gpus || []) counts[activity(gpu, { online, busyPct })] += 1;
    }
    return Object.freeze({ ...counts, online: counts.compute + counts.occupied + counts.free + counts.unknown });
  }

  function isShared(filesystemType) {
    const type = String(filesystemType || "").toLowerCase();
    return SHARED_FILESYSTEMS.has(type) || type.startsWith("fuse.") || type.startsWith("nfs");
  }

  // Local disks are already unique per host by device (the probe dedupes
  // bind mounts); shared volumes are keyed by device across the fleet.
  function storageTotals(servers) {
    const local = { usedMiB: 0, totalMiB: 0 };
    const shared = new Map();
    for (const server of servers) {
      if (server.status !== "online" || !server.system) continue;
      const seen = new Set();
      for (const disk of server.system.disks || []) {
        if (!finite(disk.total_mib) || !finite(disk.used_mib)) continue;
        if (isShared(disk.filesystem_type)) {
          if (!shared.has(disk.device)) shared.set(disk.device, disk);
        } else if (!seen.has(disk.device)) {
          seen.add(disk.device);
          local.usedMiB += disk.used_mib;
          local.totalMiB += disk.total_mib;
        }
      }
    }
    const volumes = [...shared.values()];
    return Object.freeze({
      local: Object.freeze(local),
      shared: Object.freeze({
        volumes: volumes.length,
        usedMiB: volumes.reduce((sum, disk) => sum + disk.used_mib, 0),
        totalMiB: volumes.reduce((sum, disk) => sum + disk.total_mib, 0),
      }),
    });
  }

  function create() {
    return Object.freeze({
      HOLDER_MIN_MIB, ACTIVITY_KEYS, SHARED_FILESYSTEMS, holdsGpu, holders, activity, breakdown, isShared, storageTotals,
    });
  }

  globalThis.MocopFleetState = Object.freeze({ create });
})();
