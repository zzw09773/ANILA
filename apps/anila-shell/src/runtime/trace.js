/** 同一輪的召回階段只留一筆。終態更新原本那筆，不另開一筆仍在搜尋的步驟。 */
export function mergeLiveTrace(trace, step) {
  const steps = Array.isArray(trace) ? trace : [];
  if (step && step.kind === "recall") {
    const index = steps.findIndex((item) => item && item.kind === "recall");
    if (index >= 0) {
      const prior = steps[index];
      steps[index] = {
        ...prior,
        ...step,
        at: prior.at ?? step.at,
      };
      return steps;
    }
  }
  steps.push(step);
  return steps;
}
