import { useEffect, useState } from "react";
import type { RefineEstimateResponse, RefineResponse } from "../api/client";
import { getRefineEstimate, runRefine } from "../api/client";
import { useScore } from "../hooks/useScore";

interface RefineSectionProps {
  projectId: string;
  isQuantizeReady: boolean;
}

/**
 * #40: L1 構造化出力整音 (Batch / Sync) とコスト可視化コンポーネント (NFR-07/R-9)。
 *
 * - HTTPリクエストの長時間ブロッキングを防ぐため、既定で同期・逐次実行 ("sync") を選択。Batch API (50% 割引) も選択可能。
 * - 実行前の想定コスト事前見積もり表示 (§7.5)。
 * - 実行後の消費トークン数・実測コスト・承認/棄却内訳の可視化 (NFR-07)。
 * - 生成された run_id を保持し、将来の DiffPanel (#41) に引き継ぐ。
 */
export function RefineSection({ projectId, isQuantizeReady }: RefineSectionProps) {
  const { data: score } = useScore(projectId);

  const parts = score?.parts ?? [];
  const [selectedPart, setSelectedPart] = useState<string>("piano");
  const [mode, setMode] = useState<"batch" | "sync">("sync");
  const [effort, setEffort] = useState<"high" | "medium">("high");

  // 見積もり状態
  const [estimate, setEstimate] = useState<RefineEstimateResponse | null>(null);
  const [isEstimating, setIsEstimating] = useState(false);
  const [estimateError, setEstimateError] = useState<string | null>(null);

  // 実行状態
  const [isRunning, setIsRunning] = useState(false);
  const [refineResult, setRefineResult] = useState<RefineResponse | null>(null);
  const [refineError, setRefineError] = useState<string | null>(null);

  // パート一覧が読み込まれたらデフォルトを選択
  useEffect(() => {
    if (parts.length > 0 && !parts.some((p) => p.id === selectedPart)) {
      setSelectedPart(parts[0].id);
    }
  }, [parts, selectedPart]);

  // パートやモードが変わるたびに見積もりを再取得
  useEffect(() => {
    if (!isQuantizeReady || !selectedPart) return;

    let active = true;
    setIsEstimating(true);
    setEstimateError(null);

    getRefineEstimate(projectId, selectedPart, mode)
      .then((est) => {
        if (active) setEstimate(est);
      })
      .catch((err: Error) => {
        if (active) setEstimateError(err.message);
      })
      .finally(() => {
        if (active) setIsEstimating(false);
      });

    return () => {
      active = false;
    };
  }, [projectId, selectedPart, mode, isQuantizeReady]);

  async function handleRunRefine() {
    setRefineError(null);
    setIsRunning(true);
    try {
      const resp = await runRefine(projectId, {
        part_id: selectedPart,
        mode,
        effort,
      });
      setRefineResult(resp);
    } catch (err) {
      setRefineError((err as Error).message);
    } finally {
      setIsRunning(false);
    }
  }

  return (
    <section className="space-y-4 rounded-lg border border-gray-200 p-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold text-gray-700">L1 整音 (AI構造化注釈)</h3>
          <p className="text-xs text-gray-500">
            Anthropic Messages API による小節単位の記譜注釈・声部・異名同音の最適化 (設計書§7.3)
          </p>
        </div>
        <span className="rounded bg-indigo-50 px-2 py-0.5 text-xs font-semibold text-indigo-700 border border-indigo-200">
          Stage 6手前 (L1)
        </span>
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1 text-sm text-gray-600">
          対象パート
          <select
            value={selectedPart}
            onChange={(e) => setSelectedPart(e.target.value)}
            disabled={parts.length === 0 || isRunning}
            className="rounded-md border border-gray-300 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
          >
            {parts.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.id})
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-sm text-gray-600">
          実行モード
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value as "batch" | "sync")}
            disabled={isRunning}
            className="rounded-md border border-gray-300 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
          >
            <option value="sync">同期・逐次実行 (即時・既定)</option>
            <option value="batch">Batch API (50%割引・非同期ポーリング)</option>
          </select>
        </label>

        <label className="flex flex-col gap-1 text-sm text-gray-600">
          推論 Effort
          <select
            value={effort}
            onChange={(e) => setEffort(e.target.value as "high" | "medium")}
            disabled={isRunning}
            className="rounded-md border border-gray-300 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
          >
            <option value="high">High (高品質・既定)</option>
            <option value="medium">Medium (低コスト)</option>
          </select>
        </label>

        <button
          type="button"
          onClick={() => void handleRunRefine()}
          disabled={isRunning || !isQuantizeReady || parts.length === 0}
          className="rounded-md bg-indigo-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500 disabled:opacity-50"
        >
          {isRunning
            ? mode === "batch"
              ? "Batch 実行中 (ポーリング中)..."
              : "逐次実行中..."
            : "L1 整音を実行"}
        </button>
      </div>

      {/* 事前コスト見積もり表示 (§7.5) */}
      <div className="rounded-md bg-gray-50 p-3 text-xs text-gray-700 border border-gray-200">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-gray-800">事前見積もり:</span>
          {isEstimating ? (
            <span className="text-gray-500">計算中...</span>
          ) : estimate ? (
            <div className="flex flex-wrap items-center gap-3">
              <span>
                チャンク数: <strong>{estimate.num_chunks}</strong>
              </span>
              <span>
                想定トークン:{" "}
                <strong>
                  {(
                    estimate.estimated_input_tokens + estimate.estimated_output_tokens
                  ).toLocaleString()}
                </strong>
              </span>
              <span className="text-indigo-700 font-semibold">
                想定コスト: 約 ${estimate.estimated_cost_usd.toFixed(4)}
                {mode === "batch" && <span className="ml-1 text-emerald-600">(50%割引適用)</span>}
              </span>
            </div>
          ) : (
            <span className="text-gray-500">量子化完了後に見積もりが表示されます</span>
          )}
        </div>
        {estimateError && <p className="mt-1 text-red-600">{estimateError}</p>}
      </div>

      {refineError && (
        <div className="rounded-md bg-red-50 p-3 text-sm text-red-700 border border-red-200">
          <p className="font-semibold">L1整音に失敗しました</p>
          <p>{refineError}</p>
        </div>
      )}

      {/* 実行結果とコスト可視化カード (NFR-07) */}
      {refineResult && (
        <div className="space-y-2 rounded-md bg-emerald-50 p-4 text-sm text-emerald-900 border border-emerald-200">
          <div className="flex items-center justify-between">
            <span className="font-semibold flex items-center gap-1.5 text-emerald-800">
              ✓ L1 整音完了 (Staging 保存済み)
            </span>
            <span className="rounded bg-emerald-100 px-2 py-0.5 text-xs font-mono text-emerald-800 border border-emerald-300">
              Run ID: {refineResult.run_id}
            </span>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-2 text-xs">
            <div className="rounded bg-white p-2 border border-emerald-100 shadow-sm">
              <span className="text-gray-500 block">承認チャンク</span>
              <span className="text-base font-bold text-emerald-700">{refineResult.chunks_ok}</span>
            </div>
            <div className="rounded bg-white p-2 border border-emerald-100 shadow-sm">
              <span className="text-gray-500 block">棄却チャンク</span>
              <span
                className={`text-base font-bold ${refineResult.chunks_rejected > 0 ? "text-amber-600" : "text-gray-700"}`}
              >
                {refineResult.chunks_rejected}
              </span>
            </div>
            <div className="rounded bg-white p-2 border border-emerald-100 shadow-sm">
              <span className="text-gray-500 block">消費トークン (入/出)</span>
              <span className="text-base font-bold text-gray-800">
                {refineResult.usage.input_tokens.toLocaleString()} /{" "}
                {refineResult.usage.output_tokens.toLocaleString()}
              </span>
            </div>
            <div className="rounded bg-white p-2 border border-emerald-100 shadow-sm">
              <span className="text-gray-500 block">実測コスト (NFR-07)</span>
              <span className="text-base font-bold text-indigo-700">
                ${refineResult.cost_usd.toFixed(4)}
              </span>
            </div>
          </div>

          {refineResult.rejected_reasons.length > 0 && (
            <div className="mt-2 rounded bg-amber-50 p-2 text-xs text-amber-800 border border-amber-200">
              <span className="font-semibold">棄却理由:</span>
              <ul className="list-disc list-inside mt-1 space-y-0.5">
                {refineResult.rejected_reasons.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            </div>
          )}

          <p className="text-xs text-emerald-700 pt-1">
            ※ 整音結果は <code>score/staging/{refineResult.run_id}.json</code> に保存されています。
            DiffPanel (#41) にて差分を確認し、小節単位で承認/却下できます。
          </p>
        </div>
      )}
    </section>
  );
}
