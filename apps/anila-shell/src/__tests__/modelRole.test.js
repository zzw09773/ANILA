import { afterEach, describe, expect, it } from "vitest";

import { resetRoleCache, resolveRoleModel, RoleUnresolvedError } from "../runtime/modelRole.js";

afterEach(() => {
  resetRoleCache();
});

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

describe("resolveRoleModel", () => {
  it("returns the summary role and caches it", async () => {
    const calls = [];
    const fetchImpl = async (url) => {
      calls.push(url);
      return jsonResponse(200, { name: "sum-llm" });
    };
    await expect(resolveRoleModel("http://csp", "summary", fetchImpl)).resolves.toBe("sum-llm");
    await expect(resolveRoleModel("http://csp", "summary", fetchImpl)).resolves.toBe("sum-llm");
    expect(calls).toEqual(["http://csp/api/models/roles/summary"]);
  });

  it("names the role when it is unset", async () => {
    const fetchImpl = async () =>
      jsonResponse(404, { detail: "摘要模型尚未在治理中心設定" });
    await expect(resolveRoleModel("", "summary", fetchImpl)).rejects.toBeInstanceOf(
      RoleUnresolvedError,
    );
    await expect(resolveRoleModel("", "summary", fetchImpl)).rejects.toThrow(
      "摘要模型尚未在治理中心設定",
    );
  });
});
