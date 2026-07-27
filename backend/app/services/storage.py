"""Storage seam: Storage protocol + S3 implementation (MinIO local, R2 prod).

The rest of the app depends on the Storage protocol, never on aioboto3.
"""

from typing import BinaryIO, Protocol

import aioboto3

from app.core.config import settings


class Storage(Protocol):
    async def save(self, fileobj: BinaryIO, key: str) -> None: ...
    async def presigned_url(self, key: str, expires_in: int = 3600) -> str: ...
    async def delete(self, key: str) -> None: ...


class S3Storage:
    def __init__(self) -> None:
        self._session = aioboto3.Session()

    def _client(self):
        return self._session.client(
            "s3",
            endpoint_url=settings.storage_endpoint_url,
            aws_access_key_id=settings.storage_access_key,
            aws_secret_access_key=settings.storage_secret_key,
            region_name=settings.storage_region,
        )

    async def save(self, fileobj: BinaryIO, key: str) -> None:
        # upload_fileobj streams in chunks (multipart under the hood) —
        # the whole file is never held in memory.
        async with self._client() as s3:
            await s3.upload_fileobj(fileobj, settings.storage_bucket_name, key)

    async def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        async with self._client() as s3:
            return await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.storage_bucket_name, "Key": key},
                ExpiresIn=expires_in,
            )

    async def delete(self, key: str) -> None:
        async with self._client() as s3:
            await s3.delete_object(Bucket=settings.storage_bucket_name, Key=key)


storage: Storage = S3Storage()


# Verify against MinIO
# cd backend && uv run python -c "
# import asyncio, io
# from app.services.storage import storage

# async def main():
#     await storage.save(io.BytesIO(b'hello storage'), 'smoke-test.txt')
#     url = await storage.presigned_url('smoke-test.txt')
#     print('presigned:', url[:80], '...')
#     import httpx
#     r = httpx.get(url)
#     assert r.content == b'hello storage', r.content
#     await storage.delete('smoke-test.txt')
#     print('save/presign/fetch/delete OK')

# asyncio.run(main())
# "