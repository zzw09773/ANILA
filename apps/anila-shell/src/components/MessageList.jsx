// Conversation transcript: the empty state, or every message on the active
// path with the compact-summary banner at its boundary. Each message is a
// MessageBubble (chat.jsx); per-message UI such as follow-up suggestions
// belongs there, wired through the props below.

import React from "react";
import { compactBoundaryOnPath } from "../runtime/compact.js";
import { CompactBoundaryBanner } from "../compactBoundary.jsx";
import { MessageBubble, questionAttachmentBasis } from "../chat.jsx";
import { EmptyState } from "./EmptyState.jsx";

export function MessageList({
  scrollRef,
  messages, conversation, conversationId, agents, activeAgent, loadingAgents,
  classified, latestAssistantId, messageActions, showRawReasoning,
  onStarterPick, onRestoreFullContext,
  onRegenerate, onRate, onEditUser, onSwitchBranch, onDeleteBranch,
  onOpenCitation, onPickFollowUp, onAction, onContinue, onInterruptSubmit,
  onCiteDocument,
  followUpsEnabled = true,
}) {
  return (
    <div ref={scrollRef} style={{
      flex: 1, overflowY: "auto", background: "var(--bg)",
      display: "flex", flexDirection: "column",
    }}>
      <div style={{
        maxWidth: 760, margin: "0 auto", width: "100%",
        padding: `calc(var(--density) * 1.2) var(--density)`,
        ...(messages.length === 0
          ? { flex: 1, display: "flex", flexDirection: "column", justifyContent: "center" }
          : {}),
      }}>
        {messages.length === 0 ? (
          <EmptyState
            agent={activeAgent}
            agents={agents}
            loading={loadingAgents}
            onPick={onStarterPick}
          />
        ) : (
          messages.map((m, idx) => (
            <React.Fragment key={m.id}>
              {conversation?.compactSummary
                && compactBoundaryOnPath(messages, conversation.compactBoundaryMessageId)
                && m.dbId === conversation.compactBoundaryMessageId ? (
                <CompactBoundaryBanner
                  summary={conversation.compactSummary}
                  onRestore={onRestoreFullContext}
                />
              ) : null}
              <MessageBubble
                msg={m}
                agents={agents}
                conversationId={conversationId}
                classified={classified}
                classificationLevel={conversation?.classificationLevel}
                onRegenerate={onRegenerate}
                onRate={onRate}
                onEditUser={onEditUser}
                onSwitchBranch={onSwitchBranch}
                onDeleteBranch={onDeleteBranch}
                onOpenCitation={onOpenCitation}
                onPickFollowUp={onPickFollowUp}
                messageActions={messageActions}
                onAction={onAction}
                onContinue={onContinue}
                onInterruptSubmit={onInterruptSubmit}
                conversationStreaming={messages.some((x) => x.streaming)}
                isLatestAssistant={m.role === "assistant" && m.id === latestAssistantId}
                questionAttachmentBasis={m.role === "assistant" ? questionAttachmentBasis(messages, idx) : "none"}
                onCiteDocument={onCiteDocument}
                showRawReasoning={showRawReasoning}
                followUpsEnabled={followUpsEnabled}
              />
            </React.Fragment>
          ))
        )}
      </div>
    </div>
  );
}
