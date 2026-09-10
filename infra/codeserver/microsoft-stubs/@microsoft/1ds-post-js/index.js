export class PostChannel {
  constructor() {}
  initialize() { return this; }
  processTelemetry() {}
  pause() {}
  resume() {}
  teardown() {}
  flush() { return Promise.resolve(); }
}
export default { PostChannel };
