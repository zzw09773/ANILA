// 產物預覽的 React context — 獨立檔案，避免 artifact.jsx ↔ markdown.jsx 循環依賴。
import React, { createContext, useContext, useMemo } from "react";

const ArtifactPreviewContext = createContext(null);

/**
 * @param {{ children: React.ReactNode, onOpen: (artifact: {kind:string, source:string, language?:string}) => void }} props
 */
export function ArtifactPreviewProvider({ children, onOpen }) {
  const value = useMemo(() => ({ openArtifact: onOpen }), [onOpen]);
  return (
    <ArtifactPreviewContext.Provider value={value}>
      {children}
    </ArtifactPreviewContext.Provider>
  );
}

/** @returns {{ openArtifact: (a: {kind:string, source:string, language?:string}) => void } | null} */
export function useArtifactPreview() {
  return useContext(ArtifactPreviewContext);
}
