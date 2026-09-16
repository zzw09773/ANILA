// 產物預覽的 React context — 獨立檔案，避免 artifact.jsx ↔ markdown.jsx 循環依賴。
import React, { createContext, useCallback, useContext, useMemo, useRef } from "react";

const ArtifactPreviewContext = createContext(null);

/**
 * @param {{
 *   children: React.ReactNode,
 *   onOpen: (artifact: {kind:string, source:string, language?:string}) => void,
 *   artifact?: {kind:string, source:string, language?:string} | null,
 *   pendingRevision?: {messageId:string, kind:string} | null,
 *   onRevisionSettled?: (messageId: string) => void,
 * }} props
 */
export function ArtifactPreviewProvider({
  children,
  onOpen,
  artifact = null,
  pendingRevision = null,
  onRevisionSettled,
}) {
  const pendingRef = useRef(pendingRevision);
  pendingRef.current = pendingRevision;
  const appliedRef = useRef(null);
  const consumeRevision = useCallback((input = {}) => {
    const ticket = pendingRef.current;
    if (!ticket?.messageId || !ticket?.kind) return false;
    if (input.messageId !== ticket.messageId || input.kind !== ticket.kind) return false;
    const key = `${ticket.messageId}:${ticket.kind}`;
    const len = String(input.source ?? "").length;
    if (appliedRef.current?.key === key && appliedRef.current.len >= len) return false;
    appliedRef.current = { key, len };
    return true;
  }, []);
  const settleRevision = useCallback((messageId) => {
    onRevisionSettled?.(messageId);
  }, [onRevisionSettled]);
  const value = useMemo(
    () => ({
      openArtifact: onOpen,
      current: artifact,
      pendingRevision,
      consumeRevision,
      settleRevision,
    }),
    [onOpen, artifact, pendingRevision, consumeRevision, settleRevision],
  );
  return (
    <ArtifactPreviewContext.Provider value={value}>
      {children}
    </ArtifactPreviewContext.Provider>
  );
}

/** @returns {{ openArtifact: Function, current: object|null, pendingRevision?: object|null, consumeRevision?: Function } | null} */
export function useArtifactPreview() {
  return useContext(ArtifactPreviewContext);
}
