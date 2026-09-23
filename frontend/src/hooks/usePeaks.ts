import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { getPeaks } from "../api/client";
import { DEFAULT_PEAKS_BUCKETS } from "../lib/waveformView";

/**
 * `name` は "original" またはステム名(#21)。
 *
 * `buckets` は波形の解像度(点の数。#169)。拡大表示では高解像度を要求し、
 * バックエンドは解像度ごとに別ファイルへキャッシュする。
 *
 * `keepPreviousData` を指定するのは、拡大のたびに解像度が変わってクエリキーが
 * 変わるとき、直前の波形を表示し続けるため(切り替えのたびに波形が消えて
 * チラつくと、拍の位置を目で追えなくなる)。
 */
export function usePeaks(projectId: string, name: string, buckets: number = DEFAULT_PEAKS_BUCKETS) {
  return useQuery({
    queryKey: ["peaks", projectId, name, buckets],
    queryFn: () => getPeaks(projectId, name, buckets),
    placeholderData: keepPreviousData,
  });
}
