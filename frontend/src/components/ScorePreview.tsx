import { OpenSheetMusicDisplay } from "opensheetmusicdisplay";
import { useEffect, useRef, useState } from "react";
import type { ScoreIR } from "../api/client";
import { getScorePreviewMusicXml } from "../api/client";
import { barBoundariesTicks, barNumberForTick } from "../lib/pianoRoll";

interface ScorePreviewProps {
  projectId: string;
  score: ScoreIR;
  selectedNoteIds: ReadonlySet<number>;
}

const DEBOUNCE_MS = 500; // NFR-03: 編集後500ms以内にプレビューが更新される
const AUTO_FOLLOW_WINDOW_BARS = 4; // 選択ノートの前後何小節を表示範囲に含めるか

const INPUT_LABEL_CLASS = "flex flex-col gap-1 text-sm text-gray-600";
const INPUT_CLASS =
  "w-20 rounded-md border border-gray-300 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500";

/**
 * #33: OSMDによる楽譜プレビュー(FR-11/NFR-03)。
 *
 * OSMDインスタンスはマウント時に1回だけ生成し(`osmdRef`)、`score`propの
 * 変更を500msデバウンスしてから`GET /score/preview.musicxml`を再取得→
 * `osmd.load()`→`render()`する(NFR-03: 「編集中は再描画せず操作停止500ms後に
 * まとめて更新する」、設計書§12.5)。
 *
 * 表示範囲(小節)は2通りの経路で決まる: (1) `fromBar`/`toBar`入力欄
 * (両方空欄なら手動指定なし)、(2) 手動指定が無い場合、ピアノロールで
 * ちょうど1件選択されているノートの小節を中心とした自動追従(#33設計:
 * 「ピアノロールとのカーソル連動」を、#34(TransportBar)が担う再生位置ベース
 * の連動とは別に、選択ベースの簡易版として実装する)。
 *
 * OSMDには明示的なdispose APIが無いため、このコンポーネントが再マウントされる
 * (親`PianoRollEditor`は`key={projectId}`でプロジェクトごとに再マウントする)
 * たびに新しいOSMDインスタンス+`autoResize`のwindowリスナーが作られる。
 * ライブラリ側にクリーンアップ手段が無い既知の制約として許容する(1回の
 * セッションでプロジェクトを何度も切り替えると緩やかにリークしうるが、
 * 実害が出るほどではないと判断)。
 */
export function ScorePreview({ projectId, score, selectedNoteIds }: ScorePreviewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const osmdRef = useRef<OpenSheetMusicDisplay | null>(null);
  const loadedRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [manualFromBar, setManualFromBar] = useState("");
  const [manualToBar, setManualToBar] = useState("");

  useEffect(() => {
    if (!containerRef.current) return;
    osmdRef.current = new OpenSheetMusicDisplay(containerRef.current, {
      autoResize: true,
      drawTitle: false,
    });
  }, []);

  // NFR-03: scoreが変わるたびにタイマーをリセットし、500ms操作が無ければ
  // まとめて再取得・再描画する(デバウンス)。初回ロード時(scoreが最初に
  // 現れた時)も同じ経路で発火する。
  useEffect(() => {
    const timer = setTimeout(() => {
      const osmd = osmdRef.current;
      if (!osmd || score.parts.length === 0) return;
      getScorePreviewMusicXml(projectId)
        .then((xml) => osmd.load(xml))
        .then(() => {
          loadedRef.current = true;
          setError(null);
          osmd.render();
        })
        .catch((err: unknown) => {
          setError(err instanceof Error ? err.message : String(err));
        });
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [score, projectId]);

  const part = score.parts[0];
  const selectedArray = [...selectedNoteIds];
  let autoFromBar: number | null = null;
  let autoToBar: number | null = null;
  if (part && selectedArray.length === 1 && manualFromBar === "" && manualToBar === "") {
    const note = part.notes.find((n) => n.id === selectedArray[0]);
    if (note?.onset_tick != null) {
      const lastTick = Math.max(
        note.onset_tick + 1,
        ...part.notes.map((n) => (n.onset_tick ?? 0) + (n.duration_tick ?? 0)),
      );
      const boundaries = barBoundariesTicks(score.time_signatures, score.divisions, lastTick);
      const bar = barNumberForTick(boundaries, note.onset_tick);
      autoFromBar = Math.max(1, bar - AUTO_FOLLOW_WINDOW_BARS);
      autoToBar = bar + AUTO_FOLLOW_WINDOW_BARS;
    }
  }

  const effectiveFromBar = manualFromBar !== "" ? Number(manualFromBar) : autoFromBar;
  const effectiveToBar = manualToBar !== "" ? Number(manualToBar) : autoToBar;

  // 表示範囲が変わるたびにOSMDのオプションを更新して再描画する(再取得・
  // 再ロードは不要、既にロード済みの譜面に対する描画範囲の変更のみ)。
  useEffect(() => {
    const osmd = osmdRef.current;
    if (!osmd || !loadedRef.current) return;
    if (effectiveFromBar != null && effectiveToBar != null) {
      // 既知の制約(実機検証で確認): OSMDの`drawUpToMeasureNumber`は指定した
      // 小節番号ちょうどではなく、実際には1小節分多く表示することがある
      // (OSMD内部の小節番号/インデックス変換の挙動、ライブラリ側の仕様の
      // 詳細までは追いきれていない)。「表示範囲(小節)の指定」はおおよその
      // 絞り込みとして機能すれば十分と判断し、この誤差は許容する。
      osmd.setOptions({
        drawFromMeasureNumber: effectiveFromBar,
        drawUpToMeasureNumber: effectiveToBar,
      });
    } else {
      // OSMDの`setOptions`は`drawFromMeasureNumber`/`drawUpToMeasureNumber`が
      // `undefined`の場合、既存の制限を維持したまま何もしない(OSMD側の実装:
      // `value >= 0`の場合のみ内部の`MinMeasureToDrawIndex`/
      // `MaxMeasureToDrawIndex`を更新する)。「全体を表示」に戻すには
      // `EngravingRules`を直接既定値へ戻す必要がある。`Min/MaxMeasureToDrawIndex`
      // だけでなく`Min/MaxMeasureToDrawNumber`もリセットしないと、後続の
      // `render()`内部処理でNumber側の古い値がIndex側を上書きし直し、
      // 「全体を表示」に戻らない(実機検証で確認した挙動)。
      osmd.EngravingRules.MinMeasureToDrawIndex = 0;
      osmd.EngravingRules.MaxMeasureToDrawIndex = Number.MAX_VALUE;
      osmd.EngravingRules.MinMeasureToDrawNumber = 0;
      osmd.EngravingRules.MaxMeasureToDrawNumber = Number.MAX_VALUE;
    }
    osmd.render();
  }, [effectiveFromBar, effectiveToBar]);

  return (
    <section className="space-y-3 rounded-lg border border-gray-200 p-4">
      <h3 className="text-lg font-semibold text-gray-700">楽譜プレビュー</h3>
      <div className="flex flex-wrap items-end gap-4">
        <label className={INPUT_LABEL_CLASS}>
          表示小節(開始)
          <input
            type="number"
            min={1}
            value={manualFromBar}
            onChange={(e) => setManualFromBar(e.target.value)}
            placeholder="全体"
            className={INPUT_CLASS}
          />
        </label>
        <label className={INPUT_LABEL_CLASS}>
          表示小節(終了)
          <input
            type="number"
            min={1}
            value={manualToBar}
            onChange={(e) => setManualToBar(e.target.value)}
            placeholder="全体"
            className={INPUT_CLASS}
          />
        </label>
        <span className="text-xs text-gray-500">
          空欄なら全体を表示(1件選択中はその小節付近へ自動追従)
        </span>
      </div>
      {error && <p className="text-sm text-red-600">{error}</p>}
      <div ref={containerRef} className="w-full overflow-x-auto" />
    </section>
  );
}
