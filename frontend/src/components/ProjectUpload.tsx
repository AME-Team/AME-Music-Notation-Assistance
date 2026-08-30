import { type DragEvent, useRef, useState } from "react";
import { useCreateProject } from "../hooks/useProjects";

const ACCEPTED_EXTENSIONS = [".mp3", ".wav", ".flac", ".m4a"];

export function ProjectUpload() {
  const [isDragging, setIsDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const createProject = useCreateProject();

  async function uploadFile(file: File) {
    await createProject.mutateAsync(file);
  }

  function handleDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setIsDragging(false);
    const file = e.dataTransfer.files[0];
    if (file) void uploadFile(file);
  }

  async function handleBrowseClick() {
    if (window.api) {
      const picked = await window.api.openFileDialog();
      if (!picked || picked.length === 0) return;
      const { name, data } = picked[0];
      await uploadFile(new File([new Uint8Array(data)], name));
    } else {
      inputRef.current?.click();
    }
  }

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: ドラッグ&ドロップ領域に相当するARIAロールは無い。クリック操作は下の<button>が担う。
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setIsDragging(true);
      }}
      onDragLeave={() => setIsDragging(false)}
      onDrop={handleDrop}
      className={`flex flex-col items-center justify-center gap-3 rounded-lg border-2 border-dashed p-10 text-center transition-colors ${
        isDragging ? "border-blue-500 bg-blue-50" : "border-gray-300 bg-gray-50"
      }`}
    >
      <p className="text-sm text-gray-600">MP3 / WAV / FLAC / M4A をここにドラッグ&ドロップ</p>
      <button
        type="button"
        onClick={handleBrowseClick}
        disabled={createProject.isPending}
        className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
      >
        {createProject.isPending ? "アップロード中..." : "ファイルを選択"}
      </button>
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPTED_EXTENSIONS.join(",")}
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) void uploadFile(file);
          e.target.value = "";
        }}
      />
      {createProject.isError && (
        <p className="text-sm text-red-600">{(createProject.error as Error).message}</p>
      )}
    </div>
  );
}
