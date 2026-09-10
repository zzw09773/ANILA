const empty = () => ({ readonlyPaths: [], readwritePaths: [] });
export function getAvailableToolsPolicy() { return empty(); }
export function getRemoteToolsPolicy() { return empty(); }
export function getUserProfilePolicy() { return empty(); }
export function getTemporaryFilesPolicy() { return empty(); }
export function buildSandboxPayload() { return { readonlyPaths: [], readwritePaths: [] }; }
export default {
  getAvailableToolsPolicy,
  getRemoteToolsPolicy,
  getUserProfilePolicy,
  getTemporaryFilesPolicy,
  buildSandboxPayload,
};
