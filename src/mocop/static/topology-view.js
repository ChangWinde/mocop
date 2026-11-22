// Connection topology dialog: the configured SSH path tree with live host
// status. The tree DOM is built once per topology revision and reused across
// telemetry updates; each render only refreshes node state and counts.
// Opening the dialog never starts collection.
(() => {
  "use strict";

  const TRANSPORT_LABELS = Object.freeze({
    ssh: "SSH",
    "frp-stcp": "FRP · STCP",
    "frp-xtcp": "FRP · XTCP",
    vpn: "VPN",
  });

  function create({ create: element, elements, displayHost, numeric, format, onHost }) {
    let renderedRevision = -1;
    let nodeRefs = new Map();
    let mappedHosts = new Set();
    let unmappedKey = "";

    function updateNode(reference, host, server) {
      if (!server && reference.signature === "infrastructure") return;
      const state = !server ? "infrastructure" : server.status === "online" && !server.stale ? "online" : "offline";
      let meta;
      if (!server) meta = "连接路径节点 · 不采集资源";
      else if (state === "offline") meta = server.stale ? "数据陈旧" : "连接不可用";
      else {
        const cpu = numeric(server.system?.cpu_usage_pct, Number.NaN);
        meta = `${server.gpus.length} GPU · CPU ${Number.isFinite(cpu) ? `${format(cpu)}%` : "—"}`;
      }
      const signature = server ? `${displayHost(server)}\u0000${state}\u0000${meta}` : "infrastructure";
      if (reference.signature === signature) return;
      reference.signature = signature;
      reference.button.className = `topology-node ${state}`;
      reference.button.disabled = !server;
      reference.name.textContent = displayHost(server || host);
      reference.meta.textContent = meta;
      reference.button.setAttribute("aria-label", `${displayHost(server || host)}，${server ? meta : "连接路径节点，不采集资源"}`);
    }

    function node(host, isRoot) {
      const button = element("button", "topology-node infrastructure");
      button.type = "button";
      button.dataset.host = host;
      const heading = element("span", "topology-node-heading");
      const name = element("strong", "", displayHost(host));
      heading.append(element("i", "topology-node-dot"), name);
      if (isRoot) heading.append(element("small", "topology-root-badge", "起点"));
      const meta = element("span", "topology-node-meta");
      button.append(heading, meta);
      // Infrastructure nodes are disabled, so only monitored hosts navigate.
      button.addEventListener("click", () => onHost(host));
      nodeRefs.set(host, { button, name, meta, signature: "" });
      return button;
    }

    function branch(host, linksBySource, isRoot = false) {
      const item = element("li", "topology-branch");
      item.append(node(host, isRoot));
      const links = linksBySource.get(host) || [];
      if (!links.length) return item;
      const children = element("ul", "topology-children");
      links.forEach((link) => {
        const child = element("li", "topology-child");
        const connector = element("div", `topology-connector ${link.transport}`);
        const label = link.label || TRANSPORT_LABELS[link.transport];
        connector.setAttribute("role", "note");
        connector.setAttribute("aria-label", `${host} 通过 ${label} 连接到 ${link.target}`);
        const visibleLabel = element("span", "topology-link-label", label);
        visibleLabel.setAttribute("aria-hidden", "true");
        connector.append(visibleLabel);
        child.append(connector, branch(link.target, linksBySource));
        children.append(child);
      });
      item.append(children);
      return item;
    }

    function reset() {
      elements.tree.replaceChildren();
      elements.unmappedList.replaceChildren();
      renderedRevision = -1;
      nodeRefs = new Map();
      mappedHosts = new Set();
      unmappedKey = "";
      elements.unmapped.hidden = true;
    }

    function showStatus(text, error = false) {
      reset();
      elements.status.hidden = false;
      elements.status.className = `topology-status${error ? " error" : ""}`;
      elements.status.textContent = text;
    }

    function build(topology, revision) {
      const linksBySource = new Map();
      topology.links.forEach((link) => {
        if (!linksBySource.has(link.source)) linksBySource.set(link.source, []);
        linksBySource.get(link.source).push(link);
      });
      nodeRefs = new Map();
      const tree = element("ul", "topology-tree-list");
      tree.append(branch(topology.root, linksBySource, true));
      elements.tree.replaceChildren(tree);
      mappedHosts = new Set([topology.root, ...topology.links.map((link) => link.target)]);
      renderedRevision = revision;
    }

    function syncUnmapped(servers) {
      const key = servers.map((server) => `${server.host}\u0000${server.displayName || ""}`).join("\u0000");
      if (unmappedKey === key) return;
      unmappedKey = key;
      elements.unmappedList.replaceChildren(...servers.map((server) => {
        const button = element("button", "topology-unmapped-node", displayHost(server));
        button.type = "button";
        button.addEventListener("click", () => onHost(server.host));
        return button;
      }));
    }

    function render({ topology, revision, loading, error, servers }) {
      if (loading && !topology) return showStatus("正在读取本地拓扑配置…");
      if (error) return showStatus(error, true);
      if (!topology?.root) {
        showStatus("尚未在 config.json 中配置连接拓扑");
        elements.nodeCount.textContent = "0";
        elements.frpCount.textContent = "0";
        elements.liveSummary.textContent = "无拓扑配置";
        return undefined;
      }
      if (renderedRevision !== revision) build(topology, revision);
      const byHost = new Map(servers.map((server) => [server.host, server]));
      nodeRefs.forEach((reference, host) => updateNode(reference, host, byHost.get(host) || null));
      const mapped = servers.filter((server) => mappedHosts.has(server.host));
      const online = mapped.filter((server) => server.status === "online" && !server.stale).length;
      elements.nodeCount.textContent = String(mappedHosts.size);
      elements.frpCount.textContent = String(topology.links.filter((link) => link.transport.startsWith("frp-")).length);
      elements.liveSummary.textContent = `${online} / ${mapped.length} 个监控节点在线`;
      elements.status.hidden = true;
      const unmapped = servers.filter((server) => !mappedHosts.has(server.host));
      elements.unmapped.hidden = unmapped.length === 0;
      syncUnmapped(unmapped);
      return undefined;
    }

    return Object.freeze({ render });
  }

  globalThis.MocopTopologyView = Object.freeze({ create, TRANSPORT_LABELS });
})();
