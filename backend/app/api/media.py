"""メディア・解析 API(#21, §11.6, §14)。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.api.deps import get_project_service, get_settings
from app.api.schemas import Beatmap, BeatmapEditRequest, PeaksResponse
from app.config import Settings
from app.infra import storage
from app.pipeline import beatmap_edit
from app.pipeline.peaks import compute_peaks
from app.services.project_service import ProjectNotFoundError, ProjectService

router = APIRouter(prefix="/api/projects/{project_id}", tags=["media"])


def _ensure_project_exists(project_id: str, service: ProjectService) -> None:
    try:
        service.get_project(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc


@router.get("/audio/original")
async def get_original_audio(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> FileResponse:
    """§11.6: 原曲配信。HTTP Range 対応(Starlette FileResponse がネイティブに対応)。

    原曲は拡張子が可変(mp3/wav/flac/m4a)なため、DBに記録された `audio_format` を
    介して `service.audio_path()` で解決する(`storage.find_original_audio()` の
    ようなファイルシステム側の拡張子探索には頼らない)。一方、分離後のステムは
    常に固定で `.wav`(#16の出力仕様)であり、DBに形式を持たせる意味が無いため、
    下の `get_stem_audio` は `storage` 経由で直接パスを組み立てる。同一ファイル内で
    解決方式が2通りあるのは、この拡張子が可変か固定かの違いに起因する意図的な差分。
    """
    _ensure_project_exists(project_id, service)
    path = service.audio_path(project_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="audio file not found")
    return FileResponse(path)


@router.get("/audio/stems/{name}")
async def get_stem_audio(
    project_id: str,
    name: str,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """#16/#21: ステム配信。Range 対応(既存の /audio/original と同じパターン)。

    ステムは常に `.wav` 固定(#16)なのでDB参照は不要。`get_original_audio` との
    パス解決方式の違いについては同関数のdocstringを参照。
    """
    _ensure_project_exists(project_id, service)
    path = storage.stems_dir(settings.workspace_dir, project_id) / f"{name}.wav"
    if not path.exists():
        raise HTTPException(status_code=404, detail="stem not found")
    return FileResponse(path)


@router.get("/analysis/peaks/{name}", response_model=PeaksResponse)
def get_peaks(
    project_id: str,
    name: str,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    """#21: 波形ピークデータ。初回計算し `analysis/peaks/{name}.json` にキャッシュする。

    `async def` ではなく通常の `def` にする(#20-M1レビュー指摘の追加ラウンド)。
    初回計算時は `compute_peaks`(`sf.read` によるWAV全体の読み込み+numpy計算)を
    呼ぶため、`async def` のままだとイベントループを直接ブロックし、SSEでのジョブ
    進捗配信など他の同時リクエスト全体を止めてしまう(NFR-04の「重いDSPはWorker
    プロセスへ分離する」方針とも矛盾する)。FastAPIは同期`def`のエンドポイントを
    自動的にスレッドプールで実行するため、これだけでイベントループを塞がなくなる。
    """
    _ensure_project_exists(project_id, service)

    cache_path = storage.peaks_path(settings.workspace_dir, project_id, name)
    if cache_path.exists():
        return storage.read_json(cache_path)

    if name == "original":
        # プロジェクト存在は上の _ensure_project_exists で確認済み。
        audio_path = service.audio_path(project_id)
    else:
        audio_path = storage.stems_dir(settings.workspace_dir, project_id) / f"{name}.wav"

    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="audio file not found")

    result = compute_peaks(str(audio_path))
    storage.write_json(cache_path, result)
    return result


@router.get("/analysis/beatmap", response_model=Beatmap)
async def get_beatmap(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    """#18: `beatmap.json` をそのまま返す。"""
    _ensure_project_exists(project_id, service)
    path = storage.beatmap_path(settings.workspace_dir, project_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="beatmap not found")
    return storage.read_json(path)


@router.patch("/analysis/beatmap", response_model=Beatmap)
async def patch_beatmap(
    project_id: str,
    body: BeatmapEditRequest,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    """#20 BeatGridEditor: 手動補正を beatmap.json に反映する(FR-04)。"""
    _ensure_project_exists(project_id, service)
    path = storage.beatmap_path(settings.workspace_dir, project_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="beatmap not found")

    original = storage.read_json(path)
    beatmap = original
    applied_any = False
    try:
        if body.offset_sec is not None:
            # apply_offset は offset_sec==0 の場合、no-opとして入力を無変更で
            # 返す設計にしてある(apply_bpm_override/rotate_downbeatと同じ
            # パターン)。それ以外は build_beatmap 経由で confidence 等も
            # 再計算した新しい dict になるため、内容比較(`!=`)ではなく
            # オブジェクト同一性(`is not`)で「呼び出し先が本当にno-opと
            # 判断したか」を検出する(#20-M1レビュー指摘の追加ラウンド)。
            before = beatmap
            beatmap = beatmap_edit.apply_offset(beatmap, body.offset_sec)
            applied_any = applied_any or beatmap is not before
        if body.bpm_override is not None:
            # apply_bpm_override は bpm<=0 の場合、no-opとして入力を無変更で
            # 返す(呼び出し元(=ここ)へ変更検出を委ねる設計)。それを
            # applied_any=True 扱いすると、実際には何も変わっていないのに
            # source が "manual" に書き換わってしまう(#20レビュー指摘)。
            before = beatmap
            beatmap = beatmap_edit.apply_bpm_override(beatmap, body.bpm_override)
            applied_any = applied_any or beatmap is not before
        if body.rotate_downbeat:
            # rotate_downbeat はビートが1つも無い場合、および回転先を持つ
            # ダウンビートが1つも残せない(全滅する)場合に no-op で入力を
            # そのまま返す(#20-M1レビュー指摘の追加ラウンド: 後者を許容すると
            # 小節構造が壊れた beatmap を無言で書き込んでしまうため)。他の3操作
            # と同様に `is not` で確実に検出できる。
            before = beatmap
            beatmap = beatmap_edit.rotate_downbeat(beatmap)
            applied_any = applied_any or beatmap is not before
        if body.time_signature_override is not None:
            # rotate_downbeat を同一リクエストで先に適用している場合、小節数が
            # 減っているため bar の妥当性はここで(rotate適用後の状態に対して)
            # 検証する必要がある。override_time_signature 自身が range を検証する。
            # 指定拍子が既存と完全一致(実質no-op)の場合は入力をそのまま返す
            # 設計になっているため、他の3操作と同様に `is not` で検出する
            # (#20-M1レビュー指摘の追加ラウンド)。
            override = body.time_signature_override
            before = beatmap
            beatmap = beatmap_edit.override_time_signature(
                beatmap,
                bar=override.bar,
                numerator=override.numerator,
                denominator=override.denominator,
            )
            applied_any = applied_any or beatmap is not before
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 何も補正が指定されていないPATCH(全フィールドNone/False)でsourceを"manual"に
    # 書き換えると、実際には何も変わっていないのにUIが「手動補正済み」と誤表示する。
    if applied_any:
        # 楽観的並行性制御(#20-M1レビュー指摘の追加ラウンド): worker/dsp_main.py の
        # beatステージと対称に、書き込み直前にファイルを再読込し、リクエスト開始時に
        # 読んだ内容(`original`)と食い違っていないか確認する。読み込みからここまでの
        # 間にbeatステージが新しい推論結果を書き込んでいた場合、それを古い手動補正
        # ベースの内容で silently 上書きしてしまう lost-update を防ぐ。
        current_on_disk = storage.read_json(path) if path.exists() else None
        if current_on_disk != original:
            raise HTTPException(
                status_code=409,
                detail="beatmap.json was modified concurrently (e.g. by a running beat "
                "estimation job); please retry with the latest beatmap",
            )
        beatmap["source"] = "manual"
        storage.write_json(path, beatmap)
    return beatmap
