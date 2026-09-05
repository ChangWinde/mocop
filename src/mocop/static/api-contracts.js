// Payload contracts for the private API, extracted from app.js under the
// ADR-0021 leaf pattern. Every normalizer either returns a bounded, typed
// projection of a server response or throws TypeError, so the dashboard can
// dereference accepted data unguarded and a malformed payload never replaces
// good state. No DOM, no storage, no network; tests/api_contracts_test.mjs
// exercises the accepted shapes and every rejection.
(() => {
  "use strict";

  const TOPOLOGY_TRANSPORT_VALUES = new Set(["ssh", "frp-stcp", "frp-xtcp", "vpn"]);

  function create() {
    function safeStoredHosts(value) {
      if (!Array.isArray(value)) return [];
      return [...new Set(value.filter(
        (host) => typeof host === "string" && /^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$/.test(host),
      ))];
    }

    // Renderers dereference the envelope unguarded: the server-owned
    // thresholds replace any threshold policy in the browser, stats always
    // carry the actionable counts, and servers is the spine of every view.
    function assertSnapshotEnvelope(snapshot) {
      if (
        !snapshot
        || typeof snapshot !== "object"
        || !Number.isSafeInteger(snapshot.version)
        || typeof snapshot.startedAt !== "string"
        || typeof snapshot.collectionStaleAfterSeconds !== "number"
        || !snapshot.thresholds
        || typeof snapshot.thresholds !== "object"
        || !snapshot.stats
        || typeof snapshot.stats !== "object"
        || !Array.isArray(snapshot.servers)
        || snapshot.servers.some(
          (server) => !server || typeof server.host !== "string" || !Array.isArray(server.gpus),
        )
      ) {
        throw new TypeError("Invalid snapshot envelope");
      }
    }

    function assertIncidentsEnvelope(incidents) {
      if (
        !incidents
        || typeof incidents !== "object"
        || !Number.isSafeInteger(incidents.version)
        || !Array.isArray(incidents.active)
        || !Array.isArray(incidents.events)
        || !Array.isArray(incidents.correlations)
      ) {
        throw new TypeError("Invalid incidents envelope");
      }
    }

    function normalizeCollectorSettings(payload) {
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        throw new TypeError("Invalid collector settings response");
      }
      const {
        pollIntervalSeconds, probeTimeoutSeconds, connectTimeoutSeconds, maxWorkers,
      } = payload;
      if (
        typeof pollIntervalSeconds !== "number"
        || !Number.isFinite(pollIntervalSeconds)
        || pollIntervalSeconds < 1
        || pollIntervalSeconds > 3600
        || typeof probeTimeoutSeconds !== "number"
        || !Number.isFinite(probeTimeoutSeconds)
        || probeTimeoutSeconds < 2
        || probeTimeoutSeconds > 300
        // Read-only: the service reports it so the dialog can explain the
        // probe-timeout lower bound.
        || typeof connectTimeoutSeconds !== "number"
        || !Number.isFinite(connectTimeoutSeconds)
        || connectTimeoutSeconds <= 0
        || connectTimeoutSeconds > 300
        || !Number.isSafeInteger(maxWorkers)
        || maxWorkers < 1
        || maxWorkers > 64
      ) {
        throw new TypeError("Invalid collector settings response");
      }
      return { pollIntervalSeconds, probeTimeoutSeconds, connectTimeoutSeconds, maxWorkers };
    }

    function normalizeHostGroups(payload, configuredHosts) {
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        throw new TypeError("Invalid host groups response");
      }
      const configured = new Set(configuredHosts);
      const groups = {};
      Object.entries(payload).forEach(([host, group]) => {
        if (
          !configured.has(host)
          || typeof group !== "string"
          || !group
          || group !== group.trim()
          || [...group].length > 48
          || /\p{C}/u.test(group)
        ) {
          throw new TypeError("Invalid host groups response");
        }
        groups[host] = group;
      });
      return groups;
    }

    function normalizeMaintenanceWindows(payload, configuredHosts) {
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        throw new TypeError("Invalid maintenance windows response");
      }
      const configured = new Set(configuredHosts);
      const allowedKeys = new Set(["until", "reason", "recurring", "active"]);
      const windows = {};
      Object.entries(payload).forEach(([host, window]) => {
        const keys = window && typeof window === "object" && !Array.isArray(window)
          ? Object.keys(window) : null;
        if (
          !configured.has(host)
          || keys == null
          || !keys.includes("until")
          || !keys.includes("reason")
          || keys.some((key) => !allowedKeys.has(key))
          // recurring is only emitted for recurring windows; active always is.
          || (keys.includes("recurring") && typeof window.recurring !== "boolean")
          || typeof window.active !== "boolean"
          || typeof window.until !== "string"
          || !Number.isFinite(Date.parse(window.until))
          || typeof window.reason !== "string"
          || [...window.reason].length > 120
          || /\p{C}/u.test(window.reason)
        ) {
          throw new TypeError("Invalid maintenance windows response");
        }
        windows[host] = {
          until: window.until,
          reason: window.reason,
          recurring: window.recurring === true,
          active: window.active,
        };
      });
      return windows;
    }

    function normalizeInventory(payload) {
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        throw new TypeError("Invalid inventory response");
      }
      const configuredHosts = safeStoredHosts(payload.configuredHosts);
      const activeHosts = safeStoredHosts(payload.activeHosts);
      const availableHosts = safeStoredHosts(payload.availableHosts);
      const collectorSettings = normalizeCollectorSettings(payload.collectorSettings);
      const maintenanceWindows = normalizeMaintenanceWindows(
        payload.maintenanceWindows,
        configuredHosts,
      );
      const hostGroups = normalizeHostGroups(payload.hostGroups, configuredHosts);
      const infrastructureHosts = safeStoredHosts(payload.infrastructureHosts || []);
      const sshDiscoveryWarnings = Array.isArray(payload.sshDiscoveryWarnings)
        ? payload.sshDiscoveryWarnings.filter((item) => typeof item === "string").slice(0, 1024)
        : [];
      const sshDiscoveryMode = payload.sshDiscoveryMode || "aliases";
      if (
        configuredHosts.length !== payload.configuredHosts?.length
        || activeHosts.length !== payload.activeHosts?.length
        || availableHosts.length !== payload.availableHosts?.length
        || (payload.localHost != null && !safeStoredHosts([payload.localHost]).length)
        || typeof payload.autoDiscover !== "boolean"
        || typeof payload.writable !== "boolean"
        || !Number.isSafeInteger(payload.ignoredCodeHostCount)
        || !Number.isSafeInteger(payload.excludedHostCount)
        || !["aliases", "topology"].includes(sshDiscoveryMode)
        || infrastructureHosts.length !== (payload.infrastructureHosts || []).length
      ) {
        throw new TypeError("Invalid inventory response");
      }
      return {
        configuredHosts,
        activeHosts,
        availableHosts,
        localHost: payload.localHost,
        autoDiscover: payload.autoDiscover,
        writable: payload.writable,
        ignoredCodeHostCount: Math.max(0, payload.ignoredCodeHostCount),
        excludedHostCount: Math.max(0, payload.excludedHostCount),
        collectorSettings,
        maintenanceWindows,
        hostGroups,
        infrastructureHosts, sshDiscoveryWarnings, sshDiscoveryMode,
      };
    }

    function normalizeTopology(payload) {
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        throw new TypeError("Invalid topology response");
      }
      if (payload.root == null && Array.isArray(payload.links) && payload.links.length === 0) {
        return { root: null, links: [] };
      }
      const root = safeStoredHosts([payload.root])[0];
      if (!root || !Array.isArray(payload.links) || payload.links.length > 512) {
        throw new TypeError("Invalid topology response");
      }
      const targets = new Set();
      const children = new Map();
      const links = payload.links.map((link) => {
        if (!link || typeof link !== "object" || Array.isArray(link)) {
          throw new TypeError("Invalid topology link");
        }
        const keys = Object.keys(link);
        if (
          !["source", "target", "transport"].every((key) => keys.includes(key))
          || keys.some((key) => !["source", "target", "transport", "label"].includes(key))
        ) {
          throw new TypeError("Invalid topology link");
        }
        const source = safeStoredHosts([link.source])[0];
        const target = safeStoredHosts([link.target])[0];
        const label = link.label == null ? null : link.label;
        if (
          !source
          || !target
          || source === target
          || target === root
          || targets.has(target)
          || !TOPOLOGY_TRANSPORT_VALUES.has(link.transport)
          || (label != null && (
            typeof label !== "string"
            || !label
            || label !== label.trim()
            || [...label].length > 64
            || /\p{C}/u.test(label)
          ))
        ) {
          throw new TypeError("Invalid topology link");
        }
        targets.add(target);
        if (!children.has(source)) children.set(source, []);
        children.get(source).push(target);
        return { source, target, transport: link.transport, label };
      });
      const reachable = new Set([root]);
      const pending = [root];
      while (pending.length) {
        (children.get(pending.pop()) || []).forEach((target) => {
          if (reachable.has(target)) return;
          reachable.add(target);
          pending.push(target);
        });
      }
      if (links.some((link) => !reachable.has(link.source) || !reachable.has(link.target))) {
        throw new TypeError("Invalid topology tree");
      }
      return { root, links };
    }

    return Object.freeze({
      safeStoredHosts,
      assertSnapshotEnvelope,
      assertIncidentsEnvelope,
      normalizeCollectorSettings,
      normalizeInventory,
      normalizeTopology,
      normalizeHostGroups,
      normalizeMaintenanceWindows,
    });
  }

  globalThis.MocopApiContracts = Object.freeze({ create });
})();
