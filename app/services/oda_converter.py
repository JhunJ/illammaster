"""
DWG -> DXF 변환.
1) ODA File Converter (ODA_FC_PATH) 사용 - CLI는 "입력폴더 출력폴더 버전 타입 recurse audit 필터"
2) 없으면 ezdxf odafc (기본 경로 C:\\Program Files\\ODA\\ODAFileConverter\\ODAFileConverter.exe)
3) 없으면 LibreDWG dwg2dxf (PATH)

참고: DWG→DXF 변환 시 ODA는 레이어 테이블(LAYER) color(62)를 그대로 출력합니다.
일부 DWG/ODA 조합에서는 레이어 색상이 기본값(7)으로 나올 수 있습니다.
dxf_parser는 raw DXF 태그 fallback으로 이 경우를 보완합니다.
"""
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

# ODA 인자: "Input Folder" "Output Folder" version type recurse audit [filter]
ODA_VERSION_DEFAULT = "ACAD2018"


def get_oda_path() -> str | None:
    """환경변수/설정에 지정된 ODA 실행 파일 경로. 없으면 None."""
    raw = os.environ.get("ODA_FC_PATH", "").strip() or (get_settings().oda_fc_path or "").strip()
    if not raw:
        return None
    path = raw.strip('"').strip("'")
    return path if Path(path).exists() else None


def _find_oda_win() -> str | None:
    """Windows에서 ODA File Converter 실행 파일 찾기. 버전 폴더(ODAFileConverter 26.12.0 등) 지원."""
    oda_base = Path(r"C:\Program Files\ODA")
    if not oda_base.exists():
        return None
    # 1) 고정 경로 (버전 없음)
    classic = oda_base / "ODAFileConverter" / "ODAFileConverter.exe"
    if classic.exists():
        return str(classic)
    # 2) ODAFileConverter* 폴더 안의 ODAFileConverter.exe (버전 포함 폴더)
    for item in oda_base.iterdir():
        if not item.is_dir() or not item.name.startswith("ODAFileConverter"):
            continue
        exe = item / "ODAFileConverter.exe"
        if exe.exists():
            logger.info("Using ODA at: %s", exe)
            return str(exe)
    return None


def _run_oda(oda: str, in_folder: str, out_dir: str, filter_arg: str, expected_dxf_name: str | None = None) -> Path | None:
    """ODA 실행 후 출력 DXF 경로 반환. filter_arg 예: '*.dwg' 또는 'filename.dwg'.
    expected_dxf_name: *.dwg 사용 시 예상 출력 파일명(예: 'file.dxf').
    """
    version = (get_settings().oda_dxf_version or os.environ.get("ODA_DXF_VERSION") or ODA_VERSION_DEFAULT).strip()
    cmd = [oda, in_folder, out_dir, version, "DXF", "0", "0", filter_arg]
    logger.info("Running ODA: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=in_folder)
    if result.returncode != 0:
        logger.warning("ODA exit code %s stdout=%s stderr=%s", result.returncode, result.stdout, result.stderr)
    dxf_name = expected_dxf_name or (Path(filter_arg).stem + ".dxf" if "*" not in filter_arg else None)
    if dxf_name:
        version = (get_settings().oda_dxf_version or os.environ.get("ODA_DXF_VERSION") or ODA_VERSION_DEFAULT).strip()
        candidates = [
            Path(out_dir) / dxf_name,
            Path(out_dir) / version / dxf_name,
        ]
        for p in candidates:
            if p.exists():
                logger.info("ODA output: %s size=%s bytes", p, p.stat().st_size)
                return p
    found = list(Path(out_dir).rglob("*.dxf"))
    if found:
        p = found[0]
        logger.info("ODA output (rglob): %s size=%s bytes", p, p.stat().st_size)
        return p
    return None


def _try_oda(dwg_path: Path) -> Path | None:
    """ODA File Converter: 입력폴더, 출력폴더, 버전, DXF, 0, 0, 필터(파일명 또는 *.dwg)."""
    oda = get_oda_path()
    if not oda:
        oda = _find_oda_win()
    if not oda:
        return None
    out_dir = tempfile.mkdtemp(prefix="oda_")
    try:
        in_folder = str(dwg_path.parent)
        dxf_path = _run_oda(oda, in_folder, out_dir, dwg_path.name)
        # 결과가 비어있을 수 있음(작은 DXF) → 임시 폴더에 복사 후 *.dwg 로 재시도
        if dxf_path is not None and dxf_path.stat().st_size < 5000:
            try:
                work_dir = tempfile.mkdtemp(prefix="oda_in_")
                copy_path = Path(work_dir) / dwg_path.name
                shutil.copy2(dwg_path, copy_path)
                out_dir2 = tempfile.mkdtemp(prefix="oda_out_")
                dxf_path2 = _run_oda(oda, work_dir, out_dir2, "*.dwg", expected_dxf_name=dwg_path.stem + ".dxf")
                if dxf_path2 and dxf_path2.stat().st_size > dxf_path.stat().st_size:
                    logger.info("ODA retry with *.dwg produced larger file, using it")
                    return dxf_path2
            except Exception as e:
                logger.debug("ODA retry failed: %s", e)
        return dxf_path
    except Exception as e:
        logger.warning("ODA conversion error: %s", e)
        return None


def _try_ezdxf_odafc(dwg_path: Path) -> Path | None:
    """ezdxf addon odafc 사용 (ODA 경로는 ODA_FC_PATH env 또는 ezdxf 기본값)."""
    try:
        from ezdxf.addons import odafc
    except ImportError:
        return None
    oda_env = get_oda_path()
    if oda_env:
        os.environ["ODA_FC_PATH"] = oda_env
    if not odafc.is_installed():
        return None
    out_dir = tempfile.mkdtemp(prefix="odafc_")
    dxf_path = Path(out_dir) / (dwg_path.stem + ".dxf")
    try:
        odafc.convert(str(dwg_path), str(dxf_path), version="R2018", audit=False, replace=True)
        return dxf_path if dxf_path.exists() else None
    except Exception as e:
        logger.debug("ezdxf odafc convert error: %s", e)
        return None


def _try_dwg2dxf(dwg_path: Path) -> Path | None:
    """LibreDWG dwg2dxf (PATH에 있으면 사용)."""
    dwg2dxf = shutil.which("dwg2dxf")
    if not dwg2dxf:
        return None
    work_dir = tempfile.mkdtemp(prefix="dwg2dxf_")
    try:
        # 같은 디렉에 두고 실행하면 stem.dxf 생성되는 경우가 많음
        dest = Path(work_dir) / dwg_path.name
        shutil.copy2(dwg_path, dest)
        result = subprocess.run(
            [dwg2dxf, str(dest)],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.debug("dwg2dxf failed: %s %s", result.stdout, result.stderr)
            return None
        dxf_path = Path(work_dir) / (dest.stem + ".dxf")
        if dxf_path.exists():
            return dxf_path
        found = list(Path(work_dir).rglob("*.dxf"))
        return found[0] if found else None
    except Exception as e:
        logger.debug("dwg2dxf error: %s", e)
        return None


def dwg_to_dxf(dwg_path: str | Path) -> Path | None:
    """
    DWG 파일을 DXF로 변환.
    ODA File Converter 우선, 없으면 ezdxf odafc, LibreDWG dwg2dxf 시도.
    """
    path, _ = dwg_to_dxf_with_info(dwg_path)
    return path


def dwg_to_dxf_with_info(dwg_path: str | Path) -> tuple[Path | None, str | None]:
    """
    DWG를 DXF로 변환하고, 사용된 변환기 이름을 함께 반환.
    반환: (dxf_path, converter_name)
    converter_name: "ODA" | "ezdxf_odafc" | "dwg2dxf" | None
    """
    dwg_path = Path(dwg_path)
    if not dwg_path.exists():
        logger.error("DWG file not found: %s", dwg_path)
        return None, None

    dxf = _try_oda(dwg_path)
    if dxf is not None:
        logger.info("DWG converted with ODA File Converter")
        return dxf, "ODA"
    dxf = _try_ezdxf_odafc(dwg_path)
    if dxf is not None:
        logger.info("DWG converted with ezdxf odafc")
        return dxf, "ezdxf_odafc"
    dxf = _try_dwg2dxf(dwg_path)
    if dxf is not None:
        logger.info("DWG converted with dwg2dxf (LibreDWG)")
        return dxf, "dwg2dxf"
    logger.warning("No DWG converter available (ODA_FC_PATH or dwg2dxf in PATH)")
    return None, None
