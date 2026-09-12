import { useCallback, useEffect, useLayoutEffect, useMemo, useRef } from "react";
import type { NoteOp } from "../api/client";
import {
  barBoundariesTicks,
  hitTestNote,
  midiToY,
  type PianoRollNote,
  provenanceStyle,
  type TimeSignatureEntry,
  tickToX,
  visibleNoteRange,
  xToTick,
  yToMidi,
} from "../lib/pianoRoll";
import { usePlaybackStore } from "../stores/playbackStore";

const ROW_HEIGHT_PX = 14;
const DEFAULT_PX_PER_TICK = 0.15;
const MIN_PX_PER_TICK = 0.01;
const MAX_PX_PER_TICK = 2;
const DEFAULT_TOP_MIDI = 84;
const INITIAL_SCROLL_MARGIN_TICK = 480;
const MIN_DRAG_DISTANCE_PX = 3; // これ未満の移動はクリック(選択)として扱う
const HATCH_SPACING_PX = 4;

/** #35: `flags: ghost_candidate`ノートの斜線ハッチ(設計書§12.4)。
 *
 * `ctx.clip()`で矩形にクリップしてから等間隔の斜線を引く、標準的な
 * Canvas斜線ハッチの実装。呼び出し元が既に`ctx.save()`/`ctx.restore()`で
 * 囲んでいる前提(このスコープ内の`clip()`が呼び出し元へ漏れないように)。
 */
function drawDiagonalHatch(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
): void {
  ctx.save();
  ctx.beginPath();
  ctx.rect(x, y, w, h);
  ctx.clip();
  ctx.strokeStyle = "rgba(0, 0, 0, 0.35)";
  ctx.lineWidth = 1;
  for (let offset = -h; offset < w; offset += HATCH_SPACING_PX) {
    ctx.beginPath();
    ctx.moveTo(x + offset, y + h);
    ctx.lineTo(x + offset + h, y);
    ctx.stroke();
  }
  ctx.restore();
}

interface PianoRollProps {
  /** #30/#31: 量子化済み(`onset_tick`/`duration_tick`が設定済み)のノートのみ。
   *
   * 呼び出し元(`PianoRollEditor`)が量子化前のノートを除外してから渡すこと
   * (tick空間で描画/編集するため)。
   */
  notes: PianoRollNote[];
  partId: string;
  timeSignatures: TimeSignatureEntry[];
  divisions: number;
  selectedNoteIds: ReadonlySet<number>;
  onSelectionChange: (ids: Set<number>) => void;
  onApplyOps: (ops: NoteOp[]) => void;
  height?: number;
}

type DragState =
  | {
      mode: "move";
      startPointerTick: number;
      noteStarts: Map<number, number>; // noteId -> ドラッグ開始時のonset_tick
      previewDeltaTick: number;
    }
  | {
      mode: "resize";
      noteId: number;
      startDurationTick: number;
      startPointerTick: number;
      previewDurationTick: number;
    }
  | {
      mode: "select";
      startTick: number;
      startMidi: number;
      currentTick: number;
      currentMidi: number;
    }
  | null;

/**
 * PianoRoll(#30): 単一Canvasへの直接描画によるノート表示・ヒットテスト・
 * ドラッグ操作。ノート1つ=DOM要素1つにしない(NFR-02、3000ノート規模)。
 *
 * 描画データ(スクロール/ズーム/ドラッグ中のプレビュー)はrefに保持し、単一の
 * 永続`requestAnimationFrame`ループが「dirtyフラグ」を見て必要な時だけ
 * `draw()`する(状態更新のたびに再描画しない)。座標系はX=tick(時間)、
 * Y=MIDI(上ほど高音)。
 *
 * 選択(`onSelectionChange`)とop確定(`onApplyOps`)のみReact側へ通知する
 * (ドラッグ中は一切React状態を更新しない)。
 */
export function PianoRoll({
  notes,
  partId,
  timeSignatures,
  divisions,
  selectedNoteIds,
  onSelectionChange,
  onApplyOps,
  height = 420,
}: PianoRollProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef({
    scrollTick: -INITIAL_SCROLL_MARGIN_TICK,
    topMidi: DEFAULT_TOP_MIDI,
    pxPerTick: DEFAULT_PX_PER_TICK,
  });
  const dragRef = useRef<DragState>(null);
  const dirtyRef = useRef(true);
  const pointerDownRef = useRef<{ x: number; y: number } | null>(null);

  const sortedNotes = useMemo(
    () => [...notes].sort((a, b) => a.onset_tick - b.onset_tick),
    [notes],
  );
  const selectedRef = useRef(selectedNoteIds);
  const sortedNotesRef = useRef(sortedNotes);
  const timeSignaturesRef = useRef(timeSignatures);
  const divisionsRef = useRef(divisions);

  const markDirty = useCallback(() => {
    dirtyRef.current = true;
  }, []);

  // #30-M3レビュー指摘: render中のref書き込みはReactが認めておらず、
  // concurrent renderingでrenderが中断・破棄されると古い値が残ったままに
  // なりうる。useLayoutEffect(コミット後・ペイント前に同期実行)で同期する
  // ことで、実際にコミットされたrenderの値とrefの整合を保証する。
  useLayoutEffect(() => {
    selectedRef.current = selectedNoteIds;
    sortedNotesRef.current = sortedNotes;
    timeSignaturesRef.current = timeSignatures;
    divisionsRef.current = divisions;
    markDirty();
  }, [selectedNoteIds, sortedNotes, timeSignatures, divisions, markDirty]);

  // 描画本体はrefのみを参照するため、useCallbackで参照を安定させる
  // (依存配列を空にできる=rAFループ用useEffectを1回だけ登録できる)。
  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const widthCss = canvas.width / dpr;
    const heightCss = canvas.height / dpr;
    ctx.save();
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, widthCss, heightCss);

    const { scrollTick, topMidi, pxPerTick } = viewRef.current;
    ctx.fillStyle = "#f9fafb";
    ctx.fillRect(0, 0, widthCss, heightCss);

    // 小節線。
    const viewEndTick = scrollTick + widthCss / pxPerTick;
    const boundaries = barBoundariesTicks(
      timeSignaturesRef.current,
      divisionsRef.current,
      Math.max(viewEndTick, 0),
    );
    ctx.strokeStyle = "#9ca3af";
    ctx.lineWidth = 1;
    for (const tick of boundaries) {
      const x = tickToX(tick, scrollTick, pxPerTick);
      if (x < -1 || x > widthCss + 1) continue;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, 0);
      ctx.lineTo(Math.round(x) + 0.5, heightCss);
      ctx.stroke();
    }

    // ノート。#35: 出自(provenance)ごとに塗り/枠線パターンを変える
    // (設計書§12.4、色だけでなくパターンでも区別できるようにする)。
    // 削除済み(status==="deleted")も#35からは描画対象にする(以前は完全に
    // スキップしていた)。"muted"(現状どのパイプラインも設定しない)のみ
    // 引き続きスキップする。
    const notesNow = sortedNotesRef.current;
    const [startIdx, endIdx] = visibleNoteRange(notesNow, scrollTick, viewEndTick);
    const drag = dragRef.current;
    for (let i = startIdx; i < endIdx; i += 1) {
      const note = notesNow[i];
      if (note.status === "muted") continue;
      let onsetTick = note.onset_tick;
      let durationTick = note.duration_tick;
      if (drag?.mode === "move" && drag.noteStarts.has(note.id)) {
        onsetTick = (drag.noteStarts.get(note.id) ?? onsetTick) + drag.previewDeltaTick;
      } else if (drag?.mode === "resize" && drag.noteId === note.id) {
        durationTick = drag.previewDurationTick;
      }
      const x = tickToX(onsetTick, scrollTick, pxPerTick);
      const w = Math.max(durationTick * pxPerTick, 2);
      const y = midiToY(note.midi, topMidi, ROW_HEIGHT_PX);
      if (x + w < 0 || x > widthCss || y + ROW_HEIGHT_PX < 0 || y > heightCss) continue;

      const isSelected = selectedRef.current.has(note.id);
      const isDeleted = note.status === "deleted";
      const style = provenanceStyle(note.provenance);
      const rectX = x;
      const rectY = y + 1;
      const rectW = w;
      const rectH = ROW_HEIGHT_PX - 2;

      ctx.save();
      // #35: 削除済みは「破線アウトライン(半透明)」(設計書§12.4)。塗り自体は
      // 出自の色のまま透明度だけ下げる(誰が触った上で削除されたかは残す)。
      if (isDeleted) ctx.globalAlpha = 0.4;
      ctx.fillStyle = style.fill;
      ctx.fillRect(rectX, rectY, rectW, rectH);

      // #35: `flags: ghost_candidate`は斜線ハッチを塗りの上に重ねる
      // (現状どのパイプラインも設定しないため実際には描画されないが、
      // 汎用的なロジックとして実装しておく)。
      if (note.flags.includes("ghost_candidate")) {
        drawDiagonalHatch(ctx, rectX, rectY, rectW, rectH);
      }

      // 枠線: 削除済みは出自のパターンによらず常に破線に上書きする
      // (設計書§12.4「破線アウトライン」)。選択中は出自非依存の高コントラスト色
      // (黒系)の太線に上書きし、色覚特性に関わらず輝度差で選択状態を判別
      // できるようにする(`user`出自のオレンジ太線と紛らわしくならないよう、
      // 以前の琥珀色ではなくこちらを使う)。
      ctx.setLineDash(isDeleted ? [6, 3] : style.dash);
      ctx.lineWidth = isSelected ? 2 : style.lineWidth;
      ctx.strokeStyle = isSelected ? "#111827" : style.stroke;
      ctx.strokeRect(rectX + 0.5, rectY + 0.5, rectW - 1, rectH - 1);
      ctx.restore();
    }

    // 選択矩形。専用のDOMオーバーレイにせず同じCanvas上に直接描く
    // (ドラッグ中はReact側の再レンダーを一切発生させない方針、NFR-02を
    // 優先した設計判断)。
    if (drag?.mode === "select") {
      const x1 = tickToX(drag.startTick, scrollTick, pxPerTick);
      const x2 = tickToX(drag.currentTick, scrollTick, pxPerTick);
      const y1 = midiToY(drag.startMidi, topMidi, ROW_HEIGHT_PX);
      const y2 = midiToY(drag.currentMidi, topMidi, ROW_HEIGHT_PX);
      const rectX = Math.min(x1, x2);
      const rectY = Math.min(y1, y2);
      ctx.fillStyle = "rgba(59, 130, 246, 0.15)";
      ctx.fillRect(rectX, rectY, Math.abs(x2 - x1), Math.abs(y2 - y1));
      ctx.strokeStyle = "#2563eb";
      ctx.lineWidth = 1;
      ctx.strokeRect(rectX + 0.5, rectY + 0.5, Math.abs(x2 - x1), Math.abs(y2 - y1));
    }

    // #34: 再生プレイヘッド。propsを介さず`usePlaybackStore`を直接読む
    // (`useMixStore`をTrackListの子が直接importするのと同じ既存パターン)。
    // `positionTick === 0`(未再生/停止直後の初期値)は非表示にする。
    const playbackTick = usePlaybackStore.getState().positionTick;
    if (playbackTick > 0) {
      const x = tickToX(playbackTick, scrollTick, pxPerTick);
      if (x >= -1 && x <= widthCss + 1) {
        ctx.strokeStyle = "#dc2626";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(Math.round(x) + 0.5, 0);
        ctx.lineTo(Math.round(x) + 0.5, heightCss);
        ctx.stroke();
      }
    }

    ctx.restore();
  }, []);

  // 描画ループ(#30: rAFで1フレーム1回に集約)。drawの参照が安定しているため、
  // このエフェクトはマウント時に1度だけ登録される。
  useEffect(() => {
    let raf = 0;
    let lastDrawnPlaybackTick = -1;
    const loop = () => {
      // #34: 再生中はプレイヘッドが毎フレーム動くため、他に変更が無くても
      // 強制的にdirtyにする(通常は選択/ドラッグ/データ変更時のみdirtyになる)。
      // #34-M3レビュー指摘: `isPlaying`のみを見ると、停止/先頭戻しで
      // `positionTick`が0に戻った直後(この時点で既に`isPlaying`はfalse)は
      // dirtyが立たず、最後に描いたプレイヘッド線が残り続ける。`isPlaying`に
      // 関わらず前回描画時からtickが変化していれば強制的にdirtyにする。
      const playbackTick = usePlaybackStore.getState().positionTick;
      if (usePlaybackStore.getState().isPlaying || playbackTick !== lastDrawnPlaybackTick) {
        dirtyRef.current = true;
        lastDrawnPlaybackTick = playbackTick;
      }
      if (dirtyRef.current) {
        dirtyRef.current = false;
        draw();
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [draw]);

  // Canvasのサイズ(DPR対応)をコンテナに追従させる。
  useEffect(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;
    const observer = new ResizeObserver(() => {
      const dpr = window.devicePixelRatio || 1;
      const width = container.clientWidth;
      canvas.width = Math.max(1, Math.floor(width * dpr));
      canvas.height = Math.max(1, Math.floor(height * dpr));
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      markDirty();
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, [height, markDirty]);

  function eventToTickMidi(e: React.PointerEvent<HTMLCanvasElement>): {
    tick: number;
    midi: number;
  } {
    const canvas = canvasRef.current;
    if (!canvas) return { tick: 0, midi: 0 };
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const { scrollTick, topMidi, pxPerTick } = viewRef.current;
    return {
      tick: xToTick(x, scrollTick, pxPerTick),
      midi: yToMidi(y, topMidi, ROW_HEIGHT_PX),
    };
  }

  // #30-M3レビュー指摘: ReactのonWheelはpassiveリスナーとしてルートに
  // 登録されるため(React 17以降)、合成イベント内のe.preventDefault()は
  // 効かず、キャンバスのパン/ズームとページ側のスクロールが同時に発生
  // してしまう。ネイティブのaddEventListenerに{passive:false}を明示して
  // 登録することで初めてpreventDefault()が機能する。
  // #30-M3レビュー指摘: この関数はref(viewRef)と安定した`markDirty`しか
  // 参照しないため、render毎にref代入し直す必要はない。useCallback(空配列)
  // で一度だけ生成し、下のeffectで一度だけ登録する(他のref同期と異なり
  // render中の代入自体が不要になる)。
  const handleWheel = useCallback(
    (e: WheelEvent) => {
      e.preventDefault();
      const view = viewRef.current;
      if (e.ctrlKey || e.metaKey) {
        const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
        view.pxPerTick = Math.min(
          MAX_PX_PER_TICK,
          Math.max(MIN_PX_PER_TICK, view.pxPerTick * factor),
        );
      } else if (e.shiftKey) {
        view.scrollTick = Math.max(0, view.scrollTick + e.deltaY / view.pxPerTick);
      } else {
        view.topMidi += e.deltaY > 0 ? -1 : 1;
        view.scrollTick = Math.max(0, view.scrollTick + e.deltaX / view.pxPerTick);
      }
      markDirty();
    },
    [markDirty],
  );

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    canvas.addEventListener("wheel", handleWheel, { passive: false });
    return () => canvas.removeEventListener("wheel", handleWheel);
  }, [handleWheel]);

  function handlePointerDown(e: React.PointerEvent<HTMLCanvasElement>) {
    (e.target as Element).setPointerCapture(e.pointerId);
    pointerDownRef.current = { x: e.clientX, y: e.clientY };
    const { tick, midi } = eventToTickMidi(e);
    const hit = hitTestNote(sortedNotesRef.current, tick, midi, viewRef.current.pxPerTick);

    // #35: 削除済みノートのクリックは即座に復活させる(設計書§12.4「クリックで
    // 復活」)。選択状態は変えず、ドラッグ(移動/リサイズ)にも入らない。
    if (hit?.region === "restore") {
      onApplyOps([{ type: "note.restore", note_ids: [hit.note.id] }]);
      return;
    }

    if (hit) {
      const alreadySelected = selectedRef.current.has(hit.note.id);
      let nextSelection: Set<number>;
      let selectionChanged = true;
      if (e.shiftKey) {
        nextSelection = toggledSelection(selectedRef.current, hit.note.id);
      } else if (alreadySelected) {
        nextSelection = new Set(selectedRef.current);
        selectionChanged = false;
      } else {
        nextSelection = new Set([hit.note.id]);
      }
      if (selectionChanged) onSelectionChange(nextSelection);

      if (hit.region === "resize-right") {
        dragRef.current = {
          mode: "resize",
          noteId: hit.note.id,
          startDurationTick: hit.note.duration_tick,
          startPointerTick: tick,
          previewDurationTick: hit.note.duration_tick,
        };
      } else {
        const targetIds = nextSelection.has(hit.note.id) ? nextSelection : new Set([hit.note.id]);
        const noteStarts = new Map<number, number>();
        for (const note of sortedNotesRef.current) {
          if (targetIds.has(note.id)) noteStarts.set(note.id, note.onset_tick);
        }
        dragRef.current = { mode: "move", startPointerTick: tick, noteStarts, previewDeltaTick: 0 };
      }
    } else {
      dragRef.current = {
        mode: "select",
        startTick: tick,
        startMidi: midi,
        currentTick: tick,
        currentMidi: midi,
      };
    }
    markDirty();
  }

  function handlePointerMove(e: React.PointerEvent<HTMLCanvasElement>) {
    const drag = dragRef.current;
    if (!drag) return;
    const { tick, midi } = eventToTickMidi(e);
    if (drag.mode === "move") {
      drag.previewDeltaTick = snapToGrid(tick - drag.startPointerTick, divisionsRef.current);
    } else if (drag.mode === "resize") {
      const delta = snapToGrid(tick - drag.startPointerTick, divisionsRef.current);
      drag.previewDurationTick = Math.max(1, drag.startDurationTick + delta);
    } else if (drag.mode === "select") {
      drag.currentTick = tick;
      drag.currentMidi = midi;
    }
    markDirty();
  }

  function handlePointerUp(e: React.PointerEvent<HTMLCanvasElement>) {
    const drag = dragRef.current;
    dragRef.current = null;
    if (!drag) return;
    const pointerStart = pointerDownRef.current;
    pointerDownRef.current = null;
    const movedEnough =
      !pointerStart ||
      Math.hypot(e.clientX - pointerStart.x, e.clientY - pointerStart.y) >= MIN_DRAG_DISTANCE_PX;

    if (drag.mode === "move" && movedEnough && drag.previewDeltaTick !== 0) {
      const noteIds = [...drag.noteStarts.keys()];
      // #30-M3レビュー指摘: 各ノートで個別にMath.max(0, ...)すると、選択中の
      // 一部だけが0にクランプされノート間の相対間隔が崩れる。グループ全体で
      // 最も左にあるノートがtick 0を下回らないようdelta自体を補正してから
      // 全ノートへ同じdeltaを適用する。
      const starts = [...drag.noteStarts.values()];
      const minStart = Math.min(...starts);
      const clampedDelta = Math.max(drag.previewDeltaTick, -minStart);
      const ops: NoteOp[] = noteIds.map((noteId) => ({
        type: "note.update",
        note_ids: [noteId],
        onset_tick: (drag.noteStarts.get(noteId) ?? 0) + clampedDelta,
      }));
      onApplyOps(ops);
    } else if (drag.mode === "resize" && drag.previewDurationTick !== drag.startDurationTick) {
      onApplyOps([
        { type: "note.update", note_ids: [drag.noteId], duration_tick: drag.previewDurationTick },
      ]);
    } else if (drag.mode === "select") {
      const lo = Math.min(drag.startTick, drag.currentTick);
      const hi = Math.max(drag.startTick, drag.currentTick);
      const midiLo = Math.min(drag.startMidi, drag.currentMidi);
      const midiHi = Math.max(drag.startMidi, drag.currentMidi);
      if (movedEnough) {
        const ids = new Set<number>();
        for (const note of sortedNotesRef.current) {
          if (note.status !== "active") continue;
          const end = note.onset_tick + note.duration_tick;
          if (end >= lo && note.onset_tick <= hi && note.midi >= midiLo && note.midi <= midiHi) {
            ids.add(note.id);
          }
        }
        onSelectionChange(ids);
      } else {
        onSelectionChange(new Set());
      }
    }
    markDirty();
  }

  // #30-M3レビュー指摘: pointercancel(他アプリへのフォーカス移動、
  // タッチのキャンセル等)やlostpointercapture発生時にdragRef/
  // pointerDownRefがクリアされないと、以降のpointermoveがドラッグ継続として
  // 扱われ、コミットされないプレビューがゴーストとして残り続ける。
  function handlePointerCancel() {
    dragRef.current = null;
    pointerDownRef.current = null;
    markDirty();
  }

  function handleDoubleClick(e: React.MouseEvent<HTMLCanvasElement>) {
    const { tick, midi } = eventToTickMidi(e as unknown as React.PointerEvent<HTMLCanvasElement>);
    const hit = hitTestNote(sortedNotesRef.current, tick, midi, viewRef.current.pxPerTick);
    if (hit) return; // 既存ノート上のダブルクリックは何もしない
    // #30-M3レビュー指摘: ドラッグ移動/リサイズはグリッドへスナップするのに
    // 追加だけMath.roundによる自由位置だと矛盾する。同じsnapToGridで
    // グリッド(divisions/4)へ整列させる。
    onApplyOps([
      {
        type: "note.add",
        part_id: partId,
        onset_tick: Math.max(0, snapToGrid(tick, divisionsRef.current)),
        duration_tick: divisionsRef.current,
        midi: Math.max(0, Math.min(127, Math.round(midi))),
      },
    ]);
  }

  return (
    <div ref={containerRef} className="w-full">
      <canvas
        ref={canvasRef}
        style={{ height }}
        className="block w-full cursor-crosshair rounded-md border border-gray-300 touch-none"
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerCancel}
        onLostPointerCapture={handlePointerCancel}
        onDoubleClick={handleDoubleClick}
      />
    </div>
  );
}

function toggledSelection(current: ReadonlySet<number>, id: number): Set<number> {
  const next = new Set(current);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

/**
 * ドラッグ量を16分音符グリッドへスナップする(#30-M3レビュー指摘: 移動/
 * リサイズがMath.roundによる自由移動のみだと、オフグリッドの
 * onset_tick/duration_tickが生成されうる。厳密にはbeatmapのスナップ候補
 * (`snap_candidates`、pipeline/quantize.py)まで合わせ込むべきだが、これは
 * ピアノロール側の"ドラッグ量"自体を素直な音楽的単位に丸めるだけの簡易実装
 * とし、正確なスナップ候補との整合は将来PRの課題とする)。`divisions`は
 * 四分音符あたりのtick数のため、16分音符は`divisions/4`。
 */
function snapToGrid(deltaTick: number, divisions: number): number {
  const gridTicks = Math.max(1, Math.round(divisions / 4));
  return Math.round(deltaTick / gridTicks) * gridTicks;
}
