// Gate 2 chat-only pilot UI surface. The flag is compiled into the image by
// the signed-pilot compose override. It is only a presentation boundary; CSP
// remains the authority and rejects every disabled endpoint independently.
const pilotMode = import.meta.env.VITE_ANILA_PILOT_MODE === 'true'

export const gate2PilotCapabilities = Object.freeze({
  studio: !pilotMode,
  artifact: !pilotMode,
  export: !pilotMode,
  flux: !pilotMode,
  promptGenerator: !pilotMode,
  relationLlm: !pilotMode,
  judge: !pilotMode,
  thirdPartyAgents: !pilotMode,
})
