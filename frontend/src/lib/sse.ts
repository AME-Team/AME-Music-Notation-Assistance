import { getBackendInfo } from "./backendInfo";

export interface JobProgressEvent {
  job_id: string;
  status: "running" | "succeeded" | "failed" | "cancelled";
  stage?: string;
  progress?: number;
  message?: string;
}

/**
 * ブラウザ標準の `EventSource` はカスタムヘッダ(認証トークン)を送れないため、
 * `fetch` + `ReadableStream` で `text/event-stream` を自前で読む(§11.4)。
 * 接続が予期せず切れた場合は再接続する(SSEの「自動再接続」を手動で再現)。
 */
export function subscribeJobEvents(
  jobId: string,
  onEvent: (event: JobProgressEvent) => void,
  onError?: (error: unknown) => void,
): () => void {
  const controller = new AbortController();
  let stopped = false;
  const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

  async function connectOnce(): Promise<boolean> {
    const { baseUrl, token } = await getBackendInfo();
    const headers = new Headers();
    if (token) headers.set("X-AME-Token", token);

    const resp = await fetch(`${baseUrl}/api/jobs/${jobId}/events`, {
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
        const event = JSON.parse(dataLine.slice("data: ".length)) as JobProgressEvent;
        onEvent(event);
        if (TERMINAL.has(event.status)) return true;
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
