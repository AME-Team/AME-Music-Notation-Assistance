import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useRef } from "react";
import type { AgentEvent } from "../lib/sse";
import { type AgentRunStatus, useAgentRunStore } from "../stores/agentRunStore";

interface AgentConsoleProps {
  runId: string;
  onDismiss: () => void;
}

const STATUS_LABEL: Record<AgentRunStatus, string> = {
  queued: "待機中",
  running: "実行中",
  completed: "完了",
  failed: "失敗",
  cancelled: "キャンセル済み",
  truncated: "上限到達(部分完了)",
};

const STATUS_BADGE: Record<AgentRunStatus, string> = {
  queued:
    "bg-gray-50 dark:bg-gray-900 text-gray-600 dark:text-gray-300 border-gray-200 dark:border-gray-700",
  running:
    "bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300 border-blue-200 dark:border-blue-900",
  completed:
    "bg-emerald-50 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-300 border-emerald-200",
  failed:
    "bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-300 border-red-200 dark:border-red-900",
  cancelled:
    "bg-gray-50 dark:bg-gray-900 text-gray-500 dark:text-gray-400 border-gray-200 dark:border-gray-700",
  truncated: "bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300 border-amber-200",
};

interface Violation {
  rule: string;
  note_id: number | null;
  message: string;
}

function isViolation(value: unknown): value is Violation {
  return typeof value === "object" && value !== null && "rule" in value && "message" in value;
}

/**
 * `score_validate`(配列を直接返す)/`score_apply_ops`(`{ok, violations}`を返す)の
 * どちらの出力形でも「検証違反」を拾えるようにする(§8.4, `domain/invariants.py`の
 * `Violation`形状 `{rule, note_id, message}`)。それ以外のツール出力はスキップする。
 */
function extractViolations(output: unknown): Violation[] {
  if (Array.isArray(output)) {
    return output.filter(isViolation);
  }
  if (typeof output === "object" && output !== null && "violations" in output) {
    const violations = (output as { violations: unknown }).violations;
    return Array.isArray(violations) ? violations.filter(isViolation) : [];
  }
  return [];
}

function summarize(value: unknown, maxLength = 200): string {
  const text = JSON.stringify(value);
  if (text === undefined) return "";
  return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text;
}

/** #51 AgentConsole: 1件の`AgentEvent`をkindに応じて描画する(§12.3)。 */
function EventRow({ event }: { event: AgentEvent }) {
  if (event.kind === "thinking") {
    const text = typeof event.payload.text === "string" ? event.payload.text : "";
    return (
      <details className="rounded-md border border-gray-100 bg-gray-50 dark:bg-gray-900 px-3 py-1.5 text-xs text-gray-500 dark:text-gray-400">
        <summary className="cursor-pointer select-none">思考 ({text.length}文字)</summary>
        <p className="mt-1 whitespace-pre-wrap font-mono text-gray-600 dark:text-gray-300">
          {text}
        </p>
      </details>
    );
  }

  if (event.kind === "text") {
    const text = typeof event.payload.text === "string" ? event.payload.text : "";
    return (
      <p className="rounded-md border border-indigo-100 bg-indigo-50/40 px-3 py-1.5 text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap">
        {text}
      </p>
    );
  }

  if (event.kind === "tool_use") {
    return (
      <div className="rounded-md border border-gray-200 dark:border-gray-700 px-3 py-1.5 text-xs">
        <div className="flex items-center gap-2">
          <span className="rounded bg-gray-100 dark:bg-gray-800 px-1.5 py-0.5 font-mono font-semibold text-gray-700 dark:text-gray-200">
            {event.tool_name ?? "?"}
          </span>
          <span className="text-gray-400 dark:text-gray-500">呼び出し</span>
        </div>
        <pre className="mt-1 overflow-x-auto font-mono text-gray-500 dark:text-gray-400">
          {summarize(event.payload.input)}
        </pre>
      </div>
    );
  }

  if (event.kind === "tool_result") {
    const isError = event.payload.is_error === true;
    const violations = extractViolations(event.payload.output);
    const hasIssue = isError || violations.length > 0;
    return (
      <div
        className={`rounded-md border px-3 py-1.5 text-xs ${
          hasIssue
            ? "border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950"
            : "border-gray-200 dark:border-gray-700"
        }`}
      >
        <div className="flex items-center gap-2">
          <span
            className={`rounded px-1.5 py-0.5 font-mono font-semibold ${
              hasIssue
                ? "bg-red-100 dark:bg-red-900 text-red-700 dark:text-red-300"
                : "bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-200"
            }`}
          >
            {event.tool_name ?? "?"}
          </span>
          <span
            className={
              hasIssue ? "text-red-600 dark:text-red-400" : "text-gray-400 dark:text-gray-500"
            }
          >
            {isError ? "エラー" : violations.length > 0 ? "検証違反あり" : "結果"}
          </span>
        </div>
        {violations.length > 0 ? (
          <ul className="mt-1 list-inside list-disc space-y-0.5 font-mono text-red-700 dark:text-red-300">
            {violations.map((v, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: violationsは同一runでも重複しうる安定IDを持たない
              <li key={i}>
                [{v.rule}] note {v.note_id ?? "-"}: {v.message}
              </li>
            ))}
          </ul>
        ) : (
          <pre className="mt-1 overflow-x-auto font-mono text-gray-500 dark:text-gray-400">
            {summarize(event.payload.output)}
          </pre>
        )}
      </div>
    );
  }

  // done / error / cancelled: 終端イベント
  const status = typeof event.payload.status === "string" ? event.payload.status : event.kind;
  const stagedOpsCount =
    typeof event.payload.staged_ops_count === "number" ? event.payload.staged_ops_count : null;
  const errorMessage = typeof event.payload.error === "string" ? event.payload.error : null;
  return (
    <div
      className={`rounded-md border px-3 py-2 text-sm font-medium ${
        event.kind === "error"
          ? "border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-300"
          : event.kind === "cancelled"
            ? "border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 text-gray-600 dark:text-gray-300"
            : "border-emerald-200 bg-emerald-50 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-300"
      }`}
    >
      run終了: {status}
      {stagedOpsCount !== null && (
        <span className="ml-2 font-normal">(ステージ済み変更: {stagedOpsCount}件)</span>
      )}
      {errorMessage && <p className="mt-1 font-normal">{errorMessage}</p>}
    </div>
  );
}

/**
 * #51: `AgentEvent`のSSEをライブ表示するコンソール(FR-20, §12.3, §12.5)。
 *
 * `thinking`は既定で折りたたみ、`tool_use`/`tool_result`はツール名+引数要約の
 * ツリー表示、検証違反は赤字強調。1 runで数千イベントに達しても劣化しないよう
 * `@tanstack/react-virtual`でビューポート外の行を描画しない(§12.5)。
 */
export function AgentConsole({ runId, onDismiss }: AgentConsoleProps) {
  const track = useAgentRunStore((s) => s.track);
  const run = useAgentRunStore((s) => s.runs[runId]);
  const cancel = useAgentRunStore((s) => s.cancel);
  const dismiss = useAgentRunStore((s) => s.dismiss);

  // Gate2レビュー指摘(MIDDLE): このコンポーネントがアンマウントされても(親が
  // onDismissでactiveAgentRunIdをnullにした場合を含む)track()が購読した
  // SSE接続とrunsストアのイベント履歴(仮想スクロール前提で数千件になりうる)が
  // 解放されないままだった。runIdの変化またはアンマウント時に必ずdismissし、
  // unsubscribers/runsエントリを破棄する。
  useEffect(() => {
    track(runId);
    return () => {
      dismiss(runId);
    };
  }, [runId, track, dismiss]);

  const parentRef = useRef<HTMLDivElement>(null);
  // 新規イベント到着時、ユーザーが末尾付近を見ている間だけ自動追従する
  // (途中までスクロールして読んでいる最中に強制的に最下部へ飛ばさない)。
  const isNearBottomRef = useRef(true);

  const events = run?.events ?? [];
  const status = run?.status ?? "queued";
  const isTerminal = status !== "running" && status !== "queued";

  const virtualizer = useVirtualizer({
    count: events.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 56,
    overscan: 8,
  });

  // events.lengthの変化のみをトリガーにする(virtualizerは安定参照ではないため依存配列には含めない)。
  // biome-ignore lint/correctness/useExhaustiveDependencies: virtualizerは意図的に依存から除外
  useEffect(() => {
    if (events.length === 0) return;
    if (isNearBottomRef.current) {
      virtualizer.scrollToIndex(events.length - 1, { align: "end" });
    }
  }, [events.length]);

  function handleScroll() {
    const el = parentRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    isNearBottomRef.current = distanceFromBottom < 80;
  }

  async function handleCancel() {
    await cancel(runId);
  }

  return (
    <section className="space-y-3 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold text-gray-700 dark:text-gray-200">AgentConsole</h3>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            エージェントの実行過程をリアルタイムに表示します(設計書§4.1 FR-20, R-14)
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`rounded border px-2 py-0.5 text-xs font-semibold ${STATUS_BADGE[status]}`}
          >
            {STATUS_LABEL[status]}
          </span>
          <span className="rounded bg-indigo-50 px-2 py-0.5 text-xs font-mono text-indigo-700 border border-indigo-200">
            Run ID: {runId}
          </span>
          {!isTerminal && (
            <button
              type="button"
              onClick={() => void handleCancel()}
              className="rounded-md bg-red-50 dark:bg-red-950 px-3 py-1 text-xs font-medium text-red-700 dark:text-red-300 border border-red-200 dark:border-red-900 hover:bg-red-100 dark:hover:bg-red-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-red-500"
            >
              キャンセル
            </button>
          )}
          {isTerminal && (
            <button
              type="button"
              onClick={onDismiss}
              className="rounded-md px-2 py-1 text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-gray-400"
            >
              閉じる
            </button>
          )}
        </div>
      </div>

      {run?.error && (
        <p className="rounded-md border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950 px-3 py-2 text-sm text-red-700 dark:text-red-300">
          {run.error}
        </p>
      )}

      {events.length === 0 ? (
        <p className="text-sm text-gray-500 dark:text-gray-400">イベントを待機しています...</p>
      ) : (
        <div
          ref={parentRef}
          onScroll={handleScroll}
          className="max-h-96 overflow-y-auto rounded-md border border-gray-100 bg-white dark:bg-gray-900"
        >
          <div
            style={{
              height: virtualizer.getTotalSize(),
              position: "relative",
              width: "100%",
            }}
          >
            {virtualizer.getVirtualItems().map((virtualRow) => (
              <div
                key={virtualRow.key}
                data-index={virtualRow.index}
                ref={virtualizer.measureElement}
                style={{
                  position: "absolute",
                  top: 0,
                  left: 0,
                  width: "100%",
                  transform: `translateY(${virtualRow.start}px)`,
                }}
                className="px-2 py-1"
              >
                <EventRow event={events[virtualRow.index]} />
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
