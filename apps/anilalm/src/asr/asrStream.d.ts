/**
 * `asrStream.js` 的型別宣告。
 *
 * 為什麼核心是 .js 而不是 .ts:那個檔在 anilalm 與 anila-shell 各有一份相同
 * 副本,而 anila-shell **沒有 TypeScript**(純 jsx + vitest)。寫成 .js 才能兩
 * 邊共用同一份;anilalm 這邊靠本檔拿型別(tsconfig 沒開 allowJs)。
 * 改 asrStream.js 的介面時,這裡與 anila-shell 那份副本都要跟著改。
 */

export const CLOSE_AUTH_FAILED: 4401
export const CLOSE_SESSION_TIMEOUT: 4408
export const CLOSE_CONCURRENCY: 4409
export const CLOSE_AUTH_UNAVAILABLE: 4503

export function describeClose(code: number): string | null

export function makeResampler(
  fromRate: number,
  toRate: number
): (input: Float32Array) => Float32Array

export function floatToInt16(float32: Float32Array): Int16Array

export type AsrState = 'idle' | 'requesting' | 'listening' | 'recording'

export interface AsrSessionOptions {
  onFinal?: (text: string) => void
  onPartial?: (text: string) => void
  onPartialClear?: () => void
  onState?: (state: AsrState) => void
  onError?: (message: string) => void
  url?: string
}

export interface AsrSession {
  start(): Promise<void>
  stop(): void
  dispose(): void
  isRecording(): boolean
}

export function createAsrSession(options: AsrSessionOptions): AsrSession

export function appendTranscript(draft: string, addition: string): string

export function probeAsrAvailable(): Promise<boolean>
