export class AppInsightsCore {
  constructor() {}
  initialize() { return this; }
  track() { return this; }
  trackPageView() { return this; }
  unload() { return Promise.resolve(); }
  pollInternalLogs() { return this; }
  addNotificationListener() { return { remove() {} }; }
  getChannels() { return []; }
  getPlugin() { return null; }
}
export default { AppInsightsCore };
