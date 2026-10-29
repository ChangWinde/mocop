(() => {
  "use strict";
  const bindPrompt = ({authentication, elements, startDashboard, view, setConnection}) => authentication.bindPrompt({
    dialog: elements.authenticationDialog,
    form: elements.authenticationForm,
    input: elements.authenticationToken,
    submit: elements.authenticationSubmit,
    status: elements.authenticationStatus,
    authenticate: async () => {
      view.authenticationFailed = false;
      const started = await startDashboard();
      return { started, rejected: view.authenticationFailed };
    },
    onRequired: () => {
      view.authenticationFailed = true;
      view.dashboardStarted = false;
      setConnection("offline", "需要访问令牌");
    },
  });
  const controls = ({request, select, storage}) => ({
    updatePill: globalThis.MocopUpdatePill.create({button:select("#update-pill"), request}),
    terminalLaunch: globalThis.MocopTerminalLaunch.create({button:select("#open-terminal"), picker:select("#terminal-app"), request, storage}),
  });
  const observers = ({request, select}) => ({
    memory: globalThis.MocopUserMemory.create({button:select("#user-memory-open"), dialog:select("#user-memory-dialog"), close:select("#user-memory-close"), content:select("#user-memory-content"), request}),
    trainingJobs: globalThis.MocopTrainingJobs.create({request}),
    personalStorage: globalThis.MocopStorageAnalysis.create({request}),
  });
  const start = async ({loadStoredBackground, authentication, view, fetchSnapshot, fetchTopology, trainingJobs, personalStorage, connect}) => {
    loadStoredBackground();
    if (authentication.consumeInvalidFragment()) authentication.forget();
    // A successful snapshot decides whether this deployment requires authentication.
    view.dashboardStarted = true;
    const snapshotLoaded = await fetchSnapshot();
    if (view.authenticationFailed || !snapshotLoaded) return false;
    fetchTopology();
    trainingJobs.start();
    personalStorage.start();
    connect();
    return true;
  };
  globalThis.MocopDashboardStartup = Object.freeze({bindPrompt, controls, observers, start});
})();
