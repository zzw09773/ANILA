// 產物預覽的 React context — 獨立檔案，避免 artifact.jsx ↔ markdown.jsx 循環依賴。
import React, { createContext, useContext, useMemo } from "react";

const ArtifactPreviewContext = createContext(null);

/**
 * @param {{
 *   children: React.ReactNode,
 *   onOpen: (artifact: {kind:string, source:string, language?:string}) => void,
 *   artifact?: {kind:string, source:string, language?:string} | null,
 * }} props
 */
export function ArtifactPreviewProvider({ children, onOpen, artifact = null }) {
  const value = useMemo(
    () => ({ openArtifact: onOpen, current: artifact }),
    [onOpen, artifact],
  );
  return (
    <ArtifactPreviewContext.Provider value={value}>
      {children}
    </ArtifactPreviewContext.Provider>
  );
}

/** @returns {{ openArtifact: Function, current: object|null } | null} */
export function useArtifactPreview() {
  return useContext(ArtifactPreviewContext);
}
