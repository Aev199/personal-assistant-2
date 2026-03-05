"""Cloud storage adapter interface and implementations for S3, Dropbox, and GCS.

This module provides a Protocol interface for cloud storage backends and concrete
implementations for AWS S3, Dropbox, and Google Cloud Storage. All implementations
support streaming uploads to avoid large memory buffers and enforce HTTPS for transfers.
"""

import asyncio
import os
from pathlib import Path
from typing import Protocol, Optional
import logging

logger = logging.getLogger(__name__)


class StorageAdapter(Protocol):
    """Protocol for cloud storage backends (S3, Dropbox, Google Cloud Storage).
    
    All implementations must support async operations and use HTTPS for transfers.
    """
    
    async def upload(self, local_path: str, remote_path: str) -> str:
        """Upload file to cloud storage using streaming to avoid memory issues.
        
        Args:
            local_path: Path to local file to upload
            remote_path: Destination path in cloud storage
            
        Returns:
            Remote URL or identifier of uploaded file
            
        Raises:
            Exception: If upload fails
        """
        ...
    
    async def download(self, remote_path: str, local_path: str) -> None:
        """Download file from cloud storage.
        
        Args:
            remote_path: Path in cloud storage
            local_path: Destination path for downloaded file
            
        Raises:
            Exception: If download fails
        """
        ...
    
    async def list_files(self, prefix: str) -> list[str]:
        """List files with given prefix in cloud storage.
        
        Args:
            prefix: Path prefix to filter files
            
        Returns:
            List of file paths matching the prefix
            
        Raises:
            Exception: If listing fails
        """
        ...
    
    async def delete(self, remote_path: str) -> None:
        """Delete file from cloud storage.
        
        Args:
            remote_path: Path to file in cloud storage
            
        Raises:
            Exception: If deletion fails
        """
        ...


class S3StorageAdapter:
    """AWS S3 storage adapter with streaming upload support.
    
    Uses boto3 for S3 operations. Enforces HTTPS and uses server-side encryption (SSE).
    Streams file uploads to avoid loading entire files into memory.
    """
    
    def __init__(
        self,
        bucket: str,
        region: str = "us-east-1",
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None
    ):
        """Initialize S3 storage adapter.
        
        Args:
            bucket: S3 bucket name
            region: AWS region (default: us-east-1)
            access_key_id: AWS access key ID (uses env var if not provided)
            secret_access_key: AWS secret access key (uses env var if not provided)
        """
        self.bucket = bucket
        self.region = region
        self.access_key_id = access_key_id or os.getenv("AWS_ACCESS_KEY_ID")
        self.secret_access_key = secret_access_key or os.getenv("AWS_SECRET_ACCESS_KEY")
        
        if not self.access_key_id or not self.secret_access_key:
            raise ValueError("AWS credentials not provided")
        
        self._client = None
    
    def _get_client(self):
        """Get or create boto3 S3 client (lazy initialization)."""
        if self._client is None:
            import boto3
            self._client = boto3.client(
                's3',
                region_name=self.region,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                config=boto3.session.Config(
                    signature_version='s3v4',
                    s3={'use_accelerate_endpoint': False}
                )
            )
        return self._client
    
    async def upload(self, local_path: str, remote_path: str) -> str:
        """Upload file to S3 with streaming and server-side encryption.
        
        Uses multipart upload for large files to stream data efficiently.
        Enforces HTTPS and enables SSE-S3 encryption at rest.
        """
        client = self._get_client()
        
        # Run blocking boto3 call in executor to avoid blocking event loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: client.upload_file(
                local_path,
                self.bucket,
                remote_path,
                ExtraArgs={
                    'ServerSideEncryption': 'AES256',  # Enable encryption at rest
                }
            )
        )
        
        # Return S3 URL (HTTPS enforced)
        url = f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{remote_path}"
        logger.info(f"Uploaded {local_path} to S3: {url}")
        return url
    
    async def download(self, remote_path: str, local_path: str) -> None:
        """Download file from S3."""
        client = self._get_client()
        
        # Ensure parent directory exists
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Run blocking boto3 call in executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: client.download_file(self.bucket, remote_path, local_path)
        )
        
        logger.info(f"Downloaded {remote_path} from S3 to {local_path}")
    
    async def list_files(self, prefix: str) -> list[str]:
        """List files in S3 bucket with given prefix."""
        client = self._get_client()
        
        # Run blocking boto3 call in executor
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        )
        
        # Extract file keys from response
        files = [obj['Key'] for obj in response.get('Contents', [])]
        logger.info(f"Listed {len(files)} files with prefix '{prefix}' in S3")
        return files
    
    async def delete(self, remote_path: str) -> None:
        """Delete file from S3."""
        client = self._get_client()
        
        # Run blocking boto3 call in executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: client.delete_object(Bucket=self.bucket, Key=remote_path)
        )
        
        logger.info(f"Deleted {remote_path} from S3")


class DropboxStorageAdapter:
    """Dropbox storage adapter with streaming upload support.
    
    Uses Dropbox SDK for operations. Enforces HTTPS (built into Dropbox API).
    Streams file uploads using chunked upload for large files.
    """
    
    def __init__(self, access_token: Optional[str] = None, base_path: str = "/backups"):
        """Initialize Dropbox storage adapter.
        
        Args:
            access_token: Dropbox access token (uses env var if not provided)
            base_path: Base path in Dropbox for all operations (default: /backups)
        """
        self.access_token = access_token or os.getenv("DROPBOX_ACCESS_TOKEN")
        self.base_path = base_path.rstrip('/')
        
        if not self.access_token:
            raise ValueError("Dropbox access token not provided")
        
        self._client = None
        self._dropbox_module = None
    
    def _get_client(self):
        """Get or create Dropbox client (lazy initialization)."""
        if self._client is None:
            import dropbox

            self._dropbox_module = dropbox
            self._client = dropbox.Dropbox(self.access_token)
        return self._client

    def _get_dropbox_module(self):
        if self._dropbox_module is None:
            self._get_client()
        return self._dropbox_module
    
    def _full_path(self, remote_path: str) -> str:
        """Convert relative path to full Dropbox path."""
        remote_path = remote_path.lstrip('/')
        return f"{self.base_path}/{remote_path}"
    
    async def upload(self, local_path: str, remote_path: str) -> str:
        """Upload file to Dropbox with streaming for large files.
        
        Uses chunked upload for files to stream data efficiently.
        HTTPS is enforced by Dropbox API. Dropbox provides encryption at rest.
        """
        client = self._get_client()
        dropbox_module = self._get_dropbox_module()
        full_path = self._full_path(remote_path)
        
        # Get file size to determine upload strategy
        file_size = os.path.getsize(local_path)
        chunk_size = 4 * 1024 * 1024  # 4MB chunks
        
        loop = asyncio.get_event_loop()
        
        if file_size <= chunk_size:
            # Small file: simple upload
            with open(local_path, 'rb') as f:
                data = f.read()
                await loop.run_in_executor(
                    None,
                    lambda: client.files_upload(data, full_path, mode=dropbox_module.files.WriteMode.overwrite)
                )
        else:
            # Large file: chunked upload for streaming
            with open(local_path, 'rb') as f:
                # Start upload session
                data = f.read(chunk_size)
                session_start = await loop.run_in_executor(
                    None,
                    lambda: client.files_upload_session_start(data)
                )
                session_id = session_start.session_id
                offset = len(data)
                
                # Upload chunks
                while True:
                    data = f.read(chunk_size)
                    if len(data) == 0:
                        break
                    
                    cursor = dropbox_module.files.UploadSessionCursor(session_id, offset)
                    await loop.run_in_executor(
                        None,
                        lambda: client.files_upload_session_append_v2(data, cursor)
                    )
                    offset += len(data)
                
                # Finish upload
                cursor = dropbox_module.files.UploadSessionCursor(session_id, offset)
                commit = dropbox_module.files.CommitInfo(full_path, mode=dropbox_module.files.WriteMode.overwrite)
                await loop.run_in_executor(
                    None,
                    lambda: client.files_upload_session_finish(b'', cursor, commit)
                )
        
        logger.info(f"Uploaded {local_path} to Dropbox: {full_path}")
        return full_path
    
    async def download(self, remote_path: str, local_path: str) -> None:
        """Download file from Dropbox."""
        client = self._get_client()
        full_path = self._full_path(remote_path)
        
        # Ensure parent directory exists
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Run blocking Dropbox call in executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: client.files_download_to_file(local_path, full_path)
        )
        
        logger.info(f"Downloaded {full_path} from Dropbox to {local_path}")
    
    async def list_files(self, prefix: str) -> list[str]:
        """List files in Dropbox with given prefix."""
        client = self._get_client()
        dropbox_module = self._get_dropbox_module()
        full_prefix = self._full_path(prefix)
        
        # Run blocking Dropbox call in executor
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: client.files_list_folder(full_prefix)
        )
        
        # Extract file paths (remove base_path prefix for consistency)
        files = []
        for entry in result.entries:
            if isinstance(entry, dropbox_module.files.FileMetadata):
                # Remove base_path to return relative paths
                relative_path = entry.path_display[len(self.base_path):].lstrip('/')
                files.append(relative_path)
        
        logger.info(f"Listed {len(files)} files with prefix '{prefix}' in Dropbox")
        return files
    
    async def delete(self, remote_path: str) -> None:
        """Delete file from Dropbox."""
        client = self._get_client()
        full_path = self._full_path(remote_path)
        
        # Run blocking Dropbox call in executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: client.files_delete_v2(full_path)
        )
        
        logger.info(f"Deleted {full_path} from Dropbox")


class GCSStorageAdapter:
    """Google Cloud Storage adapter with streaming upload support.
    
    Uses google-cloud-storage library. Enforces HTTPS and uses encryption at rest.
    Streams file uploads to avoid loading entire files into memory.
    """
    
    def __init__(
        self,
        bucket: str,
        project_id: Optional[str] = None,
        credentials_json: Optional[str] = None
    ):
        """Initialize GCS storage adapter.
        
        Args:
            bucket: GCS bucket name
            project_id: GCP project ID (uses env var if not provided)
            credentials_json: Service account credentials JSON (uses env var if not provided)
        """
        self.bucket_name = bucket
        self.project_id = project_id or os.getenv("GCS_PROJECT_ID")
        self.credentials_json = credentials_json or os.getenv("GCS_CREDENTIALS_JSON")
        
        if not self.project_id:
            raise ValueError("GCS project ID not provided")
        
        self._client = None
        self._bucket = None
    
    def _get_bucket(self):
        """Get or create GCS bucket client (lazy initialization)."""
        if self._bucket is None:
            from google.cloud import storage
            from google.oauth2 import service_account
            import json
            
            # Initialize client with credentials
            if self.credentials_json:
                # Parse credentials from JSON string
                creds_dict = json.loads(self.credentials_json)
                credentials = service_account.Credentials.from_service_account_info(creds_dict)
                self._client = storage.Client(project=self.project_id, credentials=credentials)
            else:
                # Use default credentials (from GOOGLE_APPLICATION_CREDENTIALS env var)
                self._client = storage.Client(project=self.project_id)
            
            self._bucket = self._client.bucket(self.bucket_name)
        
        return self._bucket
    
    async def upload(self, local_path: str, remote_path: str) -> str:
        """Upload file to GCS with streaming.
        
        Uses blob upload to stream data efficiently.
        HTTPS is enforced by GCS API. GCS provides encryption at rest by default.
        """
        bucket = self._get_bucket()
        blob = bucket.blob(remote_path)
        
        # Run blocking GCS call in executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: blob.upload_from_filename(local_path)
        )
        
        # Return GCS URL (HTTPS enforced)
        url = f"gs://{self.bucket_name}/{remote_path}"
        logger.info(f"Uploaded {local_path} to GCS: {url}")
        return url
    
    async def download(self, remote_path: str, local_path: str) -> None:
        """Download file from GCS."""
        bucket = self._get_bucket()
        blob = bucket.blob(remote_path)
        
        # Ensure parent directory exists
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Run blocking GCS call in executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: blob.download_to_filename(local_path)
        )
        
        logger.info(f"Downloaded {remote_path} from GCS to {local_path}")
    
    async def list_files(self, prefix: str) -> list[str]:
        """List files in GCS bucket with given prefix."""
        bucket = self._get_bucket()
        
        # Run blocking GCS call in executor
        loop = asyncio.get_event_loop()
        blobs = await loop.run_in_executor(
            None,
            lambda: list(bucket.list_blobs(prefix=prefix))
        )
        
        # Extract blob names
        files = [blob.name for blob in blobs]
        logger.info(f"Listed {len(files)} files with prefix '{prefix}' in GCS")
        return files
    
    async def delete(self, remote_path: str) -> None:
        """Delete file from GCS."""
        bucket = self._get_bucket()
        blob = bucket.blob(remote_path)
        
        # Run blocking GCS call in executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: blob.delete()
        )
        
        logger.info(f"Deleted {remote_path} from GCS")
