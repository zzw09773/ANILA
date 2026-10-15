// Agent picker plus, when the router is selected, its model and thinking pickers.
// Rendered above the composer; thinking here sets the conversation tier,
// separate from the one-shot deep-thinking button beside the input.

import React from "react";
import { AgentSelector } from "../chat.jsx";
import RouterModelPicker from "./RouterModelPicker.jsx";
import ThinkingPicker from "./ThinkingPicker.jsx";

export function AgentModelControls({
  agents, selectedAgentId, onSelectAgent, isRouter,
  routerModels, selectedRouterModelId, routerDefaultId, routerFallbackName,
  routerModelError, modelNoticeActive, locked, onChangeRouterModel,
  thinkingTier, selectedThinkingModel, thinkingError, onChangeThinkingTier,
}) {
  return (
    <>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <span style={{ fontSize: 11, color: "var(--fg-muted)" }}>助手</span>
        <AgentSelector agents={agents} value={selectedAgentId} onChange={onSelectAgent} />
      </div>
      {isRouter ? (
        <>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span style={{ fontSize: 11, color: "var(--fg-muted)" }}>模型</span>
            <RouterModelPicker
              models={routerModels}
              selectedId={selectedRouterModelId}
              defaultModelId={routerDefaultId}
              fallbackName={routerFallbackName}
              error={routerModelError}
              highlighted={modelNoticeActive}
              disabled={locked}
              onChange={onChangeRouterModel}
            />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span style={{ fontSize: 11, color: "var(--fg-muted)" }}>對話思考</span>
            <ThinkingPicker
              value={thinkingTier}
              model={selectedThinkingModel}
              error={thinkingError}
              disabled={locked}
              onChange={onChangeThinkingTier}
            />
          </div>
        </>
      ) : null}
    </>
  );
}
