import { describe, it, expect } from "vitest";
import { normalizeAgents } from "../app.jsx";

describe("normalizeAgents", () => {
  it("prepends the anila-router pseudo-agent when input is empty", () => {
    const out = normalizeAgents([]);
    expect(out).toHaveLength(1);
    expect(out[0].id).toBe("anila-router");
    expect(out[0].requiresEncryption).toBe(false);
  });

  it("tolerates null/undefined input", () => {
    expect(normalizeAgents(undefined)).toHaveLength(1);
    expect(normalizeAgents(null)).toHaveLength(1);
  });

  it("maps snake_case backend fields to camelCase UI fields", () => {
    const out = normalizeAgents([
      {
        id: "hr-agent",
        name: "HR Bot",
        description_for_router: "Answers HR questions",
        endpoint_url: "http://hr:9100",
        capabilities: { fileUpload: true },
        requires_encryption: true,
      },
    ]);
    const agent = out.find((a) => a.id === "hr-agent");
    expect(agent).toMatchObject({
      id: "hr-agent",
      name: "HR Bot",
      description: "Answers HR questions",
      endpointUrl: "http://hr:9100",
      capabilities: { fileUpload: true },
      requiresEncryption: true,
    });
  });

  it("coerces requires_encryption to a boolean", () => {
    const out = normalizeAgents([
      { id: "a", requires_encryption: 1 },
      { id: "b", requires_encryption: 0 },
      { id: "c" },
      { id: "d", requires_encryption: null },
    ]);
    const byId = Object.fromEntries(out.map((a) => [a.id, a.requiresEncryption]));
    expect(byId.a).toBe(true);
    expect(byId.b).toBe(false);
    expect(byId.c).toBe(false);
    expect(byId.d).toBe(false);
  });

  it("falls back name/short/description when backend omits them", () => {
    const out = normalizeAgents([{ id: "bare" }]);
    const agent = out.find((a) => a.id === "bare");
    expect(agent.name).toBe("bare");
    expect(agent.short).toBe("bare");
    expect(agent.description).toBe("");
    expect(agent.capabilities).toEqual({});
  });

  it("truncates short to 12 chars", () => {
    const out = normalizeAgents([
      { id: "abcdefghijklmnopqrstuvwxyz", short: "abcdefghijklmnopqrstuvwxyz" },
    ]);
    const agent = out.find((a) => a.id.startsWith("abc"));
    expect(agent.short.length).toBeLessThanOrEqual(12);
  });

  it("keeps the router agent in position 0 before registry agents", () => {
    const out = normalizeAgents([
      { id: "zz-agent" },
      { id: "aa-agent" },
    ]);
    expect(out[0].id).toBe("anila-router");
    expect(out[1].id).toBe("zz-agent");
    expect(out[2].id).toBe("aa-agent");
  });
});
