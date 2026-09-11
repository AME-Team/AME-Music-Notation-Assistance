import { useCallback, useEffect, useRef, useState } from "react";
import * as Tone from "tone";
import { getStemAudioUrl } from "../api/client";
import { useScore } from "../hooks/useScore";
import { useStems } from "../hooks/useStems";
import { barBoundariesTicks, barNumberForTick } from "../lib/pianoRoll";
import { bpmAtBar, buildTickMappingPoints, formatTime, secondsToTick } from "../lib/transport";
import { useMixStore } from "../stores/mixStore";
import { type PlaybackMode, usePlaybackStore } from "../stores/playbackStore";

interface TransportBarProps {
  projectId: string;
}

const BUTTON_CLASS =
  "rounded-md px-3 py-1.5 text-sm font-medium text-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50";

const MODE_BUTTON_CLASS = (active: boolean) =>
  `rounded-md px-3 py-1 text-xs font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 ${
    active ? "bg-blue-600 text-white" : "bg-gray-100 text-gray-700 hover:bg-gray-200"
  }`;

/**
 * #34: Tone.js Transportによる、Score IR(MIDI相当)とステム音声の同期A/B比較(FR-12)。
 *
 * MIDIシンセとステムの全`Tone.Player`を常に同じTransport上で並行再生し、
 * A/B切替は片方の`Tone.Volume.mute`を反転するだけにする(#34設計判断:
 * どちらか一方だけをロード/破棄して切り替える方式だと、切替のたびに再生位置が
 * ズレたり再ロード待ちが発生し、「切り替えながら聴き比べる」というFR-12の
 * 即応性の意図を損なう)。
 *
 * 再生対象は`score.parts[0]`のみ(#30以来のこのプロジェクトの既存スコープ
 * 限定「先頭パートのみ編集/表示対象」を踏襲)。ステムのソロ/ミュートは
 * `useMixStore`(`TrackList`と共有)をライブ購読して反映する(`TrackList`側で
 * 操作した結果が再生中でも即座に効く)。
 *
 * 親`ProjectWorkspace`は他のプロジェクト依存コンポーネント
 * (`PianoRollEditor`/`TrackList`と同様)`key={projectId}`でこのコンポーネント
 * ごと再マウントする前提のため、`projectId`が変わった場合の後始末は考慮しない
 * (フルアンマウント→フル再マウントで自然に賄われる)。
 */
export function TransportBar({ projectId }: TransportBarProps) {
  const { data: score } = useScore(projectId);
  const { data: stems } = useStems(projectId);
  const [error, setError] = useState<string | null>(null);
  const mode = usePlaybackStore((s) => s.mode);
  const isPlaying = usePlaybackStore((s) => s.isPlaying);
  const currentBar = usePlaybackStore((s) => s.currentBar);
  const bpm = usePlaybackStore((s) => s.bpm);
  // #34-M3レビュー指摘: `positionSec`は再生中rAFごと(約60fps)に変わるため、
  // これをセレクタで購読すると`TransportBar`全体が毎フレーム再レンダーされ、
  // playbackStoreの設計意図(高頻度更新はgetState()直読み、`PianoRoll`と同じ
  // 方針)に反する。時間表示だけはDOMを`ref`経由で直接書き換え、Reactの
  // 再レンダーを経由しない(`PianoRoll`のCanvas直接描画と同じ考え方)。
  const timeTextRef = useRef<HTMLSpanElement>(null);

  const synthRef = useRef<Tone.PolySynth | null>(null);
  const partRef = useRef<Tone.Part | null>(null);
  const midiVolumeRef = useRef<Tone.Volume | null>(null);
  const audioVolumeRef = useRef<Tone.Volume | null>(null);
  const playersRef = useRef<{ name: string; player: Tone.Player }[]>([]);
  const rafRef = useRef<number | null>(null);
  // #34-M3レビュー指摘: 曲終端の自動停止用`scheduleOnce`のイベントIDを保持する。
  // 再生開始のたびに`transport.cancel(0)`(Transport上のスケジュールを全消去)
  // していたが、これは`Tone.Part`が`.start(0)`で内部的にスケジュールした
  // ノートイベントも巻き添えで消してしまい、MIDIモードが無音になる原因だった
  // (Part自体は`notes`が変わらない限り作り直されないため)。前回のIDだけを
  // `transport.clear()`で個別に解除する。
  const stopEventIdRef = useRef<number | null>(null);
  const boundariesRef = useRef<number[]>([0]);
  const tickMappingPointsRef = useRef<{ sec: number; tick: number }[]>([]);

  const notes = score?.parts[0]?.notes ?? [];
  const tempoMap = score?.tempo_map ?? [];
  const timeSignatures = score?.time_signatures ?? [];
  const divisions = score?.divisions ?? 480;
  const durationSec = score?.source.duration_sec ?? 0;

  // マウント時に一度だけ: MIDI/Audioの2バス(Tone.Volume)をDestinationへ接続する。
  useEffect(() => {
    const midiVolume = new Tone.Volume(0).toDestination();
    const audioVolume = new Tone.Volume(0).toDestination();
    midiVolume.mute = usePlaybackStore.getState().mode !== "midi";
    audioVolume.mute = usePlaybackStore.getState().mode !== "audio";
    midiVolumeRef.current = midiVolume;
    audioVolumeRef.current = audioVolume;
    usePlaybackStore.getState().reset();

    return () => {
      const transport = Tone.getTransport();
      transport.stop();
      transport.cancel(0);
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      midiVolume.dispose();
      audioVolume.dispose();
    };
  }, []);

  // rAFループ(高頻度)や各種ハンドラから呼ぶ、Tone.Transportの現在位置を
  // playbackStoreへ反映する処理。`useCallback`にして、実際に依存する`tempoMap`
  // (score由来、scoreが変わらない限り参照は安定)が変わらない限り毎レンダー
  // 再生成されないようにする(下の境界計算effectの依存配列に安全に含めるため)。
  const syncPosition = useCallback(() => {
    const positionSec = Tone.getTransport().seconds;
    const positionTick = secondsToTick(tickMappingPointsRef.current, positionSec);
    const currentBar = barNumberForTick(boundariesRef.current, positionTick);
    const bpm = bpmAtBar(tempoMap, currentBar);
    usePlaybackStore.setState({ positionSec, positionTick, currentBar, bpm });
  }, [tempoMap]);

  // `syncPosition`と同じ理由でuseCallback化する(下の境界計算effectの依存配列に
  // 安全に含めるため)。`positionSec`自体は`usePlaybackStore.getState()`で直読み
  // し、Reactのセレクタ購読は使わない。
  const updateTimeText = useCallback(() => {
    if (!timeTextRef.current) return;
    const sec = usePlaybackStore.getState().positionSec;
    timeTextRef.current.textContent = `${formatTime(sec)} / ${formatTime(durationSec)}`;
  }, [durationSec]);

  // 小節境界(tick<->秒変換用の点列/BPM表示用のtempo_mapはstateではなくrefに
  // 保持し、rAFループ(高頻度)から毎回`score`をクロージャ経由で読まなくても
  // 済むようにする(#30のドラッグプレビューと同じ「高頻度アクセスはrefで」方針)。
  // 更新後に`syncPosition()`を呼ぶことで、再生開始前(positionSec=0)でも
  // score読み込み直後の時点でBar 1のBPMが表示されるようにする(呼ばないと
  // playbackStoreの初期値である既定120のままになってしまう)。
  useEffect(() => {
    tickMappingPointsRef.current = buildTickMappingPoints(notes);
    const lastTick = notes.reduce(
      (max, n) => Math.max(max, (n.onset_tick ?? 0) + (n.duration_tick ?? 0)),
      1,
    );
    boundariesRef.current = barBoundariesTicks(timeSignatures, divisions, lastTick);
    syncPosition();
    updateTimeText();
  }, [notes, timeSignatures, divisions, syncPosition, updateTimeText]);

  // MIDI再生: scoreのノートが変わるたびにTone.Partを作り直す。編集操作の
  // たびに(再生中でも)作り直すため、再生中に編集すると一瞬途切れる/巻き戻る
  // ことがあるが、#34設計判断としてこの制約を許容する(頻繁な編集操作中は
  // 通常再生を止めているはずで、実害は小さいと判断)。
  useEffect(() => {
    if (!midiVolumeRef.current) return;
    const synth = new Tone.PolySynth().connect(midiVolumeRef.current);
    const events = notes
      .filter((n) => n.status === "active" && n.onset_tick != null)
      .map((n) => ({
        time: n.onset_sec,
        midi: n.midi,
        duration: Math.max(n.duration_sec, 0.05),
        velocity: n.velocity / 127,
      }));
    const part = new Tone.Part((time, value) => {
      synth.triggerAttackRelease(
        Tone.Frequency(value.midi, "midi").toFrequency(),
        value.duration,
        time,
        value.velocity,
      );
    }, events).start(0);

    synthRef.current = synth;
    partRef.current = part;

    return () => {
      part.dispose();
      synth.dispose();
    };
  }, [notes]);

  // ステム再生: ステム一覧(分離の再実行時のみ変わる)が変わるたびに全プレイヤーを
  // 作り直す。`fetchAudioObjectUrl`ベースの`getStemAudioUrl`(既存、TrackList/
  // AudioPlayerと同じ認証済みfetch→Blob→objectURL、`file://`を使わない、
  // NFR-17)をそのまま再利用する。呼び出しのたびに新規fetch+新規objectURLが
  // 発行される(キャッシュ/共有はしない)ため、ここでrevokeしてもTrackList側の
  // 独立したobjectURLに影響しない。
  useEffect(() => {
    if (!audioVolumeRef.current || !stems || stems.length === 0) {
      playersRef.current = [];
      return;
    }
    let cancelled = false;
    const audioVolume = audioVolumeRef.current;
    const created: { name: string; player: Tone.Player }[] = [];
    // #34-M3レビュー指摘: 成功時のobjectURLも保持し、クリーンアップで解放する
    // (キャンセル時は既にrevokeしているが、成功時は`player.dispose()`だけでは
    // objectURL自体は解放されずリークしていた)。
    const createdUrls: string[] = [];

    Promise.all(
      stems.map(async (name) => {
        try {
          const url = await getStemAudioUrl(projectId, name);
          if (cancelled) {
            URL.revokeObjectURL(url);
            return;
          }
          const player = new Tone.Player();
          await player.load(url);
          if (cancelled) {
            player.dispose();
            URL.revokeObjectURL(url);
            return;
          }
          createdUrls.push(url);
          player.mute = !useMixStore.getState().isAudible(name);
          player.connect(audioVolume);
          player.sync().start(0);
          created.push({ name, player });
          playersRef.current = created;
        } catch (err) {
          if (!cancelled) setError((err as Error).message);
        }
      }),
    );

    return () => {
      cancelled = true;
      for (const { player } of created) player.dispose();
      for (const url of createdUrls) URL.revokeObjectURL(url);
      playersRef.current = [];
    };
  }, [stems, projectId]);

  // #34-M3レビュー指摘: ステムのソロ/ミュートをマウント時点だけでなくライブに
  // 反映する(FR-12の聴き比べ用途では、TrackList側の操作が再生に効かないと
  // 混乱しうるため)。`useMixStore`の生の`subscribe`(Reactフックではない)を使い、
  // `TransportBar`自体の再レンダーは発生させない(`playersRef`を直接更新する
  // だけで十分なため)。
  useEffect(() => {
    const unsubscribe = useMixStore.subscribe(() => {
      const { isAudible } = useMixStore.getState();
      for (const { name, player } of playersRef.current) {
        player.mute = !isAudible(name);
      }
    });
    return unsubscribe;
  }, []);

  function loop() {
    syncPosition();
    updateTimeText();
    if (Tone.getTransport().state === "started") {
      rafRef.current = requestAnimationFrame(loop);
    }
  }

  async function handlePlayPause() {
    setError(null);
    const transport = Tone.getTransport();
    if (transport.state === "started") {
      transport.pause();
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      usePlaybackStore.setState({ isPlaying: false });
      syncPosition();
      updateTimeText();
      return;
    }
    try {
      await Tone.start();
    } catch (err) {
      setError((err as Error).message);
      return;
    }
    // #34: 曲の終端(source.duration_sec)で自動停止する。再生開始のたびに
    // 直前のスケジュールが残っていれば重複するため、開始前に一旦クリアする。
    // #34-M3レビュー指摘: `transport.cancel(0)`はTransport上の全スケジュールを
    // 消去するため、MIDI再生用に`Tone.Part`が`.start(0)`で内部スケジュール
    // 済みのノートイベントも巻き添えで消えてしまう(Partは`notes`が変わらない
    // 限り作り直されないため、以降MIDIモードが無音になる)。前回の自動停止
    // イベントIDだけを`transport.clear()`で個別に解除する。
    if (stopEventIdRef.current != null) transport.clear(stopEventIdRef.current);
    stopEventIdRef.current = transport.scheduleOnce(() => {
      handleStop();
    }, durationSec);
    transport.start();
    usePlaybackStore.setState({ isPlaying: true });
    rafRef.current = requestAnimationFrame(loop);
  }

  function handleStop() {
    const transport = Tone.getTransport();
    transport.stop();
    transport.seconds = 0;
    if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    usePlaybackStore.setState({ isPlaying: false });
    syncPosition();
    updateTimeText();
  }

  function handleRewind() {
    Tone.getTransport().seconds = 0;
    syncPosition();
    updateTimeText();
  }

  function handleSetMode(next: PlaybackMode) {
    usePlaybackStore.getState().setMode(next);
    if (midiVolumeRef.current) midiVolumeRef.current.mute = next !== "midi";
    if (audioVolumeRef.current) audioVolumeRef.current.mute = next !== "audio";
  }

  if (!score) return null;

  return (
    <section className="flex flex-wrap items-center gap-4 rounded-lg border border-gray-200 p-4">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={handleRewind}
          className={`${BUTTON_CLASS} bg-gray-600 hover:bg-gray-700`}
          aria-label="先頭に戻る"
        >
          ⏮
        </button>
        <button
          type="button"
          onClick={() => void handlePlayPause()}
          className={`${BUTTON_CLASS} bg-blue-600 hover:bg-blue-700`}
          aria-label={isPlaying ? "一時停止" : "再生"}
        >
          {isPlaying ? "⏸" : "▶"}
        </button>
        <button
          type="button"
          onClick={handleStop}
          className={`${BUTTON_CLASS} bg-gray-600 hover:bg-gray-700`}
          aria-label="停止"
        >
          ⏹
        </button>
      </div>
      <span ref={timeTextRef} className="text-sm text-gray-700">
        {formatTime(usePlaybackStore.getState().positionSec)} / {formatTime(durationSec)}
      </span>
      <span className="text-sm text-gray-700">Bar {currentBar}</span>
      <span className="text-sm text-gray-700">♩={Math.round(bpm)}</span>
      <div className="flex items-center gap-1">
        <button
          type="button"
          aria-pressed={mode === "midi"}
          onClick={() => handleSetMode("midi")}
          className={MODE_BUTTON_CLASS(mode === "midi")}
        >
          MIDI
        </button>
        <button
          type="button"
          aria-pressed={mode === "audio"}
          onClick={() => handleSetMode("audio")}
          className={MODE_BUTTON_CLASS(mode === "audio")}
        >
          Audio
        </button>
      </div>
      {error && <p className="text-sm text-red-600">{error}</p>}
    </section>
  );
}
