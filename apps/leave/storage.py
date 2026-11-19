"""Private storage for leave request attachments.

Global ``DEFAULT`` storage stays on the filesystem. Only this backend is used
for leave documents.

When ``AWS_STORAGE_BUCKET_NAME`` is set, files go to S3 under
``leave-attachments/`` with no public ACL and signed URLs. When it is unset
(local and tests), files go to ``media/leave-attachments/`` and the link
endpoint must stream them instead of presigning.

The attachment model does not live here. A later file field should use
``storage=LeaveAttachmentStorage()``. The link action should then:

* call :func:`presign_leave_attachment_url` when ``storage.uses_s3`` is true
  and return ``{"url": url, "expires_in": PRESIGN_EXPIRES_SECONDS}``
* return :func:`stream_leave_attachment` directly when ``storage.uses_s3``
  is false

Pass ``attachment.file.name`` (the storage-relative name) and the original
filename. Do not call ``storage.url()`` for local files.
"""

import os

from decouple import config
from django.core.files.storage import FileSystemStorage, Storage
from django.http import FileResponse
from django.utils.deconstruct import deconstructible
from django.utils.http import content_disposition_header

LEAVE_ATTACHMENT_LOCATION = "leave-attachments"
PRESIGN_EXPIRES_SECONDS = 300
DISPOSITION_INLINE = "inline"
DISPOSITION_ATTACHMENT = "attachment"
_DISPOSITIONS = (DISPOSITION_INLINE, DISPOSITION_ATTACHMENT)


def _env(name):
    return config(name, default="").strip()


def leave_attachment_bucket_name():
    """Bucket name from the environment. Empty means local filesystem."""
    return _env("AWS_STORAGE_BUCKET_NAME")


def _download_filename(name, filename):
    raw = (filename or "").replace("\r", "").replace("\n", "").strip()
    if not raw:
        raw = os.path.basename(str(name).replace("\\", "/"))
    cleaned = os.path.basename(raw.replace("\\", "/"))
    return cleaned or "attachment"


def _content_disposition(disposition, filename):
    if disposition not in _DISPOSITIONS:
        raise ValueError("disposition must be 'inline' or 'attachment'.")
    header = content_disposition_header(
        disposition == DISPOSITION_ATTACHMENT,
        filename,
    )
    if header:
        return header
    return disposition


def _is_s3_backend(backend):
    return backend.__class__.__module__.startswith("storages.backends.s3")


def _local_root():
    from django.conf import settings

    return str(settings.BASE_DIR / "media" / LEAVE_ATTACHMENT_LOCATION)


class _LocalLeaveAttachmentStorage(FileSystemStorage):
    """Filesystem fallback. ``url()`` is disabled so files stay private."""

    def __init__(self):
        super().__init__(location=_local_root(), base_url=None)

    def url(self, name):
        raise ValueError(
            "Local leave attachments are not available by URL. "
            "Use stream_leave_attachment."
        )


def _build_s3_storage():
    # Imported only when a bucket is configured so local/tests need no boto3.
    from storages.backends.s3 import S3Storage

    options = {
        "bucket_name": leave_attachment_bucket_name(),
        "querystring_auth": True,
        "querystring_expire": PRESIGN_EXPIRES_SECONDS,
        "default_acl": None,
        "file_overwrite": False,
        "location": LEAVE_ATTACHMENT_LOCATION,
        "signature_version": "s3v4",
    }
    region = _env("AWS_S3_REGION_NAME")
    access_key = _env("AWS_ACCESS_KEY_ID")
    secret_key = _env("AWS_SECRET_ACCESS_KEY")
    if region:
        options["region_name"] = region
    if access_key:
        options["access_key"] = access_key
    if secret_key:
        options["secret_key"] = secret_key
    return S3Storage(**options)


def _build_backend():
    if leave_attachment_bucket_name():
        return _build_s3_storage()
    return _LocalLeaveAttachmentStorage()


@deconstructible
class LeaveAttachmentStorage(Storage):
    """S3 when ``AWS_STORAGE_BUCKET_NAME`` is set, otherwise the local disk.

    The backend is chosen on first use from the current environment, so a
    future ``FileField(storage=LeaveAttachmentStorage())`` migration does not
    embed bucket names.
    """

    def __init__(self):
        self._backend = None

    @property
    def backend(self):
        if self._backend is None:
            self._backend = _build_backend()
        return self._backend

    @property
    def uses_s3(self):
        return _is_s3_backend(self.backend)

    def _open(self, name, mode="rb"):
        return self.backend._open(name, mode)

    def _save(self, name, content):
        return self.backend._save(name, content)

    def save(self, name, content, max_length=None):
        return self.backend.save(name, content, max_length=max_length)

    def open(self, name, mode="rb"):
        return self.backend.open(name, mode)

    def delete(self, name):
        return self.backend.delete(name)

    def exists(self, name):
        return self.backend.exists(name)

    def listdir(self, path):
        return self.backend.listdir(path)

    def size(self, name):
        return self.backend.size(name)

    def url(self, name, *args, **kwargs):
        return self.backend.url(name, *args, **kwargs)

    def path(self, name):
        return self.backend.path(name)

    def get_accessed_time(self, name):
        return self.backend.get_accessed_time(name)

    def get_created_time(self, name):
        return self.backend.get_created_time(name)

    def get_modified_time(self, name):
        return self.backend.get_modified_time(name)

    def get_available_name(self, name, max_length=None):
        return self.backend.get_available_name(name, max_length=max_length)

    def generate_filename(self, filename):
        return self.backend.generate_filename(filename)


def _resolve_storage(storage):
    if storage is None:
        storage = LeaveAttachmentStorage()
    if isinstance(storage, LeaveAttachmentStorage):
        return storage
    raise TypeError("storage must be a LeaveAttachmentStorage instance.")


def presign_leave_attachment_url(
    name,
    *,
    disposition,
    content_type,
    filename="",
    expires_in=PRESIGN_EXPIRES_SECONDS,
    storage=None,
):
    """Return a 300-second S3 GET URL for ``name``.

    ``name`` is the storage-relative file name (``FileField.name``), not the
    full S3 key. ``disposition`` is ``inline`` or ``attachment``. The signed
    URL sets ``ResponseContentDisposition`` and ``ResponseContentType``.

    Raises ``RuntimeError`` when the bucket env is unset; stream the file
    instead.
    """
    content_type = (content_type or "").replace("\r", "").replace("\n", "").strip()
    if not content_type:
        raise ValueError("content_type is required.")
    if expires_in <= 0:
        raise ValueError("expires_in must be positive.")

    resolved = _resolve_storage(storage)
    if not resolved.uses_s3:
        raise RuntimeError(
            "presign_leave_attachment_url requires AWS_STORAGE_BUCKET_NAME. "
            "Use stream_leave_attachment for local storage."
        )

    download_name = _download_filename(name, filename)
    return resolved.backend.url(
        name,
        parameters={
            "ResponseContentDisposition": _content_disposition(
                disposition, download_name
            ),
            "ResponseContentType": content_type,
        },
        expire=expires_in,
        http_method="GET",
    )


def stream_leave_attachment(
    name,
    *,
    disposition,
    content_type,
    filename="",
    storage=None,
):
    """Stream a local file with the same Content-Disposition as a presigned URL.

    Returns a :class:`~django.http.FileResponse`. Raises ``RuntimeError`` when
    the file is on S3; presign that object instead.
    """
    content_type = (content_type or "").replace("\r", "").replace("\n", "").strip()
    if not content_type:
        raise ValueError("content_type is required.")
    if disposition not in _DISPOSITIONS:
        raise ValueError("disposition must be 'inline' or 'attachment'.")

    resolved = _resolve_storage(storage)
    if resolved.uses_s3:
        raise RuntimeError(
            "stream_leave_attachment is for local storage. "
            "Use presign_leave_attachment_url when AWS_STORAGE_BUCKET_NAME is set."
        )

    download_name = _download_filename(name, filename)
    opened = resolved.open(name, "rb")
    return FileResponse(
        opened,
        content_type=content_type,
        as_attachment=disposition == DISPOSITION_ATTACHMENT,
        filename=download_name,
    )
