import { useEffect, useRef, useState } from "react";
import * as Tone from "tone";
import { getStemAudioUrl } from "../api/client";
import { useScore } from "../hooks/useScore";
import { useStems } from "../hooks/useStems";
import { barBoundariesTicks, barNumberForTick } from "../lib/pianoRoll";
import { bpmAtBar, formatTime, secondsToTick } from "../lib/transport";
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
 * 限定「先頭パートのみ編集/表示対象」を踏襲)。ステムは`useMixStore`
 * (`TrackList`と共有)の**マウント時点**のソロ/ミュート状態のみ初期値として
 * 反映する(その後のTrackList側の操作にライブ追従はしない、意図的なスコープ
 * 限定)。
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
  const positionSec = usePlaybackStore((s) => s.positionSec);
  const currentBar = usePlaybackStore((s) => s.currentBar);
  const bpm = usePlaybackStore((s) => s.bpm);

  const synthRef = useRef<Tone.PolySynth | null>(null);
  const partRef = useRef<Tone.Part | null>(null);
  const midiVolumeRef = useRef<Tone.Volume | null>(null);
  const audioVolumeRef = useRef<Tone.Volume | null>(null);
  const playersRef = useRef<Tone.Player[]>([]);
  const rafRef = useRef<number | null>(null);
  const boundariesRef = useRef<number[]>([0]);
  const tickMappingNotesRef = useRef<{ onset_sec: number; onset_tick: number | null }[]>([]);

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

  // 小節境界(tick<->秒変換用の点列/BPM表示用のtempo_mapはstateではなくrefに
  // 保持し、rAFループ(高頻度)から毎回`score`をクロージャ経由で読まなくても
  // 済むようにする(#30のドラッグプレビューと同じ「高頻度アクセスはrefで」方針)。
  useEffect(() => {
    tickMappingNotesRef.current = notes
      .filter((n) => n.onset_tick != null)
      .map((n) => ({ onset_sec: n.onset_sec, onset_tick: n.onset_tick }));
    const lastTick = notes.reduce(
      (max, n) => Math.max(max, (n.onset_tick ?? 0) + (n.duration_tick ?? 0)),
      1,
    );
    boundariesRef.current = barBoundariesTicks(timeSignatures, divisions, lastTick);
  }, [notes, timeSignatures, divisions]);

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
  // NFR-17)をそのまま再利用する。
  useEffect(() => {
    if (!audioVolumeRef.current || !stems || stems.length === 0) {
      playersRef.current = [];
      return;
    }
    let cancelled = false;
    const audioVolume = audioVolumeRef.current;
    const created: Tone.Player[] = [];

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
          player.mute = !useMixStore.getState().isAudible(name);
          player.connect(audioVolume);
          player.sync().start(0);
          created.push(player);
          playersRef.current = created;
        } catch (err) {
          if (!cancelled) setError((err as Error).message);
        }
      }),
    );

    return () => {
      cancelled = true;
      for (const player of created) player.dispose();
      playersRef.current = [];
    };
  }, [stems, projectId]);

  function syncPosition() {
    const positionSec = Tone.getTransport().seconds;
    const positionTick = secondsToTick(tickMappingNotesRef.current, positionSec);
    const currentBar = barNumberForTick(boundariesRef.current, positionTick);
    const bpm = bpmAtBar(tempoMap, currentBar);
    usePlaybackStore.setState({ positionSec, positionTick, currentBar, bpm });
  }

  function loop() {
    syncPosition();
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
    transport.cancel(0);
    transport.scheduleOnce(() => {
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
  }

  function handleRewind() {
    Tone.getTransport().seconds = 0;
    syncPosition();
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
      <span className="text-sm text-gray-700">
        {formatTime(positionSec)} / {formatTime(durationSec)}
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
