// Header-only tests still return a valid terminal SSE frame.
export function finishedSseReader() {
  let sent = false;
  return {
    read: async () => {
      if (sent) return { done: true };
      sent = true;
      return { done: false, value: new TextEncoder().encode('data: [DONE]\n\n') };
    },
    cancel: async () => {},
  };
}
