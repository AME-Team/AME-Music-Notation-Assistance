"""#21: 波形ピークデータ計算のテスト。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf
from app.infra import storage
from app.pipeline.peaks import compute_peaks


def _write_wav(path: Path, *, seconds: float, sr: int = 44100) -> None:
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    audio = 0.5 * np.sin(2 * np.pi * 440 * t)
    sf.write(path, audio.astype(np.float32), sr)


def test_compute_peaks_basic_shape(tmp_path: Path) -> None:
    path = tmp_path / "tone.wav"
    _write_wav(path, seconds=2.0)

    result = compute_peaks(str(path), buckets=100)

    assert result["sample_rate"] == 44100
    assert result["duration_sec"] == 2.0
    assert len(result["peaks"]) == 100
    for lo, hi in result["peaks"]:
        assert lo <= hi
        assert -1.0 <= lo <= 1.0
        assert -1.0 <= hi <= 1.0


def test_compute_peaks_bucket_count_capped_by_sample_count(tmp_path: Path) -> None:
    path = tmp_path / "short.wav"
    sf.write(path, np.zeros(5, dtype=np.float32), 44100)

    result = compute_peaks(str(path), buckets=1000)

    assert len(result["peaks"]) <= 5


def test_compute_peaks_empty_audio(tmp_path: Path) -> None:
    path = tmp_path / "empty.wav"
    sf.write(path, np.zeros(0, dtype=np.float32), 44100)

    result = compute_peaks(str(path))

    assert result["peaks"] == []
    assert result["duration_sec"] == 0.0


def test_invalidate_peaks_cache_removes_only_named_stems(tmp_path: Path) -> None:
    """再分離後に古いキャッシュが残ると、get_peaks() が古い波形を返し続けてしまう。"""
    project_id = "proj_test"
    vocals_path = storage.peaks_path(tmp_path, project_id, "vocals")
    drums_path = storage.peaks_path(tmp_path, project_id, "drums")
    storage.write_json(vocals_path, {"peaks": []})
    storage.write_json(drums_path, {"peaks": []})

    storage.invalidate_peaks_cache(tmp_path, project_id, ["vocals"])

    assert not vocals_path.exists()
    assert drums_path.exists()


def test_invalidate_peaks_cache_missing_file_is_a_noop(tmp_path: Path) -> None:
    storage.invalidate_peaks_cache(tmp_path, "proj_test", ["nonexistent"])


def test_peaks_path_separates_resolutions(tmp_path: Path) -> None:
    """#169: 解像度ごとに別ファイルへキャッシュする(既定解像度のパスは従来のまま)。

    派生キャッシュは`analysis/peaks/{name}/{buckets}.json`へ置く(サブディレクトリ)。
    """
    default = storage.peaks_path(tmp_path, "proj_test", "original")
    hires = storage.peaks_path(tmp_path, "proj_test", "original", buckets=8000)

    assert default.name == "original.json"
    assert hires.parent.name == "original"
    assert hires.name == "8000.json"
    assert default != hires


def test_peaks_path_does_not_collide_with_dotted_stem_names(tmp_path: Path) -> None:
    """ドットを含むステム名の既定キャッシュと、派生キャッシュが同一パスにならないこと。

    同一名前空間に`{name}.{buckets}.json`を並べると、ステム`original.8000`の既定
    キャッシュと、ステム`original`のbuckets=8000の派生キャッシュが衝突し、読み書きで
    取り違えて誤った波形を表示しうる。
    """
    dotted_default = storage.peaks_path(tmp_path, "proj_test", "original.8000")
    derived = storage.peaks_path(tmp_path, "proj_test", "original", buckets=8000)

    assert dotted_default != derived
    assert dotted_default.parent == derived.parent.parent


def test_invalidate_peaks_cache_removes_derived_resolutions(tmp_path: Path) -> None:
    """#169: 再分離時は解像度別の派生キャッシュ(`{name}.{buckets}.json`)も消す。

    既定解像度だけを消すと、拡大表示用の高解像度キャッシュが古いステムのまま
    残り、拡大したときだけ古い波形が表示され続けてしまう。
    """
    project_id = "proj_test"
    storage.write_json(
        storage.peaks_path(tmp_path, project_id, "vocals"), {"peaks": []}
    )
    derived = storage.peaks_path(tmp_path, project_id, "vocals", buckets=8000)
    storage.write_json(derived, {"peaks": []})
    other = storage.peaks_path(tmp_path, project_id, "drums", buckets=8000)
    storage.write_json(other, {"peaks": []})

    storage.invalidate_peaks_cache(tmp_path, project_id, ["vocals"])

    assert not derived.exists()
    assert other.exists()


def test_invalidate_peaks_cache_keeps_other_stems_with_dotted_names(
    tmp_path: Path,
) -> None:
    """別ステム名が数字接尾辞に見えても巻き込まない(`vocals` と `vocals.2` の区別)。"""
    project_id = "proj_test"
    dotted = storage.peaks_path(tmp_path, project_id, "vocals.2")
    storage.write_json(dotted, {"peaks": []})
    storage.write_json(
        storage.peaks_path(tmp_path, project_id, "vocals", buckets=8000), {"peaks": []}
    )

    storage.invalidate_peaks_cache(tmp_path, project_id, ["vocals"])

    assert dotted.exists()
