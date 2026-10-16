from pathlib import Path

from app.audio.downloader import download_audio


class AudioService:
    async def download(self, url: str, target_directory: Path) -> Path:
        return await download_audio(url, target_directory)
