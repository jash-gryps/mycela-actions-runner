"""
shared/gdrive.py — Google Drive upload helper for run artifacts.

Used by notebooks/report.py and notebooks/finalize.py to upload
check reports, logs, and run summaries to Google Drive.

All pipelines share this module — never duplicate this logic.

Usage:
    from shared.gdrive import GDriveUploader

    uploader = GDriveUploader()
    uploader.upload_file(
        local_path="artifacts/check_report_stage1.json",
        folder_id=os.environ["GDRIVE_FOLDER_ID"],
        filename="check_report_stage1.json"
    )
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REQUIRED_ENV = ["GDRIVE_CREDENTIALS", "GDRIVE_FOLDER_ID"]
SCOPES = ["https://www.googleapis.com/auth/drive"]


def run_folder_segments(alias: str, run_number: str | int,
                        when: Optional[datetime] = None) -> list[str]:
    """
    Build the structured archive path for a run:
        [alias, "YYYY-MM", "run-{number}-YYYY-MM-DD"]
    e.g. ["harbor", "2026-06", "run-42-2026-06-12"]
    """
    when = when or datetime.now(timezone.utc)
    return [
        alias,
        when.strftime("%Y-%m"),
        f"run-{run_number}-{when.strftime('%Y-%m-%d')}",
    ]


class GDriveUploader:
    """
    Uploads files to Google Drive using a service account.
    Errors are logged but never raised — upload failure must not
    abort the pipeline or suppress the original error.
    """

    def __init__(self):
        self._service = None
        self._init_service()

    def _init_service(self):
        missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
        if missing:
            logger.warning(f"[gdrive] Missing env vars: {missing}. Uploads disabled.")
            return
        try:
            from googleapiclient.discovery import build

            creds_json = json.loads(os.environ["GDRIVE_CREDENTIALS"])
            # Auto-detect credential type. A service account has NO storage quota in a
            # regular My Drive folder (uploads fail with storageQuotaExceeded), so the
            # archive must upload as a USER via OAuth — detected by a refresh_token.
            if "refresh_token" in creds_json:
                from google.oauth2.credentials import Credentials
                credentials = Credentials(
                    token=None,
                    refresh_token=creds_json["refresh_token"],
                    client_id=creds_json["client_id"],
                    client_secret=creds_json["client_secret"],
                    token_uri=creds_json.get("token_uri",
                                             "https://oauth2.googleapis.com/token"),
                    scopes=SCOPES,
                )
                logger.info("[gdrive] Authenticated via OAuth user credentials")
            else:
                from google.oauth2 import service_account
                credentials = service_account.Credentials.from_service_account_info(
                    creds_json, scopes=SCOPES
                )
                logger.info("[gdrive] Authenticated via service account")
            self._service = build("drive", "v3", credentials=credentials,
                                  cache_discovery=False)
        except Exception as e:
            logger.error(f"[gdrive] Failed to initialise service: {e}")

    def upload_file(self, local_path: str, folder_id: str,
                    filename: Optional[str] = None) -> Optional[str]:
        """
        Upload a file to Google Drive. Returns the file ID or None on failure.
        Never raises — logs errors instead.
        """
        if not self._service:
            logger.warning("[gdrive] Service not available — skipping upload")
            return None

        path = Path(local_path)
        if not path.exists():
            logger.warning(f"[gdrive] File not found: {local_path}")
            return None

        name = filename or path.name
        try:
            from googleapiclient.http import MediaFileUpload
            media = MediaFileUpload(str(path), resumable=False)
            metadata = {"name": name, "parents": [folder_id]}
            result = self._service.files().create(
                body=metadata, media_body=media, fields="id"
            ).execute()
            file_id = result.get("id")
            logger.info(f"[gdrive] Uploaded {name} → {file_id}")
            return file_id
        except Exception as e:
            logger.error(f"[gdrive] Upload failed for {name}: {e}")
            return None

    def create_folder(self, name: str, parent_id: str) -> Optional[str]:
        """Create a subfolder in Google Drive. Returns folder ID or None."""
        if not self._service:
            return None
        try:
            metadata = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id]
            }
            result = self._service.files().create(body=metadata, fields="id").execute()
            folder_id = result.get("id")
            logger.info(f"[gdrive] Created folder '{name}' → {folder_id}")
            return folder_id
        except Exception as e:
            logger.error(f"[gdrive] Failed to create folder '{name}': {e}")
            return None

    def get_or_create_folder(self, name: str, parent_id: str) -> Optional[str]:
        """
        Return the ID of the folder `name` under `parent_id`, creating it only
        if it does not already exist. Avoids duplicate folders on retry.
        Returns None on failure — never raises.
        """
        if not self._service:
            logger.warning("[gdrive] Service not available — cannot get/create folder")
            return None
        try:
            # Escape single quotes in the name for the Drive query language
            safe_name = name.replace("'", "\\'")
            results = self._service.files().list(
                q=(f"'{parent_id}' in parents and name = '{safe_name}' "
                   f"and mimeType = 'application/vnd.google-apps.folder' "
                   f"and trashed=false"),
                fields="files(id)",
                pageSize=1
            ).execute()
            existing = results.get("files", [])
            if existing:
                folder_id = existing[0]["id"]
                logger.info(f"[gdrive] Reusing folder '{name}' → {folder_id}")
                return folder_id
        except Exception as e:
            logger.error(f"[gdrive] Folder lookup failed for '{name}': {e}")
            return None

        return self.create_folder(name, parent_id)

    def ensure_folder_path(self, segments: list[str],
                           root_id: Optional[str] = None) -> Optional[str]:
        """
        Walk a list of folder names (e.g. ["harbor", "2026-06", "run-42-2026-06-12"]),
        creating each level only if missing. Returns the deepest folder ID,
        or None if any level failed.
        """
        parent = root_id or os.environ.get("GDRIVE_FOLDER_ID", "")
        if not parent:
            logger.warning("[gdrive] GDRIVE_FOLDER_ID not set — cannot build folder path")
            return None
        for name in segments:
            parent = self.get_or_create_folder(name, parent)
            if not parent:
                return None
        return parent

    def upload_run_artifacts(self, run_folder_name: str,
                              artifact_paths: list[str]) -> Optional[str]:
        """
        Create a subfolder for this run and upload all artifacts into it.
        Returns the run folder ID or None if creation failed.
        """
        root_folder_id = os.environ.get("GDRIVE_FOLDER_ID", "")
        if not root_folder_id:
            logger.warning("[gdrive] GDRIVE_FOLDER_ID not set — skipping artifact upload")
            return None

        folder_id = self.get_or_create_folder(run_folder_name, root_folder_id)
        if not folder_id:
            return None

        for path in artifact_paths:
            self.upload_file(path, folder_id)

        return folder_id

    def find_file(self, name: str, folder_id: str) -> Optional[str]:
        """
        Return the ID of the file named `name` directly under `folder_id`, or None.
        Used to read a run's per-stage reports back from the private archive.
        Never raises — logs errors and returns None.
        """
        if not self._service:
            logger.warning("[gdrive] Service not available — cannot find file")
            return None
        try:
            safe_name = name.replace("'", "\\'")
            results = self._service.files().list(
                q=(f"'{folder_id}' in parents and name = '{safe_name}' "
                   f"and trashed=false"),
                fields="files(id)",
                pageSize=1
            ).execute()
            files = results.get("files", [])
            return files[0]["id"] if files else None
        except Exception as e:
            logger.error(f"[gdrive] File lookup failed for '{name}': {e}")
            return None

    def read_json_from_folder(self, folder_id: str, name: str) -> Optional[dict]:
        """
        Find `name` under `folder_id`, download it, and parse it as JSON.
        Returns the parsed dict, or None if the file is missing or unreadable.
        Never raises — finalize must not abort because a report could not be read.
        """
        if not self._service:
            logger.warning("[gdrive] Service not available — cannot read file")
            return None
        file_id = self.find_file(name, folder_id)
        if not file_id:
            return None
        try:
            content = self._service.files().get_media(fileId=file_id).execute()
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            return json.loads(content)
        except Exception as e:
            logger.error(f"[gdrive] Failed to read JSON '{name}': {e}")
            return None

    def list_folder(self, folder_id: str) -> list[str]:
        """
        Return the names of all files in a Google Drive folder.
        Returns an empty list on failure — never raises.
        """
        if not self._service:
            logger.warning("[gdrive] Service not available — cannot list folder")
            return []
        try:
            results = self._service.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="files(name)",
                pageSize=1000
            ).execute()
            return [f["name"] for f in results.get("files", [])]
        except Exception as e:
            logger.error(f"[gdrive] Failed to list folder {folder_id}: {e}")
            return []

    def list_subfolders(self, parent_id: str) -> list[dict]:
        """
        Return [{"id", "name"}] for each sub*folder* of parent_id (files excluded).
        Used by retention pruning to walk alias / month folders.
        Returns an empty list on failure — never raises.
        """
        if not self._service:
            logger.warning("[gdrive] Service not available — cannot list subfolders")
            return []
        try:
            results = self._service.files().list(
                q=(f"'{parent_id}' in parents "
                   f"and mimeType = 'application/vnd.google-apps.folder' "
                   f"and trashed=false"),
                fields="files(id,name)",
                pageSize=1000
            ).execute()
            return [{"id": f["id"], "name": f["name"]} for f in results.get("files", [])]
        except Exception as e:
            logger.error(f"[gdrive] Failed to list subfolders of {parent_id}: {e}")
            return []

    def trash_folder(self, folder_id: str) -> bool:
        """
        Move a folder (and its contents) to Drive trash. Recoverable for ~30 days —
        deliberately not a permanent delete, so a mistaken prune is reversible
        (Golden Rule 6). Returns True on success, False on failure — never raises.
        """
        if not self._service:
            logger.warning("[gdrive] Service not available — cannot trash folder")
            return False
        try:
            self._service.files().update(
                fileId=folder_id, body={"trashed": True}
            ).execute()
            logger.info(f"[gdrive] Trashed folder {folder_id}")
            return True
        except Exception as e:
            logger.error(f"[gdrive] Failed to trash folder {folder_id}: {e}")
            return False
