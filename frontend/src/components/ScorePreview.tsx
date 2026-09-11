import { OpenSheetMusicDisplay } from "opensheetmusicdisplay";
import { useEffect, useRef, useState } from "react";
import type { ScoreIR } from "../api/client";
import { getScorePreviewMusicXml } from "../api/client";
import { barBoundariesTicks, barNumberForTick } from "../lib/pianoRoll";
import { usePlaybackStore } from "../stores/playbackStore";

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
 * #33/#34: OSMDによる楽譜プレビュー(FR-11/NFR-03)+再生カーソル連動(FR-12)。
 *
 * OSMDインスタンスはマウント時に1回だけ生成し(`osmdRef`)、`score`propの
 * 変更を500msデバウンスしてから`GET /score/preview.musicxml`を再取得→
 * `osmd.load()`→`render()`する(NFR-03: 「編集中は再描画せず操作停止500ms後に
 * まとめて更新する」、設計書§12.5)。
 *
 * 表示範囲(小節)は2通りの経路で決まる: (1) `fromBar`/`toBar`入力欄
 * (両方空欄なら手動指定なし)、(2) 手動指定が無い場合、ピアノロールで
 * ちょうど1件選択されているノートの小節を中心とした自動追従(#33設計:
 * 「ピアノロールとのカーソル連動」を選択ベースの簡易版として実装する)。
 *
 * 再生中のカーソル連動(#34)は上記の表示範囲とは独立に動く: `playbackStore`の
 * `currentBar`が変わるたびにOSMDの`Cursor`(`nextMeasure()`/`previousMeasure()`)
 * を小節単位で追従させる(音符単位の精密な追従はスコープ外)。手動で表示範囲を
 * 絞っている間に再生中の小節がその範囲外になった場合、カーソル自体は動くが
 * 画面上には見えない(既知の制約として許容する)。
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
  // #33-M3レビュー指摘: `loaded`はrefではなくstateにする。refだと`true`に
  // なっても後述の表示範囲effect(`[effectiveFromBar, effectiveToBar, loaded]`
  // 依存)が再評価されず、初回ロード完了時点で既に選択済みだった自動追従の
  // 範囲が反映されない(全体表示のままになる)。
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [manualFromBar, setManualFromBar] = useState("");
  const [manualToBar, setManualToBar] = useState("");
  // #33-M3レビュー指摘: デバウンスされた非同期ロードに順序保証・キャンセルが
  // 無いと、連続編集時に古いリクエストのレスポンスが後から解決して新しい
  // 描画を上書きしうる。世代カウンタで「自分が最新のリクエストか」を
  // `.then`/`.catch`内で確認し、古ければ結果を破棄する(`useScore.ts`の
  // `latestMutationIdRef`と同じ考え方)。アンマウント時にもインクリメントし、
  // 未マウント後の`setState`を防ぐ。
  const latestRequestIdRef = useRef(0);
  // #34: OSMDカーソルの現在位置(小節番号、1始まり)。`nextMeasure()`/
  // `previousMeasure()`は相対移動APIのため、直前の同期位置を覚えておき
  // 差分から呼び分ける必要がある。
  const lastSyncedBarRef = useRef(1);
  const currentBar = usePlaybackStore((s) => s.currentBar);
  const isPlaying = usePlaybackStore((s) => s.isPlaying);

  useEffect(() => {
    if (!containerRef.current) return;
    osmdRef.current = new OpenSheetMusicDisplay(containerRef.current, {
      autoResize: true,
      drawTitle: false,
    });
    return () => {
      latestRequestIdRef.current += 1;
    };
  }, []);

  // NFR-03: scoreが変わるたびにタイマーをリセットし、500ms操作が無ければ
  // まとめて再取得・再描画する(デバウンス)。初回ロード時(scoreが最初に
  // 現れた時)も同じ経路で発火する。
  useEffect(() => {
    const timer = setTimeout(() => {
      const osmd = osmdRef.current;
      if (!osmd || score.parts.length === 0) return;
      const requestId = ++latestRequestIdRef.current;
      getScorePreviewMusicXml(projectId)
        .then((xml) => osmd.load(xml))
        .then(() => {
          if (latestRequestIdRef.current !== requestId) return; // 古いリクエストの結果は破棄
          setError(null);
          setLoaded(true);
          osmd.render();
        })
        .catch((err: unknown) => {
          if (latestRequestIdRef.current !== requestId) return;
          setError(err instanceof Error ? err.message : String(err));
        });
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [score, projectId]);

  const part = score.parts[0];
  const selectedArray = [...selectedNoteIds];
  const lastBar = part
    ? (() => {
        const lastTick = part.notes.reduce(
          (max, n) => Math.max(max, (n.onset_tick ?? 0) + (n.duration_tick ?? 0)),
          1,
        );
        const boundaries = barBoundariesTicks(score.time_signatures, score.divisions, lastTick);
        return boundaries.length;
      })()
    : null;

  let autoFromBar: number | null = null;
  let autoToBar: number | null = null;
  if (part && selectedArray.length === 1 && manualFromBar === "" && manualToBar === "") {
    const note = part.notes.find((n) => n.id === selectedArray[0]);
    if (note?.onset_tick != null) {
      const lastTick = part.notes.reduce(
        (max, n) => Math.max(max, (n.onset_tick ?? 0) + (n.duration_tick ?? 0)),
        note.onset_tick + 1,
      );
      const boundaries = barBoundariesTicks(score.time_signatures, score.divisions, lastTick);
      const bar = barNumberForTick(boundaries, note.onset_tick);
      autoFromBar = Math.max(1, bar - AUTO_FOLLOW_WINDOW_BARS);
      autoToBar = bar + AUTO_FOLLOW_WINDOW_BARS;
    }
  }

  // #33-M3レビュー指摘: 開始/終了の片方だけ手動指定した場合、もう片方が
  // auto値(未計算ならnull)のままだと範囲指定全体が無視されてしまうため、
  // 空欄側は既定値(開始側は1小節目、終了側は最終小節)で補う。
  const effectiveFromBar =
    manualFromBar !== "" ? Number(manualFromBar) : (autoFromBar ?? (manualToBar !== "" ? 1 : null));
  const effectiveToBar =
    manualToBar !== ""
      ? Number(manualToBar)
      : (autoToBar ?? (manualFromBar !== "" ? lastBar : null));

  // 表示範囲が変わるたびにOSMDのオプションを更新して再描画する(再取得・
  // 再ロードは不要、既にロード済みの譜面に対する描画範囲の変更のみ)。
  useEffect(() => {
    const osmd = osmdRef.current;
    if (!osmd || !loaded) return;
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
  }, [effectiveFromBar, effectiveToBar, loaded]);

  // #34: 譜面が(再)ロードされた直後は小節1から同期を始める(直前のプロジェクト
  // /直前ロードの小節位置を引き継がない)。
  useEffect(() => {
    if (loaded) lastSyncedBarRef.current = 1;
  }, [loaded]);

  // #34: TransportBar(playbackStore)の再生状態に合わせてOSMDカーソルの表示を
  // 切り替える。ピアノロールとOSMDのカーソル連動(#34の作業項目)を、
  // 小節(bar)単位の粒度で実装する(#34設計判断: OSMDの`Cursor`が
  // `nextMeasure()`/`previousMeasure()`という小節単位のAPIしか持たないため、
  // 音符単位の精密な追従はスコープ外とする)。
  useEffect(() => {
    const osmd = osmdRef.current;
    if (!osmd || !loaded) return;
    if (isPlaying) osmd.cursor.show();
    else osmd.cursor.hide();
  }, [isPlaying, loaded]);

  useEffect(() => {
    const osmd = osmdRef.current;
    if (!osmd || !loaded) return;
    const delta = currentBar - lastSyncedBarRef.current;
    if (delta === 1) {
      osmd.cursor.nextMeasure();
    } else if (delta === -1) {
      osmd.cursor.previousMeasure();
    } else if (delta !== 0) {
      // 連続していない移動(シーク・巻き戻し・初回同期)は一旦先頭へ戻してから
      // 目的の小節まで進める。
      osmd.cursor.reset();
      for (let i = 1; i < currentBar; i += 1) osmd.cursor.nextMeasure();
    }
    lastSyncedBarRef.current = currentBar;
  }, [currentBar, loaded]);

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
