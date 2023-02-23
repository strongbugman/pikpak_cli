import os
import typing

import aiofiles
from ant_nest import ant, pipelines
from tqdm import tqdm

import settings


class ErrorPipeline(pipelines.Pipeline):
    async def process(self, obj: pipelines.Response) -> pipelines.Response:
        if obj.status_code >= 400:
            obj.raise_for_status()

        return obj


class AuthPipeline(pipelines.Pipeline):
    def __init__(self):
        super().__init__()
        self.token = {}

    async def process(self, obj: pipelines.Request) -> pipelines.Request:
        if "auth" not in obj.url.path:
            obj.headers.update(
                {
                    "Authorization": f"{self.token.get('token_type')} {self.token.get('access_token')}"
                }
            )

        return obj


class Pikpak(ant.Ant):
    auth_pipeline = AuthPipeline()
    request_pipelines = [auth_pipeline]
    response_pipelines = [ErrorPipeline()]

    async def login(self, account: str = "", password: str = ""):
        res = await self.request(
            "https://user.mypikpak.com/v1/auth/signin",
            method="post",
            json={
                "client_id": settings.PIKPAK_CLIENT_ID,
                "client_secret": settings.PIKPAK_CLIENT_SECRET,
                "username": account,
                "password": password,
                "captcha_token": "",
            },
        )
        data = res.json()
        self.auth_pipeline.token = data
        return data

    async def list_files(self, parent_id: str = "") -> typing.Dict:
        params = {
            "thumbnail_size": "SIZE_LARGE",
            "limit": 0,
        }
        if parent_id:
            params["parent_id"] = parent_id
        res = await self.request(
            "https://api-drive.mypikpak.com/drive/v1/files",
            params=params,
        )
        return res.json()

    async def get_file_link(self, file_id: str) -> typing.Dict:
        return (
            await self.request(
                f"https://api-drive.mypikpak.com/drive/v1/files/{file_id}",
            )
        ).json()

    async def delete_file(
        self, file_ids: typing.List[str], trash: bool = True
    ) -> typing.Dict:
        url = "https://api-drive.mypikpak.com/drive/v1/files:batchDelete"
        if trash:
            url = "https://api-drive.mypikpak.com/drive/v1/files:batchTrash"

        return (await self.request(url, method="post", json={"ids": file_ids})).json()

    async def download(
        self, url: str, path: str, start_at: int = 0, cache_size=300 * 1024 * 1024
    ):
        if os.path.exists(path):
            return

        path += ".part"
        if os.path.exists(path):
            start_at = os.stat(path).st_size

        headers = {}
        if start_at:
            headers = {"Range": f"bytes={start_at}-"}
        res = await self.request(url, stream=True, headers=headers)
        downloaded = 0
        with tqdm(
            initial=start_at,
            total=int(res.headers["Content-Length"]),
            unit_scale=True,
            unit_divisor=1024,
            unit="B",
            ascii=" \/>",
        ) as progress:
            async with aiofiles.open(path, "ab" if start_at else "wb") as f:
                cache_bs = b""
                async for bs in res.aiter_bytes(30 * 1024 * 1024):
                    cache_bs += bs
                    if len(cache_bs) >= cache_size:
                        await f.write(cache_bs)
                        cache_bs = b""
                    progress.update(res.num_bytes_downloaded - downloaded)
                    downloaded = res.num_bytes_downloaded
                if cache_bs:
                    await f.write(cache_bs)
        os.rename(path, path[:-5])

    async def run(self):
        await self.login()
