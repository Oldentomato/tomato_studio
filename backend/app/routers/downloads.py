from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..services import volume_files

router = APIRouter(prefix="/api/downloads", tags=["downloads"])


@router.get("/{token}")
def download(token: str):
    try:
        filename, path = volume_files.read_download(token)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="다운로드 파일이 만료되었거나 없습니다.")
    return FileResponse(path, filename=filename)
