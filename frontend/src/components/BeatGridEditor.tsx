import { type ReactNode, useState } from "react";
import type { Beatmap } from "../api/client";
import { usePatchBeatmap } from "../hooks/useBeatmap";
import {
  bpmOverrideIntervalSec,
  currentBeatIntervalSec,
  currentMeterLabel,
  offsetPreview,
} from "../lib/beatEditPreview";
import { formatBpm, formatSeconds, summarizeBeatmap } from "../lib/beatSummary";

interface BeatGridEditorProps {
  projectId: string;
  beatmap: Beatmap;
}

const DENOMINATOR_CHOICES = [1, 2, 4, 8, 16, 32] as const;

/**
 * 全体オフセットの微調整量(ms)。拡大した波形を見ながら少しずつ寄せる用途を想定し、
 * 数値入力(例: 0.25)を計算しなくても済むようにする。押すたびに`offset_sec`の
 * 差分として現在のビート時刻へ加算される(§6/#20)。
 */
const OFFSET_NUDGE_STEPS_MS = [-50, -10, 10, 50] as const;

const SOURCE_LABEL: Record<string, string> = {
  auto: "自動推定",
  manual: "手動補正済み",
};

const INPUT_CLASS =
  "rounded-md border border-gray-300 dark:border-gray-600 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500";
const SUBMIT_CLASS =
  "rounded-md bg-gray-800 px-3 py-1.5 text-sm font-medium text-white hover:bg-gray-900 disabled:opacity-50";
const SECONDARY_BUTTON_CLASS =
  "rounded-md border border-gray-300 px-2 py-1 text-xs text-gray-700 hover:bg-gray-100 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800";

/** 現在値を表す1項目(見出しと値を並べる)。 */
function CurrentItem({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-baseline gap-1">
      <span>{label}</span>
      <span className="text-sm font-semibold text-gray-800 dark:text-gray-100">{value}</span>
    </span>
  );
}

/**
 * 補正項目の1ブロック。**現在値を操作のすぐ上に出す**のがこのブロックの役割
 * (#171)。以前はフォームしか無く、いま何オフセットが効いているのか、拍子が何に
 * なっているのかを画面から読み取れなかった。
 */
function EditBlock({
  title,
  current,
  children,
}: {
  title: string;
  current: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="space-y-2 rounded-md border border-gray-200 p-3 dark:border-gray-700">
      <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-200">{title}</h4>
      <p className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
        <span className="rounded bg-gray-100 px-1.5 py-0.5 font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-300">
          現在
        </span>
        {current}
      </p>
      {children}
    </section>
  );
}

/** #20 FR-04: ビート推定結果に対する手動補正フォーム(オフセット/BPM/回転/拍子)。 */
export function BeatGridEditor({ projectId, beatmap }: BeatGridEditorProps) {
  const patch = usePatchBeatmap(projectId);
  const summary = summarizeBeatmap(beatmap);

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

  const offsetInput = Number(offsetSec);
  const offsetAfter = offsetSec.trim() === "" ? null : offsetPreview(summary, offsetInput);
  const bpmInput = Number(bpm);
  const bpmAfter = bpm.trim() === "" ? null : bpmOverrideIntervalSec(bpmInput);
  const currentInterval = currentBeatIntervalSec(summary);

  const errorMessage =
    patch.error instanceof Error ? patch.error.message : patch.error ? String(patch.error) : null;

  return (
    <section className="space-y-4 rounded-lg border border-gray-200 p-4 dark:border-gray-700">
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-semibold text-gray-700 dark:text-gray-200">
          ビートグリッド補正
        </h3>
        <span className="rounded-md bg-gray-100 px-2 py-1 text-xs text-gray-600 dark:bg-gray-800 dark:text-gray-300">
          {SOURCE_LABEL[beatmap.source] ?? beatmap.source} · 信頼度{" "}
          {Math.round(beatmap.confidence * 100)}%
        </span>
      </div>

      <EditBlock
        title="全体オフセット(ビート全体を前後にずらす)"
        current={
          <>
            <CurrentItem label="先頭の拍" value={formatSeconds(summary.firstBeatSec)} />
            <CurrentItem label="最後の拍" value={formatSeconds(summary.lastBeatSec)} />
            <CurrentItem label="平均拍間隔" value={formatSeconds(summary.beatIntervalSec)} />
          </>
        }
      >
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (!Number.isFinite(offsetInput)) return;
            patch.mutate(
              { offset_sec: offsetInput },
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
          <label className="flex flex-col gap-1 text-sm text-gray-600 dark:text-gray-300">
            ずらす量(秒・正で後ろへ)
            <input
              type="number"
              step="0.01"
              placeholder="例: 0.25"
              value={offsetSec}
              onChange={(event) => setOffsetSec(event.target.value)}
              className={`w-32 ${INPUT_CLASS}`}
            />
          </label>
          <button
            type="submit"
            disabled={patch.isPending || offsetSec.trim() === ""}
            className={SUBMIT_CLASS}
          >
            適用
          </button>
          {offsetAfter && (
            <span className="pb-1 text-xs text-gray-600 dark:text-gray-300">
              適用後: 先頭の拍{" "}
              <span className="font-semibold">{formatSeconds(offsetAfter.firstBeatSec)}</span>
              /最後の拍{" "}
              <span className="font-semibold">{formatSeconds(offsetAfter.lastBeatSec)}</span>
            </span>
          )}
        </form>
        <div className="flex flex-wrap items-center gap-1">
          <span className="text-xs text-gray-500 dark:text-gray-400">微調整(押すたびに加算)</span>
          {OFFSET_NUDGE_STEPS_MS.map((ms) => (
            <button
              key={ms}
              type="button"
              disabled={patch.isPending}
              onClick={() => patch.mutate({ offset_sec: ms / 1000 })}
              className={SECONDARY_BUTTON_CLASS}
            >
              {ms > 0 ? `+${ms}` : ms} ms
            </button>
          ))}
        </div>
      </EditBlock>

      <EditBlock
        title="固定BPMへ上書き(テンポが動く曲を一定にする)"
        current={
          <>
            <CurrentItem label="代表テンポ" value={`${formatBpm(summary.tempoBpm)} BPM`} />
            <CurrentItem label="拍間隔" value={formatSeconds(currentInterval)} />
            <CurrentItem
              label="検出したテンポの幅"
              value={
                summary.tempoMinBpm === null || summary.tempoMaxBpm === null
                  ? "—"
                  : `${formatBpm(summary.tempoMinBpm)}〜${formatBpm(summary.tempoMaxBpm)} BPM`
              }
            />
          </>
        }
      >
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (Number.isFinite(bpmInput) && bpmInput > 0) patch.mutate({ bpm_override: bpmInput });
          }}
        >
          <label className="flex flex-col gap-1 text-sm text-gray-600 dark:text-gray-300">
            上書きするBPM
            <input
              type="number"
              step="0.1"
              min="0"
              placeholder="例: 120"
              value={bpm}
              onChange={(event) => setBpm(event.target.value)}
              className={`w-32 ${INPUT_CLASS}`}
            />
          </label>
          <button type="submit" disabled={patch.isPending || !bpm} className={SUBMIT_CLASS}>
            適用
          </button>
          {bpmAfter && (
            <span className="pb-1 text-xs text-gray-600 dark:text-gray-300">
              適用後: 全ての拍間隔が{" "}
              <span className="font-semibold">{formatSeconds(bpmAfter)}</span> になる
            </span>
          )}
        </form>
      </EditBlock>

      <EditBlock
        title="ダウンビートの位置(小節の頭)"
        current={
          <>
            <CurrentItem label="ダウンビート" value={`${summary.downbeatCount} 箇所`} />
            <CurrentItem
              label="先頭のダウンビート"
              value={formatSeconds(
                beatmap.downbeats_sec.length > 0 ? Math.min(...beatmap.downbeats_sec) : null,
              )}
            />
            <CurrentItem label="拍の数" value={`${summary.beatCount} 拍`} />
          </>
        }
      >
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            disabled={patch.isPending}
            onClick={() => patch.mutate({ rotate_downbeat: true })}
            className="rounded-md bg-gray-100 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-200 disabled:opacity-50 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            ダウンビートを1拍分ずらす
          </button>
          <span className="text-xs text-gray-500 dark:text-gray-400">
            小節の頭が1拍ずれているときに使います(波形の小節線が変わります)
          </span>
        </div>
      </EditBlock>

      <EditBlock
        title="小節番号と拍子"
        current={
          <>
            <CurrentItem label="拍子" value={currentMeterLabel(summary)} />
            <CurrentItem
              label="小節"
              value={summary.barCount > 0 ? `1〜${summary.barCount}` : "—"}
            />
            <CurrentItem
              label="1小節あたり"
              value={
                summary.timeSignature ? `${Number(summary.timeSignature.split("/")[0])} 拍` : "—"
              }
            />
          </>
        }
      >
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            const bar = Number(tsBar);
            const numerator = Number(tsNumerator);
            if (!Number.isInteger(bar) || bar < 1) return;
            if (!Number.isInteger(numerator) || numerator < 1) return;
            patch.mutate({
              time_signature_override: {
                bar,
                numerator,
                denominator: tsDenominator,
              },
            });
          }}
        >
          <label className="flex flex-col gap-1 text-sm text-gray-600 dark:text-gray-300">
            この小節から
            <input
              type="number"
              min="1"
              step="1"
              value={tsBar}
              onChange={(event) => setTsBar(event.target.value)}
              className={`w-20 ${INPUT_CLASS}`}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm text-gray-600 dark:text-gray-300">
            拍子
            <div className="flex items-center gap-1">
              <input
                type="number"
                min="1"
                step="1"
                value={tsNumerator}
                onChange={(event) => setTsNumerator(event.target.value)}
                className={`w-16 ${INPUT_CLASS}`}
              />
              <span className="text-gray-400 dark:text-gray-500">/</span>
              <select
                value={tsDenominator}
                onChange={(event) =>
                  setTsDenominator(
                    Number(event.target.value) as (typeof DENOMINATOR_CHOICES)[number],
                  )
                }
                className={INPUT_CLASS}
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
            className={SUBMIT_CLASS}
          >
            この小節以降に適用
          </button>
          <span className="pb-1 text-xs text-gray-600 dark:text-gray-300">
            適用後: 小節 <span className="font-semibold">{tsBar || "—"}</span> 以降が{" "}
            <span className="font-semibold">
              {tsNumerator || "—"}/{tsDenominator}
            </span>
          </span>
        </form>
      </EditBlock>

      {errorMessage && <p className="text-sm text-red-600 dark:text-red-400">{errorMessage}</p>}
    </section>
  );
}
