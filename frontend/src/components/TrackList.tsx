import { useEffect, useRef, useState } from "react";
import { getStemAudioUrl } from "../api/client";
import { useStems } from "../hooks/useStems";
import { useMixStore } from "../stores/mixStore";

interface TrackRowProps {
  projectId: string;
  name: string;
}

function TrackRow({ projectId, name }: TrackRowProps) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const objectUrlRef = useRef<string | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const muted = useMixStore((s) => s.muted.has(name));
  const soloed = useMixStore((s) => s.soloed.has(name));
  const audible = useMixStore((s) => s.isAudible(name));
  const toggleMute = useMixStore((s) => s.toggleMute);
  const toggleSolo = useMixStore((s) => s.toggleSolo);

  useEffect(() => {
    if (audioRef.current) audioRef.current.muted = !audible;
  }, [audible]);

  const isMountedRef = useRef(true);
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    };
  }, []);

  async function handlePlayPause() {
    const audio = audioRef.current;
    if (!audio) return;
    if (isPlaying) {
      audio.pause();
      return;
    }
    setError(null);
    if (!objectUrlRef.current) {
      setIsLoading(true);
      let url: string;
      try {
        url = await getStemAudioUrl(projectId, name);
      } catch (err) {
        if (isMountedRef.current) {
          setError((err as Error).message);
          setIsLoading(false);
        }
        return;
      }
      if (!isMountedRef.current) {
        // アンマウント後にfetchが解決した場合(#21-M1レビュー指摘): 上の
        // クリーンアップは既に実行済みで`objectUrlRef.current`もnullのままの
        // ため、ここで作られたobjectURLは誰にも解放されずリークする。
        // 即座に解放して終える。
        URL.revokeObjectURL(url);
        return;
      }
      objectUrlRef.current = url;
      audio.src = url;
      audio.muted = !audible;
      setIsLoading(false);
    }
    try {
      // `HTMLMediaElement.play()` は自動再生ポリシーやデコード失敗等で reject
      // しうる(#21-M1レビュー指摘)。無視すると unhandled rejection になる。
      await audio.play();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <li className="flex items-center gap-4 py-2">
      <span className="w-28 truncate text-sm font-medium text-gray-900">{name}</span>
      <button
        type="button"
        onClick={() => void handlePlayPause()}
        disabled={isLoading}
        className="rounded-md bg-gray-800 px-3 py-1 text-xs font-medium text-white hover:bg-gray-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50"
      >
        {isLoading ? "読込中" : isPlaying ? "一時停止" : "再生"}
      </button>
      <button
        type="button"
        aria-pressed={muted}
        onClick={() => toggleMute(name)}
        className={`rounded-md px-3 py-1 text-xs font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 ${
          muted ? "bg-red-600 text-white" : "bg-gray-100 text-gray-700 hover:bg-gray-200"
        }`}
      >
        ミュート
      </button>
      <button
        type="button"
        aria-pressed={soloed}
        onClick={() => toggleSolo(name)}
        className={`rounded-md px-3 py-1 text-xs font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 ${
          soloed ? "bg-amber-500 text-white" : "bg-gray-100 text-gray-700 hover:bg-gray-200"
        }`}
      >
        ソロ
      </button>
      {error && <span className="text-xs text-red-600">{error}</span>}
      {/* biome-ignore lint/a11y/useMediaCaption: 楽器音源に字幕は適用対象外 */}
      <audio
        ref={audioRef}
        onPlay={() => setIsPlaying(true)}
        onPause={() => setIsPlaying(false)}
        onEnded={() => setIsPlaying(false)}
        hidden
      />
    </li>
  );
}

interface TrackListProps {
  projectId: string;
}

/** #21: ステムごとのソロ/ミュート/個別再生を提供する。 */
export function TrackList({ projectId }: TrackListProps) {
  const { data: stems, isLoading, error } = useStems(projectId);
  const reset = useMixStore((s) => s.reset);

  useEffect(() => {
    // マウント時に1度だけリセットする。プロジェクト切替時に前のプロジェクトの
    // ソロ/ミュート状態を引き継がないことは、呼び出し元(ProjectWorkspace)が
    // `key={projectId}` を指定してこのコンポーネントごと再マウントすることで
    // 保証する(ソロ/ミュートはグローバルなzustandストアの状態のため、
    // 再マウントするだけでは自動的にはリセットされない)。
    reset();
    // resetはzustandのアクションで参照が変わらないため、依存配列に含めても
    // マウント時の1回のみ実行される(プロジェクト切替時の再実行は、呼び出し元の
    // `key={projectId}` による再マウントで保証する)。
  }, [reset]);

  if (isLoading) return <p className="text-sm text-gray-500">読み込み中...</p>;
  if (error) return <p className="text-sm text-red-600">{(error as Error).message}</p>;
  if (!stems || stems.length === 0) {
    return (
      <p className="text-sm text-gray-500">まだステムがありません。音源分離を実行してください。</p>
    );
  }

  return (
    <ul className="divide-y divide-gray-100">
      {stems.map((name) => (
        <TrackRow key={name} projectId={projectId} name={name} />
      ))}
    </ul>
  );
}
