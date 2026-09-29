/** 新登錄模型的同時處理上限。留空仍是不限，既有列不回填。 */
export const DEFAULT_MODEL_MAX_CONCURRENT = 16

/** 模型清單上的處理中／排隊中重抓間隔。 */
export const MODEL_LIST_REFRESH_MS = 15000

/** 有人在排隊才醒目。0 與未知都不標。 */
export function modelQueueHot(queueLength) {
  return Number(queueLength) > 0
}
