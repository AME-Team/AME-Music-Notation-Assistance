import { type BarWindow, midiBarRect, type PreviewNote, previewPitchRange } from "../lib/scoreView";

interface MidiBarProps {
  notes: PreviewNote[];
  window: BarWindow;
  height?: number;
}

/** SVGの内部座標系(横は`preserveAspectRatio="none"`で実幅へ伸ばす)。 */
const VIEW_WIDTH = 1000;

/** 小節番号を出す上限(それ以上は番号が重なるため線だけにする)。 */
const LABEL_MAX_BARS = 8;

/**
 * #172: 先頭N小節のMIDIバー(読み取り専用)。
 *
 * 作業ページでは補正の効果を**その場で**見たいので、編集可能な
 * `PianoRoll`(ドラッグ・選択・op送信を伴う)ではなく、描画だけの軽量なバーを
 * 常設する。位置合わせの主目的は「補正した拍・小節の頭に対して音符がどこに乗って
 * いるか」なので、小節線と音高の対応だけを描けば足りる。
 */
export function MidiBar({ notes, window: barWindow, height = 96 }: MidiBarProps) {
  const range = previewPitchRange(notes);
  const span = Math.max(barWindow.toTick - barWindow.fromTick, 1);
  const boundariesInView = barWindow.boundaries.filter(
    (tick) => tick >= barWindow.fromTick && tick <= barWindow.toTick,
  );
  // 小節番号は「実在する小節」の左端に置く。`boundaries`の末尾は次小節の開始線
  // (表示範囲の右端)なので、そのまま数えると存在しない小節番号が1つ増え、
  // 等幅カラムに並べると小節線の位置ともずれる(MIDDLEレビュー指摘)。
  const labelTicks = boundariesInView.slice(0, barWindow.toBar);
  const showLabels = barWindow.toBar <= LABEL_MAX_BARS;
  const ratioOf = (tick: number) => ((tick - barWindow.fromTick) / span) * 100;

  return (
    <div className="overflow-hidden rounded-md border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-900">
      <svg
        viewBox={`0 0 ${VIEW_WIDTH} ${height}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`先頭${barWindow.toBar}小節のMIDIバー`}
        data-testid="midi-bar"
        className="block w-full"
        style={{ height }}
      >
        {notes.map((note) => {
          if (!range) return null;
          const rect = midiBarRect(note, barWindow, range, { width: VIEW_WIDTH, height });
          return (
            <rect
              key={note.id}
              data-testid="midi-note"
              x={rect.x}
              y={rect.y}
              width={rect.width}
              height={rect.height}
              rx={1}
              className="fill-blue-500 dark:fill-blue-400"
            />
          );
        })}
        {boundariesInView.map((tick) => (
          <line
            key={`line-${tick}`}
            data-testid="midi-barline"
            x1={(ratioOf(tick) / 100) * VIEW_WIDTH}
            x2={(ratioOf(tick) / 100) * VIEW_WIDTH}
            y1={0}
            y2={height}
            className="stroke-gray-300 dark:stroke-gray-600"
            strokeWidth={1}
            strokeDasharray="2 2"
          />
        ))}
      </svg>
      {showLabels && (
        <div className="relative h-4" data-testid="midi-bar-labels">
          {labelTicks.map((tick, index) => (
            <span
              key={`label-${tick}`}
              data-testid="midi-bar-label"
              className="absolute top-0 text-[10px] text-gray-400 dark:text-gray-500"
              // 小節線と同じ比率で置く(等幅カラムでは線と一致しない)。
              style={{ left: `calc(${ratioOf(tick)}% + 2px)` }}
            >
              {index + 1}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
