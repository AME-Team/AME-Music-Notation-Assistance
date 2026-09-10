"""メディア・解析 API(#21, §11.6, §14)。"""

from __future__ import annotations

import logging

import soundfile as sf
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.api.deps import ensure_project_exists, get_project_service, get_settings
from app.api.schemas import Beatmap, BeatmapEditRequest, PeaksResponse, StemListResponse
from app.config import Settings
from app.infra import storage
from app.pipeline import beatmap_edit
from app.pipeline.peaks import compute_peaks
from app.services import stage_invalidation
from app.services.project_service import ProjectService

router = APIRouter(prefix="/api/projects/{project_id}", tags=["media"])


_AUDIO_BINARY_SCHEMA = {"type": "string", "format": "binary"}


def _audio_responses(media_type: str) -> dict:
    """バイナリ音声配信エンドポイント用の `responses=` を組み立てる。

    `get_original_audio`/`get_stem_audio` で同一構造(200+206、`_AUDIO_BINARY_SCHEMA`)
    を重複させると、将来ステータスやスキーマを変える際に2箇所を揃え忘れて食い違う
    リスクがあるため、共通ヘルパーに集約する(#21-M1レビュー指摘の追加ラウンド)。
    200単独ではなく206も宣言するのは、Range リクエストに Starlette FileResponse が
    ネイティブに 206 Partial Content で応答するため(§11.6のシーク再生対応)。
    """
    return {
        200: {"content": {media_type: {"schema": _AUDIO_BINARY_SCHEMA}}},
        206: {"content": {media_type: {"schema": _AUDIO_BINARY_SCHEMA}}},
    }


@router.get(
    "/audio/original",
    # OpenAPIコントラクトが実態(Range対応のバイナリ音声配信)を反映するよう明示する。
    # `response_class=FileResponse` を明示しないと、戻り値型注釈だけからは
    # FastAPIが既定の `application/json`(空スキーマ)応答も併記してしまい、生成
    # されたTS型に `"application/json": unknown` が `audio/*` と並存し続ける
    # (#21-M1レビュー指摘の追加ラウンド)。`response_class` を明示することで
    # FastAPIに「既定はJSONではない」と伝え、既定のJSON応答を出させない。
    # 実際は原曲の拡張子が可変(mp3/wav/flac/m4a)なため、コンテンツタイプは
    # `audio/*` として汎用的に宣言する。
    response_class=FileResponse,
    responses=_audio_responses("audio/*"),
)
async def get_original_audio(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> FileResponse:
    """§11.6: 原曲配信。HTTP Range 対応(Starlette FileResponse がネイティブに対応)。

    原曲は拡張子が可変(mp3/wav/flac/m4a)なため、DBに記録された `audio_format` を
    介して `service.audio_path_for_project()` で解決する(`storage.find_original_audio()`
    のようなファイルシステム側の拡張子探索には頼らない)。一方、分離後のステムは
    常に固定で `.wav`(#16の出力仕様)であり、DBに形式を持たせる意味が無いため、
    下の `get_stem_audio` は `storage` 経由で直接パスを組み立てる。同一ファイル内で
    解決方式が2通りあるのは、この拡張子が可変か固定かの違いに起因する意図的な差分。
    """
    project = ensure_project_exists(project_id, service)
    # `service.audio_path(project_id)` ではなく、既に取得済みの `project` を
    # `audio_path_for_project` に渡す(#21-M1レビュー指摘の追加ラウンド):
    # 前者は内部で `get_project` を再実行してしまい、`get_peaks` 用に導入した
    # 「取得済みレコードを再利用してDBラウンドトリップの重複を避ける」設計と
    # 同一ファイル内で不整合になっていた。副次的に、この2回目の `get_project`
    # と存在確認の間でプロジェクトが削除された場合の未捕捉
    # `ProjectNotFoundError`(500)も回避できる。
    path = service.audio_path_for_project(project)
    if not path.exists():
        raise HTTPException(status_code=404, detail="audio file not found")
    return FileResponse(path)


@router.get(
    "/audio/stems/{name}",
    # get_original_audio と同じ理由でresponse_classを明示する。ステムは常に .wav
    # 固定(#16)なので、原曲(可変拡張子)と異なり具体的なコンテンツタイプ
    # (audio/wav)を宣言できる(#21-M1レビュー指摘の追加ラウンド)。
    response_class=FileResponse,
    responses=_audio_responses("audio/wav"),
)
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
    ensure_project_exists(project_id, service)
    path = storage.stems_dir(settings.workspace_dir, project_id) / f"{name}.wav"
    if not path.exists():
        raise HTTPException(status_code=404, detail="stem not found")
    # media_typeを明示する(#21-M1レビュー指摘の追加ラウンド): 未指定だとFileResponseは
    # `mimetypes.guess_type()` に頼るが、Linux環境では `.wav` が `audio/x-wav` に
    # 解決されることが多く、OpenAPIで宣言した `audio/wav` と実際のレスポンスヘッダが
    # 食い違ってしまう(OS依存で不定にもなる)。ステムは常に .wav 固定(#16)なので、
    # 宣言と一致する固定値をここで明示できる。
    return FileResponse(path, media_type="audio/wav")


@router.get("/stems", response_model=StemListResponse)
async def list_stems(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    """#21 TrackList: 分離済みステム名の一覧を返す(分離ステージ未実行時は空リスト)。

    フロントはこれでステム名を知るまで `/audio/stems/{name}` の存在する `name` を
    知る手段が無い(#16のプリセットごとにステム名の集合が異なるため、固定リストを
    ハードコードできない)。空リストは404ではなく200で返す: 分離未実行は正常な
    初期状態であり、エラーではないため。
    """
    ensure_project_exists(project_id, service)
    names = storage.list_stem_names(settings.workspace_dir, project_id)
    return {"names": sorted(names)}


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
    project = ensure_project_exists(project_id, service)

    if name == "original":
        # `service.audio_path()` は内部で `get_project` を再度呼ぶため、既に
        # `ensure_project_exists` で取得済みのレコードを渡せる
        # `audio_path_for_project` を使う(#21-M1レビュー指摘の追加ラウンド):
        # 本エンドポイントはキャッシュヒット時もステイル判定のため毎回 audio_path
        # を解決するようになり、DBラウンドトリップの重複を避けたい。パス解決
        # ロジック自体はサービス層の1箇所にまとまったままなので、API層が
        # `storage.original_audio_path` の呼び出し方を再実装して二重管理に
        # なることもない。
        audio_path = service.audio_path_for_project(project)
    else:
        audio_path = storage.stems_dir(settings.workspace_dir, project_id) / f"{name}.wav"

    cache_path = storage.peaks_path(settings.workspace_dir, project_id, name)
    if cache_path.exists():
        # キャッシュを返す前に元音源がまだ存在するか確認する(#21-M1レビュー
        # 指摘の追加ラウンド): should_skip_stage同様、ステムがパイプライン外で
        # (手動)削除されるケースを想定していないと、/audio/stems/{name} は
        # 404を返すのにピークだけ200で返り続けてしまう。欠損していればキャッシュ
        # ごと破棄し、以降のリクエストにも一貫して404を返す。
        if not audio_path.exists():
            cache_path.unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail="audio file not found")
        return storage.read_json(cache_path)

    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="audio file not found")

    try:
        result = compute_peaks(str(audio_path))
    except sf.SoundFileError as exc:
        # soundfile(libsndfile)は、開けないフォーマット(m4a/AAC等)では
        # LibsndfileErrorを、ヘッダは有効だが読み取り中に壊れたファイル等では
        # サブクラスの SoundFileRuntimeError を送出しうる(#21-M1レビュー指摘の
        # 追加ラウンド)。両方とも共通の基底クラス `SoundFileError` で捕捉する。
        # 原曲はmp3/wav/flac/m4aを受け付けるため(get_original_audioのdocstring
        # 参照)、m4a原曲に対して本エンドポイントは未処理の500を返してしまって
        # いた。デコード不能をクライアントの問題として415で返す(キャッシュも
        # 作らない)。422ではなく415(Unsupported Media Type)にするのは、422は
        # FastAPIのリクエスト検証エラーが既定で使うステータスであり、リクエスト
        # 自体(URLパスの`name`パラメータ等)ではなく参照先の音源データそのものが
        # 処理できないことを表すには415の方がRESTの意味論として正確なため
        # (#21-M1レビュー指摘の追加ラウンド)。
        # libsndfileの例外メッセージには渡したファイルのフルパス(workspace_dir
        # 配下の実パス)が含まれるため、そのままdetailに含めるとサーバ内部の
        # ディレクトリ構成をクライアントに漏らしてしまう(#21-M1レビュー指摘の
        # 追加ラウンド)。detailは固定文言にし、詳細はサーバ側ログにのみ出す。
        logging.getLogger(__name__).warning(
            "peak computation failed to decode audio: project_id=%s name=%s: %s",
            project_id,
            name,
            exc,
        )
        if not audio_path.exists():
            # 上の存在チェックとこのcompute_peaks呼び出しの間に、ファイルが
            # (パイプライン外で)削除されたレース。単なるデコード不能(415)
            # ではなく欠損(404)として扱う(#21-M1レビュー指摘の追加ラウンド):
            # 415のままだと、本PRが確立した「欠損時は一貫して404」という
            # 契約と食い違う。
            raise HTTPException(status_code=404, detail="audio file not found") from exc
        raise HTTPException(
            status_code=415, detail="could not decode audio for peak computation"
        ) from exc
    storage.write_json(cache_path, result)
    return result


@router.get("/analysis/beatmap", response_model=Beatmap)
async def get_beatmap(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    """#18: `beatmap.json` をそのまま返す。"""
    ensure_project_exists(project_id, service)
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
    ensure_project_exists(project_id, service)
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
        # #29完了条件: ビートを補正すると量子化以降(quantize)が無効化され、
        # 再実行すると反映される。beatmap.jsonへ書き込む「前」に呼ぶこと
        # (#27-M2レビュー指摘、worker/dsp_main.pyの各ステージと同じフェイル
        # クローズの呼び出し契約): 逆順(書き込み後に呼ぶ)だと、Windowsの
        # 共有違反等でinvalidate_downstreamがリトライ上限超過で例外送出した際、
        # beatmap.jsonだけが手動補正済みに書き換わり、quantizeのmeta.jsonは
        # 無効化されないまま(stale=Falseのまま)残ってしまう。この順序なら
        # 無効化失敗時はbeatmap.json自体が不変のまま例外が伝播し、安全に
        # 再試行できる。
        stage_invalidation.invalidate_downstream(settings.workspace_dir, project_id, "beat")
        storage.write_json(path, beatmap)
    return beatmap
