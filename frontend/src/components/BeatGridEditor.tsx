import { useState } from "react";
import type { Beatmap } from "../api/client";
import { usePatchBeatmap } from "../hooks/useBeatmap";

interface BeatGridEditorProps {
  projectId: string;
  beatmap: Beatmap;
}

const DENOMINATOR_CHOICES = [1, 2, 4, 8, 16, 32] as const;

const SOURCE_LABEL: Record<string, string> = {
  auto: "自動推定",
  manual: "手動補正済み",
};

/** #20 FR-04: ビート推定結果に対する手動補正フォーム(オフセット/BPM/回転/拍子)。 */
export function BeatGridEditor({ projectId, beatmap }: BeatGridEditorProps) {
  const patch = usePatchBeatmap(projectId);
  // 空欄始まりにする(#21-M1レビュー指摘): `offset_sec` はbeatmapに保存された
  // 絶対値ではなく、送信するたびに現在のビート時刻へ加算される差分(§6/#20)。
  // "0"で初期化すると、ユーザーが値を意識せず「適用」を押した際に
  // offset_sec=0(no-op)を送ってしまい、何が起きたのか分かりにくい。
  // 空欄なら「適用」ボタン自体を無効化し、誤送信を防ぐ。
  const [offsetSec, setOffsetSec] = useState("");
  const [bpm, setBpm] = useState("");
  const [tsBar, setTsBar] = useState("1");
  const [tsNumerator, setTsNumerator] = useState("4");
  const [tsDenominator, setTsDenominator] = useState<(typeof DENOMINATOR_CHOICES)[number]>(4);

  const errorMessage =
    patch.error instanceof Error ? patch.error.message : patch.error ? String(patch.error) : null;

  return (
    <section className="space-y-4 rounded-lg border border-gray-200 p-4">
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-semibold text-gray-700">ビートグリッド補正</h3>
        <span className="rounded-md bg-gray-100 px-2 py-1 text-xs text-gray-600">
          {SOURCE_LABEL[beatmap.source] ?? beatmap.source} · 信頼度{" "}
          {Math.round(beatmap.confidence * 100)}%
        </span>
      </div>

      <form
        className="flex flex-wrap items-end gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          const value = Number(offsetSec);
          if (!Number.isFinite(value)) return;
          patch.mutate(
            { offset_sec: value },
            {
              // オフセットは「現在のビート時刻へ加算される差分」であり絶対値では
              // ないため(§6/#20)、適用成功後も入力欄に値を残すと、そのまま
              // 再度「適用」を押した際に同じ差分が二重に加算されてしまう
              // (#21-M1レビュー指摘)。成功後は空欄に戻し、「適用」を無効化する
              // 既存の空欄ガードへ回帰させることで、次の適用を明確な新規補正
              // として扱う。
              onSuccess: () => setOffsetSec(""),
            },
          );
        }}
      >
        <label className="flex flex-col gap-1 text-sm text-gray-600">
          全体オフセット(秒)
          <input
            type="number"
            step="0.01"
            placeholder="例: 0.25"
            value={offsetSec}
            onChange={(event) => setOffsetSec(event.target.value)}
            className="w-28 rounded-md border border-gray-300 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
          />
        </label>
        <button
          type="submit"
          disabled={patch.isPending || offsetSec.trim() === ""}
          className="rounded-md bg-gray-800 px-3 py-1.5 text-sm font-medium text-white hover:bg-gray-900 disabled:opacity-50"
        >
          適用
        </button>
      </form>

      <form
        className="flex flex-wrap items-end gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          const value = Number(bpm);
          if (Number.isFinite(value) && value > 0) patch.mutate({ bpm_override: value });
        }}
      >
        <label className="flex flex-col gap-1 text-sm text-gray-600">
          固定BPMへ上書き
          <input
            type="number"
            step="0.1"
            min="0"
            placeholder="例: 120"
            value={bpm}
            onChange={(event) => setBpm(event.target.value)}
            className="w-28 rounded-md border border-gray-300 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
          />
        </label>
        <button
          type="submit"
          disabled={patch.isPending || !bpm}
          className="rounded-md bg-gray-800 px-3 py-1.5 text-sm font-medium text-white hover:bg-gray-900 disabled:opacity-50"
        >
          適用
        </button>
      </form>

      <div className="flex flex-wrap items-end gap-4">
        <button
          type="button"
          disabled={patch.isPending}
          onClick={() => patch.mutate({ rotate_downbeat: true })}
          className="rounded-md bg-gray-100 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-200 disabled:opacity-50"
        >
          ダウンビートを1拍分ずらす
        </button>
      </div>

      <form
        className="flex flex-wrap items-end gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          const bar = Number(tsBar);
          const numerator = Number(tsNumerator);
          if (!Number.isInteger(bar) || bar < 1) return;
          if (!Number.isInteger(numerator) || numerator < 1) return;
          patch.mutate({
            time_signature_override: { bar, numerator, denominator: tsDenominator },
          });
        }}
      >
        <label className="flex flex-col gap-1 text-sm text-gray-600">
          小節番号
          <input
            type="number"
            min="1"
            step="1"
            value={tsBar}
            onChange={(event) => setTsBar(event.target.value)}
            className="w-20 rounded-md border border-gray-300 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm text-gray-600">
          拍子
          <div className="flex items-center gap-1">
            <input
              type="number"
              min="1"
              step="1"
              value={tsNumerator}
              onChange={(event) => setTsNumerator(event.target.value)}
              className="w-16 rounded-md border border-gray-300 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
            />
            <span className="text-gray-400">/</span>
            <select
              value={tsDenominator}
              onChange={(event) =>
                setTsDenominator(Number(event.target.value) as (typeof DENOMINATOR_CHOICES)[number])
              }
              className="rounded-md border border-gray-300 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
            >
              {DENOMINATOR_CHOICES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </div>
        </label>
        <button
          type="submit"
          disabled={
            patch.isPending ||
            !Number.isInteger(Number(tsBar)) ||
            Number(tsBar) < 1 ||
            !Number.isInteger(Number(tsNumerator)) ||
            Number(tsNumerator) < 1
          }
          className="rounded-md bg-gray-800 px-3 py-1.5 text-sm font-medium text-white hover:bg-gray-900 disabled:opacity-50"
        >
          この小節以降に適用
        </button>
      </form>

      {errorMessage && <p className="text-sm text-red-600">{errorMessage}</p>}
    </section>
  );
}
