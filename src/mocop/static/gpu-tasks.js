// GPU task projections: pure helpers that turn a GPU process record into the
// facts an operator reads first — the real entry point behind a generic
// interpreter, the Python environment it runs in, its host-side CPU/memory
// footprint and the per-card summary. Extracted from app.js under the
// ADR-0021 leaf pattern. No DOM, no storage, no network.
(() => {
  "use strict";

  // Launchers whose argv0 says nothing about the job. For these the command
  // line is scanned for the actual entry point.
  const INTERPRETERS = /^(?:python[0-9.]*|pypy[0-9.]*|sh|bash|zsh|dash|env|node|deno|bun|perl|ruby[0-9.]*|java|uv|uvx|torchrun|deepspeed|accelerate|mpirun|mpiexec|srun|ray)$/;
  const SCRIPT_SUFFIX = /\.(?:py|sh|bash|zsh|js|mjs|ts|rb|pl|lua|jl|r)$/i;
  const ENVIRONMENT_PATTERNS = Object.freeze([
    /\/envs\/([^/]+)\/(?:bin|Scripts)\//, // conda / mamba named environments
    /\/uv\/tools\/([^/]+)\/bin\//, // uv tool installs
    /\/virtualenvs\/([^/]+)\/bin\//, // poetry-style shared virtualenvs
    /\/([^/]+)\/\.?venv[^/]*\/bin\//, // project-local venv: name the project
  ]);

  function create() {
    function processName(process) {
      const fullName = String(process.name || "unknown process");
      return fullName.replaceAll("\\", "/").split("/").at(-1) || fullName;
    }

    function processStartMs(process) {
      const timestamp = process.workload?.started_at || process.first_seen_at;
      const parsed = timestamp ? Date.parse(timestamp) : NaN;
      // Processes without any start signal sort behind every dated process.
      return Number.isFinite(parsed) ? parsed : Number.MAX_SAFE_INTEGER;
    }

    // "python3.10" tells nobody what the card is doing. When argv0 is a bare
    // interpreter, surface the module after -m or the first script argument,
    // so the row reads "train.dragon_video2motion" instead.
    function taskEntry(process) {
      const interpreter = processName(process);
      if (!INTERPRETERS.test(interpreter)) return null;
      const command = String(process.workload?.command || "");
      const tokens = command.split(/\s+/).filter(Boolean).slice(1);
      for (let index = 0; index < tokens.length; index += 1) {
        const token = tokens[index];
        if (token === "-c") return null; // inline code has no nameable entry
        if (token === "-m" && tokens[index + 1]) {
          return tokens[index + 1].split(/[^\w.-]/, 1)[0] || null;
        }
        if (!token.startsWith("-") && SCRIPT_SUFFIX.test(token)) {
          return token.replaceAll("\\", "/").split("/").at(-1);
        }
      }
      return null;
    }

    function environmentName(process) {
      const path = String(process.name || "").replaceAll("\\", "/");
      for (const pattern of ENVIRONMENT_PATTERNS) {
        const matched = path.match(pattern);
        if (matched) return matched[1];
      }
      return null;
    }

    // Host-side footprint of the PID: average busy cores over its lifetime
    // (cumulative CPU seconds / wall-clock runtime) and resident memory.
    function footprint(process, nowMs) {
      const workload = process.workload || {};
      const memoryValue = Number(workload.rss_mib);
      const memoryMiB = workload.rss_mib != null
        && Number.isFinite(memoryValue) && memoryValue >= 0 ? memoryValue : null;
      let averageCores = null;
      const cpuSeconds = Number(workload.cpu_seconds);
      const startMs = workload.started_at ? Date.parse(workload.started_at) : NaN;
      if (
        workload.cpu_seconds != null && Number.isFinite(cpuSeconds)
        && cpuSeconds >= 0 && Number.isFinite(startMs) && Number.isFinite(nowMs)
      ) {
        const runtimeSeconds = Math.max((nowMs - startMs) / 1000, 1);
        averageCores = Math.max(cpuSeconds / runtimeSeconds, 0);
      }
      return { averageCores, memoryMiB };
    }

    function summarize(gpu) {
      const processes = Array.isArray(gpu.processes) ? gpu.processes : [];
      let knownMemoryMiB = 0;
      let knownMemoryCount = 0;
      let topProcess = null;
      let topMemoryMiB = -1;
      let ownedCount = 0;
      let identifiedCount = 0;
      let oldestProcess = null;
      let oldestStartMs = Number.MAX_SAFE_INTEGER;
      const owners = new Set();
      processes.forEach((process) => {
        const processMemory = Number(process.used_memory_mib);
        if (process.used_memory_mib != null && Number.isFinite(processMemory)) {
          knownMemoryMiB += processMemory;
          knownMemoryCount += 1;
          if (processMemory > topMemoryMiB) {
            topMemoryMiB = processMemory;
            topProcess = process;
          }
        } else if (!topProcess) {
          topProcess = process;
        }
        if (process.workload) identifiedCount += 1;
        if (process.workload?.owner) {
          ownedCount += 1;
          owners.add(String(process.workload.owner));
        }
        const startMs = processStartMs(process);
        if (startMs < oldestStartMs) {
          oldestStartMs = startMs;
          oldestProcess = process;
        }
      });
      return {
        count: processes.length,
        knownMemoryMiB,
        knownMemoryCount,
        topProcess,
        topMemoryMiB,
        ownedCount,
        identifiedCount,
        ownerCount: owners.size,
        oldestProcess,
      };
    }

    return Object.freeze({
      processName,
      processStartMs,
      taskEntry,
      environmentName,
      footprint,
      summarize,
    });
  }

  globalThis.MocopGpuTasks = Object.freeze({ create });
})();
