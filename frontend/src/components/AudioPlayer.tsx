import { useEffect, useRef, useState } from "react";
import WaveSurfer from "wavesurfer.js";
import { getBackendInfo } from "../lib/backendInfo";

interface AudioPlayerProps {
  projectId: string;
}

export function AudioPlayer({ projectId }: AudioPlayerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const wavesurferRef = useRef<WaveSurfer | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;

    async function load() {
      if (!containerRef.current) return;
      try {
        // wavesurfer.js の内部 fetch はカスタムヘッダ(認証トークン)を付けられないため、
        // 認証済み fetch で取得した Blob を objectURL 化して渡す。
        const { baseUrl, token } = await getBackendInfo();
        const headers = new Headers();
        if (token) headers.set("X-AME-Token", token);
        const resp = await fetch(`${baseUrl}/api/projects/${projectId}/audio/original`, {
          headers,
        });
        if (!resp.ok) throw new Error(`failed to load audio: ${resp.status}`);
        const blob = await resp.blob();
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);

        const ws = WaveSurfer.create({
          container: containerRef.current,
          waveColor: "#93c5fd",
          progressColor: "#2563eb",
          height: 64,
        });
        ws.on("play", () => setIsPlaying(true));
        ws.on("pause", () => setIsPlaying(false));
        ws.on("finish", () => setIsPlaying(false));
        ws.load(objectUrl);
        wavesurferRef.current = ws;
      } catch (err) {
        if (!cancelled) setError((err as Error).message);
      }
    }

    void load();
    return () => {
      cancelled = true;
      wavesurferRef.current?.destroy();
      wavesurferRef.current = null;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [projectId]);

  if (error) return <p className="text-sm text-red-600">{error}</p>;

  return (
    <div className="rounded border border-gray-200 p-3">
      <div ref={containerRef} />
      <button
        type="button"
        onClick={() => wavesurferRef.current?.playPause()}
        className="mt-2 rounded bg-gray-800 px-3 py-1 text-sm text-white hover:bg-gray-900"
      >
        {isPlaying ? "一時停止" : "再生"}
      </button>
    </div>
  );
}
