// Jobs page rows: one table row per job from gpu-jobs.js. Builds nodes
// through the injected factory; the caller filters, sorts, and places them.
(() => {
  "use strict";

  const STATE_LABELS = Object.freeze({ compute: "运行中", idle: "空转", light: "轻量", offline: "失联" });
  const STATE_HINTS = Object.freeze({
    compute: "负载达到忙碌阈值",
    idle: "占着显存但负载低于忙碌阈值",
    light: "只占用很少显存（例如桌面或编解码探测进程），不影响调度",
    offline: "所在主机失联，显示最后一次读数",
  });

  function create({ create: element, format, memory, durationSince }) {
    function ownerCell(job, me) {
      const cell = element("td");
      const owner = element("span", `job-owner${job.owner ? "" : " unowned"}${me && job.owner === me ? " mine" : ""}`);
      const name = job.owner || "未归属";
      owner.append(element("span", "avatar", name.slice(0, 1).toUpperCase()), element("strong", "", name));
      if (!job.owner) owner.title = "启用 workloads.mode=identity 可显示属主";
      cell.append(owner);
      return cell;
    }

    function programCell(job) {
      const cell = element("td", "job-program");
      const command = element("small", "", job.command);
      command.title = job.command;
      cell.append(element("strong", "", job.program), command);
      return cell;
    }

    // A process that did not report memory makes the total a lower bound.
    function memoryText(job) {
      if (job.memoryKnown) return memory(job.memoryMiB);
      return job.memoryMiB > 0 ? `≥ ${memory(job.memoryMiB)}` : "未知";
    }

    function row(job, { me, hostLabel, onOpen }) {
      const line = element("tr");
      line.dataset.job = job.key;
      line.title = `${hostLabel(job.host)} · ${job.program}`;
      line.addEventListener("click", () => onOpen(job));
      const state = element("span", "job-state", STATE_LABELS[job.state]);
      state.dataset.state = job.state;
      state.title = STATE_HINTS[job.state];
      const stateCell = element("td");
      stateCell.append(state);
      line.append(
        ownerCell(job, me),
        programCell(job),
        element("td", "", `${hostLabel(job.host)} · ${job.gpus.join(", ")}`),
        element("td", "numeric", job.utilization == null ? "—" : `${format(job.utilization)}%`),
        element("td", "numeric", memoryText(job)),
        element("td", "", job.startedAt ? durationSince(job.startedAt) : "—"),
        stateCell,
      );
      return line;
    }

    return Object.freeze({ STATE_LABELS, row });
  }

  globalThis.MocopJobsView = Object.freeze({ create });
})();
