import { useCallback, useEffect, useRef, useState } from "react";
import type { Beatmap } from "../api/client";
import { getBackendInfo } from "../lib/backendInfo";
import {
  beatsInRange,
  CLICK_DURATION_SEC,
  CLICK_LOOKAHEAD_SEC,
  clickFrequencyHz,
  DEFAULT_CLICK_VOLUME,
  downbeatTimeSet,
} from "../lib/beatClick";
import { formatSeconds } from "../lib/beatSummary";
import {
  fullView,
  panFraction,
  panView,
  pointDurationSec,
  rulerTicks,
  type TimeView,
  viewFromFraction,
  viewSpanSec,
  ZOOM_STEP,
  zoomFactor,
  zoomView,
} from "../lib/waveformView";
import { BeatGridOverlay } from "./BeatGridOverlay";
import { Waveform } from "./Waveform";

interface BeatWaveformViewerProps {
  projectId: string;
  peaks: number[][];
  durationSec: number;
  beatmap: Beatmap | null;
  view: TimeView;
  onViewChange: (view: TimeView) => void;
  height?: number;
}

/** 幅を測る前(初回レンダー)に目盛りを間引くための想定幅。 */
const FALLBACK_WIDTH_PX = 900;

/** クリック音の先読みループの間隔(ms)。 */
const CLICK_TICK_MS = 25;

/** 再生ヘッドがこの比率を外れたら表示区間を追従させる。 */
const FOLLOW_LEFT_RATIO = 0.05;
const FOLLOW_RIGHT_RATIO = 0.95;
/** 追従時に再生ヘッドを置く位置(表示区間の左端からの比率)。 */
const FOLLOW_TARGET_RATIO = 0.2;

/**
 * #169/#171: ビートグリッド補正用の波形ビュー。
 *
 * 表示区間(`view`)を明示的に持ち、拡大(ズーム)と横スクロールで任意の区間を
 * 等倍以上で見られるようにする(#169)。加えて、**推定したビートが正しいかを耳で
 * 確かめられる**よう、原音の再生・再生ヘッド・拍ごとのクリック音を提供する(#171)。
 * クリック音は音声要素の`timeupdate`(粗い)ではなく、Web Audioへ数十ms先まで
 * 予約する方式で鳴らす(そのままでは拍の間隔で正確に鳴らない)。
 *
 * 操作:
 * - 「＋」「−」ボタン: 表示中央を固定して拡大/縮小
 * - 「全体表示」: 全曲表示へ戻す
 * - ホイール: 横スクロール / Ctrl(⌘)+ホイール: ポインタ位置を固定して拡大
 * - 下部のスライダ: 横スクロール(キーボードの←→でも動く)
 * - 「再生」: 原音を再生し、拍にクリック音を重ねる(再生ヘッドは表示区間に追従)
 */
export function BeatWaveformViewer({
  projectId,
  peaks,
  durationSec,
  beatmap,
  view,
  onViewChange,
  height = 128,
}: BeatWaveformViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const playheadRef = useRef<HTMLDivElement>(null);
  const timeTextRef = useRef<HTMLSpanElement>(null);
  const [widthPx, setWidthPx] = useState(FALLBACK_WIDTH_PX);

  const [isPlaying, setIsPlaying] = useState(false);
  const [clickEnabled, setClickEnabled] = useState(true);
  const [clickVolume, setClickVolume] = useState(DEFAULT_CLICK_VOLUME);
  const [followPlayhead, setFollowPlayhead] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isLoadingAudio, setIsLoadingAudio] = useState(false);

  const audioRef = useRef<HTMLAudioElement | null>(null);
  /** 生成した音源の Blob URL(アンマウント時に revoke する)。 */
  const objectUrlRef = useRef<string | null>(null);
  /** 音源取得中の通信(アンマウント・プロジェクト切替で中断する)。 */
  const abortRef = useRef<AbortController | null>(null);
  /** 読み込み済みの音源がどのプロジェクトのものか。 */
  const loadedProjectIdRef = useRef<string | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const clickGainRef = useRef<GainNode | null>(null);
  /** 予約済みの最後の拍(シーク時に数え直す)。 */
  const lastClickSecRef = useRef(Number.NEGATIVE_INFINITY);
  /** 直前の再生位置(シーク検出用)。 */
  const lastPositionRef = useRef(0);
  const rafRef = useRef(0);
  /** 予約済みのクリック音(停止時に鳴り残らないよう止める)。 */
  const scheduledClicksRef = useRef<{ oscillator: OscillatorNode; endAt: number }[]>([]);

  // 先読みループと再生ヘッドの更新は毎フレーム走るため、依存する値はrefで読む
  // (レンダーごとに新しい配列やコールバックを依存に入れると、再生中にループが
  //  張り直されて拍が飛ぶ)。
  const beatTimesRef = useRef<number[]>([]);
  const downbeatSetRef = useRef<Set<number>>(new Set());
  const volumeRef = useRef(DEFAULT_CLICK_VOLUME);
  const viewRef = useRef(view);
  const spanRef = useRef(viewSpanSec(view));
  const durationRef = useRef(durationSec);
  const followRef = useRef(true);
  const onViewChangeRef = useRef(onViewChange);

  const span = viewSpanSec(view);
  const factor = zoomFactor(view, durationSec);
  const granularityMs = Math.round(pointDurationSec(peaks.length, durationSec) * 1000);
  const scrollable = span < durationSec - 1e-6;
  const ticks = rulerTicks(view, widthPx);

  useEffect(() => {
    const beatTimes = (beatmap?.beats ?? []).map((beat) => beat.time_sec).sort((a, b) => a - b);
    beatTimesRef.current = beatTimes;
    // ダウンビートは拍の時刻へ許容誤差つきで対応付ける(別経路の丸めで厳密一致が
    // 崩れると、クリック音の高低が無言で壊れるため。#173レビュー指摘)。
    downbeatSetRef.current = downbeatTimeSet(beatTimes, beatmap?.downbeats_sec ?? []);
  }, [beatmap]);

  useEffect(() => {
    volumeRef.current = clickEnabled ? clickVolume : 0;
    const context = audioContextRef.current;
    if (clickGainRef.current && context) {
      clickGainRef.current.gain.setValueAtTime(volumeRef.current, context.currentTime);
    }
  }, [clickEnabled, clickVolume]);

  useEffect(() => {
    followRef.current = followPlayhead;
  }, [followPlayhead]);

  useEffect(() => {
    viewRef.current = view;
    spanRef.current = span;
    durationRef.current = durationSec;
    onViewChangeRef.current = onViewChange;
  }, [view, span, durationSec, onViewChange]);

  /** 予約済みのクリック音を止める(一時停止・先頭へ戻すとき)。 */
  const stopScheduledClicks = useCallback(() => {
    for (const entry of scheduledClicksRef.current) {
      try {
        entry.oscillator.stop();
      } catch {
        // 既に停止した発振器への stop() は環境によって例外になるため無視する。
      }
    }
    scheduledClicksRef.current = [];
  }, []);

  /** 取得中の通信と音源を解放する(アンマウント・プロジェクト切替)。 */
  const releaseAudio = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    audioRef.current?.pause();
    audioRef.current = null;
    loadedProjectIdRef.current = null;
    if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    objectUrlRef.current = null;
    stopScheduledClicks();
  }, [stopScheduledClicks]);

  /**
   * 原音を認証付きで取得して`<audio>`を作る(`<audio src>`ではトークンを送れないため。
   * `AudioPlayer`と同じ理由)。
   *
   * 取得は**初回の再生まで遅延**させる。②を開いただけで数十MBの音源をメモリへ載せない
   * ようにするため。通信は`AbortController`で中断でき、画面を離れたらダウンロードも止める
   * (#173レビュー指摘)。
   */
  const ensureAudio = useCallback(async (): Promise<HTMLAudioElement | null> => {
    if (audioRef.current && loadedProjectIdRef.current === projectId) return audioRef.current;
    // 別プロジェクトの音源が残っている場合は、取得し直す前に必ず解放する
    // (上書きすると旧音源が鳴り続け、Blob URL もリークする。#173レビュー指摘)。
    if (audioRef.current || objectUrlRef.current) releaseAudio();
    if (abortRef.current) return null;

    const controller = new AbortController();
    abortRef.current = controller;
    setIsLoadingAudio(true);
    try {
      const { baseUrl, token } = await getBackendInfo();
      const headers = new Headers();
      if (token) headers.set("X-AME-Token", token);
      const resp = await fetch(`${baseUrl}/api/projects/${projectId}/audio/original`, {
        headers,
        signal: controller.signal,
      });
      if (!resp.ok) throw new Error(`音源の読み込みに失敗しました (${resp.status})`);
      const blob = await resp.blob();
      if (controller.signal.aborted) return null;

      objectUrlRef.current = URL.createObjectURL(blob);
      const audio = new Audio(objectUrlRef.current);
      audio.preload = "auto";
      audio.addEventListener("ended", () => setIsPlaying(false));
      audioRef.current = audio;
      loadedProjectIdRef.current = projectId;
      return audio;
    } catch (err) {
      // 中断(abort)は失敗ではないので、エラー表示しない。
      if (!controller.signal.aborted) setError((err as Error).message);
      return null;
    } finally {
      setIsLoadingAudio(false);
      if (abortRef.current === controller) abortRef.current = null;
    }
  }, [projectId, releaseAudio]);

  // 画面を離れる時に、通信・音源・AudioContextを片付ける(#173レビュー指摘)。
  useEffect(() => {
    return () => {
      releaseAudio();
      // AudioContextを開いたままにすると、画面を往復するたびに蓄積するため閉じる。
      // 次に再生したときに作り直す。
      void audioContextRef.current?.close().catch(() => undefined);
      audioContextRef.current = null;
      clickGainRef.current = null;
    };
  }, [releaseAudio]);

  // 目盛りの間引きは実際の表示幅で決めるため、幅を測って追従させる。
  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;

    const update = () => setWidthPx(element.clientWidth || FALLBACK_WIDTH_PX);
    update();
    if (typeof ResizeObserver === "undefined") return;

    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  // Reactの`onWheel`はパッシブ登録になり`preventDefault`できないため、DOMへ直接登録する
  // (`preventDefault`しないと、ホイールで拡大したついでに画面全体がスクロールしてしまう)。
  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;

    function handleWheel(event: WheelEvent) {
      const rect = element?.getBoundingClientRect();
      if (!rect || rect.width <= 0) return;
      event.preventDefault();

      const currentView = viewRef.current;
      const currentSpan = spanRef.current;
      const timeAtCursor =
        currentView.startSec + ((event.clientX - rect.left) / rect.width) * currentSpan;
      if (event.ctrlKey || event.metaKey) {
        // ポインタ位置を固定して拡大/縮小する。
        const zoomIn = event.deltaY < 0;
        onViewChangeRef.current(
          zoomView(
            currentView,
            zoomIn ? ZOOM_STEP : 1 / ZOOM_STEP,
            timeAtCursor,
            durationRef.current,
          ),
        );
        return;
      }

      // 横スクロール(トラックパッドの横成分も拾う)。
      const deltaPx = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
      onViewChangeRef.current(
        panView(currentView, (deltaPx / rect.width) * currentSpan, durationRef.current),
      );
    }

    element.addEventListener("wheel", handleWheel, { passive: false });
    return () => element.removeEventListener("wheel", handleWheel);
  }, []);

  /** 拍のクリック音を先読みして予約する(25msごとに呼ばれる)。 */
  const scheduleClicks = useCallback(() => {
    const audio = audioRef.current;
    const context = audioContextRef.current;
    const gain = clickGainRef.current;
    if (!audio || !context || !gain || gain.gain.value <= 0) return;

    const now = audio.currentTime;
    const previous = lastPositionRef.current;
    lastPositionRef.current = now;
    // シーク(前後どちらでも)を検出したら、予約済みの続きから外して数え直す。
    if (now < previous - 1e-3 || now - previous > 1) {
      lastClickSecRef.current = Number.NEGATIVE_INFINITY;
    }

    const from = Math.max(now, lastClickSecRef.current + 1e-6);
    const until = now + CLICK_LOOKAHEAD_SEC;
    for (const timeSec of beatsInRange(beatTimesRef.current, from, until)) {
      const startAt = context.currentTime + Math.max(timeSec - now, 0);
      const oscillator = context.createOscillator();
      const envelope = context.createGain();
      oscillator.frequency.value = clickFrequencyHz(downbeatSetRef.current.has(timeSec));
      envelope.gain.setValueAtTime(0, startAt);
      envelope.gain.linearRampToValueAtTime(1, startAt + 0.002);
      envelope.gain.exponentialRampToValueAtTime(0.001, startAt + CLICK_DURATION_SEC);
      oscillator.connect(envelope).connect(gain);
      oscillator.start(startAt);
      const endAt = startAt + CLICK_DURATION_SEC + 0.02;
      oscillator.stop(endAt);
      lastClickSecRef.current = timeSec;

      // 鳴り終わった予約は捨てる(停止時にまとめて止められるように保持する)。
      scheduledClicksRef.current = scheduledClicksRef.current.filter(
        (entry) => entry.endAt > context.currentTime,
      );
      scheduledClicksRef.current.push({ oscillator, endAt });
    }
  }, []);

  /** 再生ヘッドと時刻表示を更新し、必要なら表示区間を追従させる。 */
  const updatePlayhead = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;

    const time = audio.currentTime;
    const currentView = viewRef.current;
    const currentSpan = spanRef.current;
    const ratio = (time - currentView.startSec) / currentSpan;

    if (playheadRef.current) {
      playheadRef.current.style.left = `${ratio * 100}%`;
      playheadRef.current.style.opacity = ratio < 0 || ratio > 1 ? "0" : "1";
    }
    if (timeTextRef.current) timeTextRef.current.textContent = formatSeconds(time);

    // 追従は「表示区間から出たとき」だけ状態を更新する(毎フレーム再レンダーさせない)。
    if (
      followRef.current &&
      currentSpan < durationRef.current - 1e-6 &&
      (ratio < FOLLOW_LEFT_RATIO || ratio > FOLLOW_RIGHT_RATIO)
    ) {
      const target = time - currentSpan * FOLLOW_TARGET_RATIO;
      onViewChangeRef.current(
        panView(currentView, target - currentView.startSec, durationRef.current),
      );
    }
  }, []);

  // 再生中だけ先読みループと再生ヘッドの描画を回す。
  useEffect(() => {
    if (!isPlaying) return;

    const timer = window.setInterval(scheduleClicks, CLICK_TICK_MS);
    const frame = () => {
      updatePlayhead();
      rafRef.current = window.requestAnimationFrame(frame);
    };
    rafRef.current = window.requestAnimationFrame(frame);

    return () => {
      window.clearInterval(timer);
      window.cancelAnimationFrame(rafRef.current);
    };
  }, [isPlaying, scheduleClicks, updatePlayhead]);

  /**
   * クリック音の出力を用意する。音声出力が使えない環境(CI等)でも再生自体は
   * 続けたいので、失敗しても例外を投げない。
   */
  function ensureClickGain(): GainNode | null {
    try {
      if (!audioContextRef.current) {
        audioContextRef.current = new AudioContext();
      }
      const context = audioContextRef.current;
      if (!clickGainRef.current) {
        const gain = context.createGain();
        gain.connect(context.destination);
        clickGainRef.current = gain;
      }
      clickGainRef.current.gain.value = volumeRef.current;
      return clickGainRef.current;
    } catch {
      return null;
    }
  }

  async function togglePlayback() {
    setError(null);

    if (isPlaying) {
      audioRef.current?.pause();
      // 先読みしていた分が鳴り残らないように止める(#173レビュー指摘)。
      stopScheduledClicks();
      setIsPlaying(false);
      return;
    }

    const audio = await ensureAudio();
    if (!audio) return;

    ensureClickGain();
    void audioContextRef.current?.resume?.();
    // 停止→再生で、前回予約した拍を飛ばさないように数え直す。
    lastClickSecRef.current = Number.NEGATIVE_INFINITY;
    lastPositionRef.current = audio.currentTime;
    try {
      await audio.play();
      setIsPlaying(true);
    } catch (err) {
      setError(`再生できませんでした: ${(err as Error).message}`);
    }
  }

  function rewind() {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = 0;
    lastClickSecRef.current = Number.NEGATIVE_INFINITY;
    lastPositionRef.current = 0;
    stopScheduledClicks();
    if (playheadRef.current) {
      playheadRef.current.style.left = "-100%";
      playheadRef.current.style.opacity = "0";
    }
    if (timeTextRef.current) timeTextRef.current.textContent = formatSeconds(0);
  }

  function zoomAtCenter(nextFactor: number) {
    onViewChange(zoomView(view, nextFactor, view.startSec + span / 2, durationSec));
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-gray-600 text-sm dark:text-gray-300">
        <div className="flex items-center gap-1">
          <button
            type="button"
            aria-label="ズームアウト"
            onClick={() => zoomAtCenter(1 / ZOOM_STEP)}
            className="h-7 w-7 rounded-md border border-gray-300 text-base leading-none hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-800"
          >
            −
          </button>
          <button
            type="button"
            aria-label="ズームイン"
            onClick={() => zoomAtCenter(ZOOM_STEP)}
            className="h-7 w-7 rounded-md border border-gray-300 text-base leading-none hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-800"
          >
            ＋
          </button>
          <button
            type="button"
            onClick={() => onViewChange(fullView(durationSec))}
            disabled={!scrollable}
            className="rounded-md border border-gray-300 px-2 py-1 text-xs hover:bg-gray-100 disabled:opacity-50 dark:border-gray-600 dark:hover:bg-gray-800"
          >
            全体表示
          </button>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => void togglePlayback()}
            disabled={!beatmap || isLoadingAudio}
            className="rounded-md bg-blue-600 px-3 py-1 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {isLoadingAudio ? "読み込み中…" : isPlaying ? "一時停止" : "再生"}
          </button>
          <button
            type="button"
            onClick={rewind}
            disabled={!beatmap}
            className="rounded-md border border-gray-300 px-2 py-1 text-xs hover:bg-gray-100 disabled:opacity-50 dark:border-gray-600 dark:hover:bg-gray-800"
          >
            先頭へ
          </button>
          <span className="text-xs tabular-nums" data-testid="beat-position">
            再生位置 <span ref={timeTextRef}>0.00 秒</span>
          </span>
        </div>
        <label className="flex items-center gap-1 text-xs">
          <input
            type="checkbox"
            checked={clickEnabled}
            onChange={(event) => setClickEnabled(event.target.checked)}
            className="h-3.5 w-3.5"
          />
          ビートにクリック音(小節の頭は高く)
        </label>
        <label className="flex items-center gap-1 text-xs">
          音量
          <input
            type="range"
            aria-label="クリック音の音量"
            min={0}
            max={100}
            step={1}
            value={Math.round(clickVolume * 100)}
            disabled={!clickEnabled}
            onChange={(event) => setClickVolume(Number(event.target.value) / 100)}
            className="h-1 w-24 cursor-pointer accent-blue-600 disabled:opacity-50"
          />
        </label>
        <label className="flex items-center gap-1 text-xs">
          <input
            type="checkbox"
            checked={followPlayhead}
            onChange={(event) => setFollowPlayhead(event.target.checked)}
            className="h-3.5 w-3.5"
          />
          再生位置に追従
        </label>
        <span className="text-xs">
          表示幅 {formatSeconds(span)}(全体 {formatSeconds(durationSec)}・倍率 {factor.toFixed(1)}×)
        </span>
        <span className="text-gray-500 text-xs dark:text-gray-400">
          波形の粒度 {granularityMs} ms/点
        </span>
      </div>

      {error && <p className="text-red-600 text-sm dark:text-red-400">{error}</p>}

      <div className="overflow-hidden rounded-md border border-gray-200 dark:border-gray-700">
        <div ref={containerRef} className="relative">
          <div className="relative h-4 border-gray-200 border-b dark:border-gray-700">
            {ticks.map((tick) => (
              <span
                key={tick.timeSec}
                className="pointer-events-none absolute top-0.5 -translate-x-1/2 text-[10px] text-gray-400 dark:text-gray-500"
                style={{ left: `${tick.ratio * 100}%` }}
              >
                {tick.label}
              </span>
            ))}
          </div>
          <div className="relative">
            <Waveform peaks={peaks} durationSec={durationSec} view={view} height={height} />
            {beatmap && <BeatGridOverlay beatmap={beatmap} durationSec={durationSec} view={view} />}
            <div
              ref={playheadRef}
              data-testid="beat-playhead"
              className="pointer-events-none absolute inset-y-0 w-px bg-red-500"
              style={{ left: "-100%", opacity: 0 }}
            />
          </div>
        </div>
        <div className="flex items-center gap-2 border-gray-200 border-t px-2 py-1 dark:border-gray-700">
          <span className="text-[10px] text-gray-400 dark:text-gray-500">横スクロール</span>
          <input
            type="range"
            aria-label="横スクロール"
            min={0}
            max={1000}
            step={1}
            value={Math.round(panFraction(view, durationSec) * 1000)}
            disabled={!scrollable}
            onChange={(event) =>
              onViewChange(viewFromFraction(Number(event.target.value) / 1000, span, durationSec))
            }
            className="h-1 flex-1 cursor-pointer accent-blue-600 disabled:opacity-50"
          />
        </div>
      </div>
    </div>
  );
}
