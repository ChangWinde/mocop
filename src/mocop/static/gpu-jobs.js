// GPU jobs: the snapshot's GPU processes folded into the jobs people launched.
// Pure projection with no DOM, storage, or network.
//
// A scheduler workload id (Slurm job, Kubernetes pod) names a job directly.
// Otherwise one launch is the processes of one owner and program on one host
// with the same start time, which folds a multi-GPU data-parallel run into a
// single row. Device utilization is per GPU, never per process, so a job's
// utilization is the mean over the GPUs it touches.
(() => {
  "use strict";

  const STATES = Object.freeze(["compute", "idle", "light", "offline"]);

  function finite(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function create({ processName, holderMinMiB }) {
    function jobKey(server, process) {
      const workload = process.workload || {};
      if (workload.workload_id) return `${server.host}\u0000id\u0000${workload.workload_id}`;
      return [server.host, workload.owner || "", processName(process), workload.started_at || process.first_seen_at || ""]
        .join("\u0000");
    }

    function list(servers, busyPct) {
      const jobs = new Map();
      for (const server of servers) {
        const online = server.status === "online";
        for (const gpu of server.gpus || []) {
          for (const process of gpu.processes || []) {
            const key = jobKey(server, process);
            const workload = process.workload || {};
            let job = jobs.get(key);
            if (!job) {
              job = {
                key,
                host: server.host,
                online,
                owner: workload.owner || null,
                program: processName(process),
                command: workload.command || process.name || "",
                kind: workload.kind || "process",
                workloadId: workload.workload_id || null,
                queue: workload.queue || null,
                startedAt: workload.started_at || process.first_seen_at || null,
                gpus: [],
                pids: [],
                memoryMiB: 0,
                memoryKnown: true,
              };
              jobs.set(key, job);
            }
            if (!job.gpus.includes(gpu)) job.gpus.push(gpu);
            job.pids.push(process.pid);
            if (finite(process.used_memory_mib)) job.memoryMiB += process.used_memory_mib;
            else job.memoryKnown = false;
          }
        }
      }
      return [...jobs.values()].map((job) => {
        const readings = job.gpus.map((gpu) => gpu.utilization_gpu_pct).filter(finite);
        const utilization = readings.length
          ? readings.reduce((sum, value) => sum + value, 0) / readings.length : null;
        let state = "idle";
        if (!job.online) state = "offline";
        else if (finite(utilization) && utilization >= busyPct) state = "compute";
        else if (job.memoryKnown && job.memoryMiB < holderMinMiB) state = "light";
        return Object.freeze({
          ...job,
          gpus: Object.freeze(job.gpus.map((gpu) => gpu.index).sort((a, b) => a - b)),
          pids: Object.freeze(job.pids),
          utilization,
          state,
        });
      });
    }

    const normalize = (value) => String(value ?? "").normalize("NFKC").toLowerCase();

    function filter(jobs, { query = "", state = "all", owner = "" } = {}) {
      const terms = normalize(query).split(/\s+/).filter(Boolean);
      return jobs.filter((job) => {
        if (state !== "all" && job.state !== state) return false;
        if (owner && job.owner !== owner) return false;
        const haystack = normalize([job.host, job.owner, job.program, job.command, job.workloadId, job.queue].join(" "));
        return terms.every((term) => haystack.includes(term));
      });
    }

    // Most GPUs first, then most memory, then the longest running.
    function sort(jobs) {
      return [...jobs].sort((first, second) => (
        second.gpus.length - first.gpus.length
        || second.memoryMiB - first.memoryMiB
        || String(first.startedAt || "~").localeCompare(String(second.startedAt || "~"))
        || first.key.localeCompare(second.key)
      ));
    }

    return Object.freeze({ STATES, list, filter, sort });
  }

  globalThis.MocopGpuJobs = Object.freeze({ create });
})();
