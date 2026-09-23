import { getBackendInfo } from "./backendInfo";

export interface JobProgressEvent {
  job_id: string;
  status: "running" | "succeeded" | "failed" | "cancelled";
  stage?: string;
  progress?: number;
  message?: string;
  /**
   * UI刷新: `backend/app/worker/dsp_main.py`が中間進捗で送る、機械可読な
   * 工程キー(例: "piano"/"bass"/"save")。`lib/jobSteps.ts`が日本語ラベルに
   * 変換する。無い場合(separate/beat/dummy等の未対応ステージ、または
   * 開始/終了イベント)は`step_index`/`step_total`も含めて省略される。
   */
  step?: string;
  step_index?: number;
  step_total?: number;
}

/** #49: `AgentEvent`のSSEペイロード形状(§11.4)。`kind`により`payload`の中身が変わる。 */
export type AgentEventKind =
  | "thinking"
  | "text"
  | "tool_use"
  | "tool_result"
  | "error"
  | "cancelled"
  | "done";

export interface AgentEvent {
  run_id: string;
  seq: number;
  kind: AgentEventKind;
  tool_name: string | null;
  payload: Record<string, unknown>;
  usage?: { input_tokens: number; output_tokens: number } & Record<string, number>;
}

const JOB_TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
const AGENT_TERMINAL = new Set<AgentEventKind>(["done", "error", "cancelled"]);

/**
 * ブラウザ標準の `EventSource` はカスタムヘッダ(認証トークン)を送れないため、
 * `fetch` + `ReadableStream` で `text/event-stream` を自前で読む(§11.4)。
 * 接続が予期せず切れた場合は再接続する(SSEの「自動再接続」を手動で再現)。
 *
 * `event: <name>`行は無視し`data: `行のみをJSONとして解釈する — job/agentの
 * どちらのSSEも`data: `行にペイロード全体が乗る形式で揃っているため、
 * イベント名の判定は呼び出し元の型パラメータ(ジェネリクス)にのみ依存する。
 */
function subscribeSSE<T>(
  path: string,
  isTerminal: (event: T) => boolean,
  onEvent: (event: T) => void,
  onError?: (error: unknown) => void,
): () => void {
  const controller = new AbortController();
  let stopped = false;

  async function connectOnce(): Promise<boolean> {
    const { baseUrl, token } = await getBackendInfo();
    const headers = new Headers();
    if (token) headers.set("X-AME-Token", token);

    const resp = await fetch(`${baseUrl}${path}`, {
      headers,
      signal: controller.signal,
    });
    if (!resp.ok || !resp.body) {
      throw new Error(`SSE connect failed: ${resp.status}`);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      while (true) {
        const sepIndex = buffer.indexOf("\n\n");
        if (sepIndex === -1) break;
        const rawEvent = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        const dataLine = rawEvent.split("\n").find((line) => line.startsWith("data: "));
        if (!dataLine) continue;
        const event = JSON.parse(dataLine.slice("data: ".length)) as T;
        onEvent(event);
        if (isTerminal(event)) return true;
      }
    }
    return false;
  }

  (async () => {
    while (!stopped) {
      try {
        const finished = await connectOnce();
        if (finished || stopped) return;
        // ストリームが完了状態イベント無しに切れた: 再接続する。
        await new Promise((r) => setTimeout(r, 1000));
      } catch (err) {
        if (stopped) return;
        onError?.(err);
        await new Promise((r) => setTimeout(r, 1000));
      }
    }
  })();

  return () => {
    stopped = true;
    controller.abort();
  };
}

export function subscribeJobEvents(
  jobId: string,
  onEvent: (event: JobProgressEvent) => void,
  onError?: (error: unknown) => void,
): () => void {
  return subscribeSSE<JobProgressEvent>(
    `/api/jobs/${jobId}/events`,
    (event) => JOB_TERMINAL.has(event.status),
    onEvent,
    onError,
  );
}

/** #51 AgentConsole向け。終端は`kind`が`done`/`error`/`cancelled`のいずれか(§11.3/§11.4)。 */
export function subscribeAgentEvents(
  runId: string,
  onEvent: (event: AgentEvent) => void,
  onError?: (error: unknown) => void,
): () => void {
  return subscribeSSE<AgentEvent>(
    `/api/agent/runs/${runId}/events`,
    (event) => AGENT_TERMINAL.has(event.kind),
    onEvent,
    onError,
  );
}
