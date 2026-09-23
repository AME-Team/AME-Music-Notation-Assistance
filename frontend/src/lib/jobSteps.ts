/**
 * UI刷新: `backend/app/worker/dsp_main.py`が送る`stage`+`step`(機械可読キー)を
 * 日本語ラベルに変換する。バックエンドの内部処理名(英語のライブラリ名など)を
 * そのまま画面に出さないためのもの。未知のキーは`step`をそのまま返す
 * (バックエンドの追加に画面が追従し忘れても、完全に無表示にはしない)。
 */
const STAGE_LABEL: Record<string, string> = {
  dummy: "動作確認",
  separate: "音源分離",
  beat: "テンポ・拍の検出",
  transcribe: "採譜",
  quantize: "リズム補正",
};

const STEP_LABEL: Record<string, Record<string, string>> = {
  transcribe: {
    piano: "ピアノを採譜中",
    bass: "ベースを採譜中",
    vocals: "ボーカルを採譜中",
    guitar: "ギターを採譜中",
    other: "その他のパートを採譜中",
    save: "結果を保存中",
  },
  quantize: {
    piano: "ピアノのリズムを補正中",
    bass: "ベースのリズムを補正中",
    vocals: "ボーカルのリズムを補正中",
    guitar: "ギターのリズムを補正中",
    other: "その他のパートのリズムを補正中",
    chords: "コード進行を推定中",
    save: "結果を保存中",
  },
};

export function stageLabel(stage: string | undefined): string {
  if (!stage) return "処理";
  return STAGE_LABEL[stage] ?? stage;
}

export function stepLabel(stage: string | undefined, step: string | undefined): string | null {
  if (!stage || !step) return null;
  return STEP_LABEL[stage]?.[step] ?? step;
}
