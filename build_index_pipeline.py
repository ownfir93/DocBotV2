#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_index_pipeline.py – Creates a vector store index from source documents in GCS.
"""

# Standard imports
import os
import sys
import time
from pathlib import Path
import tempfile
import json
import atexit
import logging

# --- START: Replit Service Account Key Handling ---
# Check if running in Replit and the JSON content secret is set
gcp_json_key_content = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
temp_key_file_path = None

if gcp_json_key_content:
    try:
        # Create a temporary file to store the key
        key_file_descriptor, temp_key_file_path = tempfile.mkstemp(suffix=".json")
        print(f"[INFO] Creating temporary key file at: {temp_key_file_path}")

        with os.fdopen(key_file_descriptor, 'w') as temp_key_file:
            temp_key_file.write(gcp_json_key_content)

        # Set the environment variable Google libraries expect
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = temp_key_file_path
        print(f"[INFO] GOOGLE_APPLICATION_CREDENTIALS set to: {temp_key_file_path}")

        # Register a cleanup function
        def cleanup_keyfile():
            if temp_key_file_path and os.path.exists(temp_key_file_path):
                print(f"[INFO] Cleaning up temporary key file: {temp_key_file_path}")
                try: os.remove(temp_key_file_path)
                except OSError as e: print(f"[WARN] Could not remove temp key file {temp_key_file_path}: {e}")
        atexit.register(cleanup_keyfile)

    except Exception as e:
        print(f"[ERROR] Failed to process GOOGLE_APPLICATION_CREDENTIALS_JSON secret: {e}", file=sys.stderr)
        # Exit if auth setup fails, as the rest of the script depends on it
        sys.exit(1)
else:
    # Check if GOOGLE_APPLICATION_CREDENTIALS is set directly (e.g., in Replit env)
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
         print("[ERROR] Authentication configuration missing.", file=sys.stderr)
         print("Ensure GOOGLE_APPLICATION_CREDENTIALS_JSON secret is set in Replit.", file=sys.stderr)
         sys.exit(1)
    else:
         print("[INFO] Using GOOGLE_APPLICATION_CREDENTIALS environment variable directly.")


# --- END: Replit Service Account Key Handling ---

from dotenv import load_dotenv
# Load environment variables
load_dotenv()

# Continue with the rest of your imports and code...

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_index_pipeline.py - Standalone script to:
1. List source documents from a GCS prefix.
2. Download/read documents, convert to text.
3. Generate text embeddings using OpenAI.
4. Build a Chroma vector index locally, storing GCS path in metadata.
5. Upload the generated Chroma index directory back to GCS.

Reads sources FROM GCS, writes index TO GCS.
"""

import os
import sys
import json
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

# Document Parsing
from pdfminer.high_level import extract_text
from pptx import Presentation
import docx2txt

# LangChain
try:
    from langchain_openai import OpenAIEmbeddings
    from langchain_chroma import Chroma
    from langchain.text_splitter import RecursiveCharacterTextSplitter
    from langchain_core.documents import Document
except ImportError as e:
    print(
        f"ERROR: LangChain packages not found: {e}. pip install -r requirements.txt"
    )
    sys.exit(1)

# Google Cloud
try:
    from google.cloud import storage
    from google.oauth2 import service_account
    from google.auth.exceptions import DefaultCredentialsError
except ImportError:
    print(
        "ERROR: google-cloud-storage not found. pip install google-cloud-storage"
    )
    sys.exit(1)
except DefaultCredentialsError:
    print(
        "ERROR: Google Cloud Default Credentials not found. Ensure `gcloud auth application-default login` or GOOGLE_APPLICATION_CREDENTIALS."
    )
    sys.exit(1)

# --- CONFIGURATION ---
# GCS Configuration (REQUIRED - Set in .env or environment)
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
GCS_SOURCE_PREFIX = os.getenv(
    "GCS_SOURCE_PREFIX",
    "source_documents/")  # Folder IN BUCKET with source PDFs, DOCX etc.
GCS_INDEX_PATH = os.getenv(
    "GCS_INDEX_PATH",
    "vector_index/chroma_db/")  # Folder IN BUCKET to store the built index

# Ensure trailing slashes for prefixes if needed by logic later
GCS_SOURCE_PREFIX = GCS_SOURCE_PREFIX.strip(
    '/') + '/' if GCS_SOURCE_PREFIX else ""
GCS_INDEX_PATH = GCS_INDEX_PATH.strip('/') + '/' if GCS_INDEX_PATH else ""

# Local Temporary Paths (Pipeline runtime only)
TEMP_DOWNLOAD_DIR = Path(tempfile.mkdtemp(prefix="pipeline_downloads_"))
TEMP_CHROMA_DB_PATH = Path(tempfile.mkdtemp(prefix="pipeline_chroma_build_"))

# OpenAI & Embedding Config
# Ensure OPENAI_API_KEY is set in environment
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME",
                                 "text-embedding-3-small")
TEXT_SPLIT_CHUNK_SIZE = int(os.getenv("TEXT_SPLIT_CHUNK_SIZE", 1000))
TEXT_SPLIT_CHUNK_OVERLAP = int(os.getenv("TEXT_SPLIT_CHUNK_OVERLAP", 200))

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(funcName)s:%(lineno)d] %(message)s")
# Quieten verbose libraries
for logger_name in [
        "openai", "httpx", "httpcore", "urllib3", "pdfminer", "chromadb",
        "google.cloud.storage", "google.auth", "googleapiclient"
]:
    logging.getLogger(logger_name).setLevel(logging.WARNING)
logging.getLogger("langchain").setLevel(logging.INFO)  # Keep Langchain info

# --- GLOBAL VARIABLES ---
embeddings_model = None
gcs_client = None
gcs_bucket = None


# --- GCS HELPER FUNCTIONS ---
def get_gcs_client_and_bucket():
    """Initializes GCS client and bucket object."""
    global gcs_client, gcs_bucket
    if gcs_client is None or gcs_bucket is None:
        if not GCS_BUCKET_NAME:
            logging.critical("GCS_BUCKET_NAME environment variable not set.")
            raise ValueError("GCS_BUCKET_NAME not configured.")
        try:
            key_file_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            if not key_file_path or not os.path.exists(key_file_path):
                 raise ValueError("GOOGLE_APPLICATION_CREDENTIALS path not valid or file missing.")

            # Explicitly load credentials from the file
            credentials = service_account.Credentials.from_service_account_file(key_file_path)
            logging.info(f"Explicitly loaded credentials from: {key_file_path}")

            # Initialize client with explicit project AND credentials
            gcs_client = storage.Client(project="elevado", credentials=credentials)
            gcs_bucket = gcs_client.get_bucket(GCS_BUCKET_NAME)
            logging.info(f"GCS client initialized and bucket '{GCS_BUCKET_NAME}' connected.")
        except Exception as e:
            logging.critical(
                f"Failed to initialize GCS client or get bucket '{GCS_BUCKET_NAME}': {e}",
                exc_info=True)
            raise
    return gcs_client, gcs_bucket


def list_gcs_source_files(prefix: str) -> List[storage.Blob]:
    """Lists supported document files in a GCS prefix."""
    _client, bucket = get_gcs_client_and_bucket()
    supported_exts = {".pdf", ".pptx", ".docx", ".txt", ".md", ".html"}
    source_blobs = []
    logging.info(
        f"Scanning gs://{GCS_BUCKET_NAME}/{prefix} for source files...")

    try:
        blobs = list(
            bucket.list_blobs(prefix=prefix))  # List all blobs in prefix
        for blob in blobs:
            # Skip "directory" objects and check extension
            if not blob.name.endswith('/') and Path(
                    blob.name).suffix.lower() in supported_exts:
                source_blobs.append(blob)
        logging.info(
            f"Found {len(source_blobs)} potential source files in GCS.")
        return source_blobs
    except Exception as e:
        logging.error(
            f"Failed to list GCS blobs in gs://{GCS_BUCKET_NAME}/{prefix}: {e}",
            exc_info=True)
        raise


def download_blob_to_temp(blob: storage.Blob, temp_dir: Path) -> Path:
    """Downloads a GCS blob to a temporary local file."""
    local_path = temp_dir / Path(blob.name).name  # Use only filename locally
    try:
        logging.debug(
            f"Downloading gs://{blob.bucket.name}/{blob.name} to {local_path}")
        blob.download_to_filename(str(local_path))
        return local_path
    except Exception as e:
        logging.error(f"Failed to download blob {blob.name}: {e}",
                      exc_info=True)
        raise


def upload_directory_to_gcs(local_path: Path, gcs_destination_prefix: str):
    """Uploads the contents of a local directory to a GCS prefix."""
    _client, bucket = get_gcs_client_and_bucket()
    logging.info(
        f"Uploading directory '{local_path}' contents to GCS path 'gs://{GCS_BUCKET_NAME}/{gcs_destination_prefix}'..."
    )
    assert local_path.is_dir()
    files_uploaded_count = 0
    for local_file in local_path.rglob('*'):
        if local_file.is_file():
            relative_path = local_file.relative_to(local_path)
            blob_path = Path(gcs_destination_prefix.strip('/')).joinpath(
                relative_path).as_posix()
            logging.debug(
                f"Uploading {local_file} to gs://{GCS_BUCKET_NAME}/{blob_path}"
            )
            try:
                blob = bucket.blob(blob_path)
                blob.upload_from_filename(str(local_file))
                files_uploaded_count += 1
            except Exception as e:
                logging.error(
                    f"Failed to upload {local_file} to {blob_path}: {e}",
                    exc_info=True)
                # Decide if one failed upload should stop the whole process
                raise  # Option: Stop on first error
    logging.info(
        f"Successfully uploaded {files_uploaded_count} index files to GCS.")


# --- DOCUMENT PROCESSING FUNCTION ---
def process_source_blob(blob: storage.Blob,
                        temp_dir: Path) -> Optional[Document]:
    """Downloads blob, extracts text, creates Langchain Document with GCS path metadata."""
    local_file_path = None
    try:
        local_file_path = download_blob_to_temp(blob, temp_dir)
        text = ""
        file_lower = local_file_path.name.lower()

        # --- Extract Text (same logic as before, but using local_file_path) ---
        if file_lower.endswith(".pdf"):
            text = extract_text(str(local_file_path))
        elif file_lower.endswith(".pptx"):
            prs = Presentation(str(local_file_path))
            text = "\n\n".join(shape.text.strip() for slide in prs.slides
                               for shape in slide.shapes
                               if hasattr(shape, "text") and shape.text
                               and shape.text.strip())
        elif file_lower.endswith(".docx"):
            text = docx2txt.process(str(local_file_path))
        elif file_lower.endswith((".txt", ".md", ".html")):
            try:
                text = local_file_path.read_text("utf-8")
            except UnicodeDecodeError:
                text = local_file_path.read_text("latin-1", errors="replace")
        else:
            logging.warning(f"Skipping unsupported file type: {blob.name}")
            return None  # Skip this blob

        # --- Clean Text ---
        text = re.sub(r'\s{3,}', '\n\n', text).strip()
        if not text:
            logging.warning(f"No text extracted from {blob.name}. Skipping.")
            return None

        # --- Create Document with Metadata ---
        # Store the GCS path for linking later
        metadata = {
            "source_gcs_path":
            f"gs://{blob.bucket.name}/{blob.name}",
            "original_filename":
            Path(blob.name).name,
            # Add other metadata if needed (e.g., inferring document type)
            # Example type detection based on GCS path:
            "document_type":
            Path(blob.name).parent.name if '/' in blob.name else "root"
        }
        logging.debug(
            f"Processed blob {blob.name}, extracted {len(text)} chars.")
        return Document(page_content=text, metadata=metadata)

    except Exception as e:
        logging.error(f"Error processing blob {blob.name}: {e}", exc_info=True)
        return None  # Skip on error
    finally:
        # Clean up temporary downloaded file
        if local_file_path and local_file_path.exists():
            try:
                local_file_path.unlink()
            except OSError:
                logging.warning(
                    f"Could not delete temp file: {local_file_path}")


# --- INDEX BUILDING FUNCTION ---
def build_chroma_index(documents: List[Document],
                       output_db_path: Path) -> bool:
    """Builds Chroma DB locally from processed documents."""
    global embeddings_model
    if not embeddings_model:
        logging.critical("Embeddings model not initialized.")
        return False
    if not documents:
        logging.error("No documents provided to build index.")
        return False

    output_db_path_str = str(output_db_path)

    # Clean previous build attempt
    if output_db_path.exists():
        logging.warning(
            f"Removing existing temporary Chroma directory: {output_db_path_str}"
        )
        try:
            shutil.rmtree(output_db_path)
        except OSError as e:
            logging.error(f"Error removing outdated Chroma directory: {e}",
                          exc_info=True)
            return False

    try:
        # --- Split Documents ---
        logging.info(f"Splitting {len(documents)} documents...")
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=TEXT_SPLIT_CHUNK_SIZE,
            chunk_overlap=TEXT_SPLIT_CHUNK_OVERLAP,
            # Keep metadata when splitting
            keep_separator=False  # Often better for Chroma chunking
        )
        docs_split = text_splitter.split_documents(documents)
        logging.info(f"Split into {len(docs_split)} chunks.")
        if not docs_split:
            logging.error("No chunks generated after splitting.")
            return False

        # --- Create Chroma Database Locally ---
        logging.info(
            f"Creating temporary Chroma database at: {output_db_path_str}...")
        # This step performs the embedding and writing to local disk
        Chroma.from_documents(documents=docs_split,
                              embedding=embeddings_model,
                              persist_directory=output_db_path_str)
        logging.info(f"Temporary Chroma database created locally.")
        return True

    except Exception as e:
        logging.error(f"Failed during index building: {e}", exc_info=True)
        if output_db_path.exists():  # Clean up failed build
            try:
                shutil.rmtree(output_db_path)
            except OSError as rm_err:
                logging.error(f"Failed cleanup {output_db_path_str}: {rm_err}")
        return False


# --- MAIN PIPELINE EXECUTION ---
def main():
    """Runs the full GCS-based indexing pipeline."""
    logging.info("--- Starting GCS Data Indexing Pipeline ---")

    # --- Validate Configuration ---
    if not GCS_BUCKET_NAME or not GCS_INDEX_PATH or not GCS_SOURCE_PREFIX:
        logging.critical(
            "GCS_BUCKET_NAME, GCS_SOURCE_PREFIX, and GCS_INDEX_PATH must be set. Exiting."
        )
        sys.exit(1)
    if not os.getenv("OPENAI_API_KEY"):
        logging.critical("OPENAI_API_KEY not set. Exiting.")
        sys.exit(1)

    # --- Initialize Services ---
    global embeddings_model
    try:
        get_gcs_client_and_bucket()  # Init GCS first
        
        # Initialize Embeddings directly, letting it create its own client
        embeddings_model = OpenAIEmbeddings(model=EMBEDDING_MODEL_NAME)
        logging.info(f"OpenAI Embeddings model '{EMBEDDING_MODEL_NAME}' initialized.")

    except Exception as init_err:
        logging.critical(
            f"Service initialization failed: {init_err}. Exiting.",
            exc_info=True)
        sys.exit(1)

    all_docs: List[Document] = []
    processed_count = 0
    error_count = 0

    try:
        # --- Step 1: List Source Files from GCS ---
        source_blobs = list_gcs_source_files(GCS_SOURCE_PREFIX)
        if not source_blobs:
            logging.warning(
                "No source files found in GCS path. Pipeline finished.")
            return  # Exit cleanly

        # --- Step 2: Process Each Source File ---
        logging.info(f"Processing {len(source_blobs)} source blobs...")
        for blob in source_blobs:
            doc = process_source_blob(blob, TEMP_DOWNLOAD_DIR)
            if doc:
                all_docs.append(doc)
                processed_count += 1
            else:
                error_count += 1

        logging.info(
            f"Blob Processing Complete. Success: {processed_count}, Errors: {error_count}"
        )
        if not all_docs:
            logging.error(
                "No documents were successfully processed. Cannot build index."
            )
            return

        # --- Step 3: Build Chroma Index Locally ---
        if not build_chroma_index(all_docs, TEMP_CHROMA_DB_PATH):
            logging.error("Chroma index building failed. Aborting upload.")
            return

        # --- Step 4: Upload Index to GCS ---
        upload_directory_to_gcs(TEMP_CHROMA_DB_PATH, GCS_INDEX_PATH)

    except Exception as pipeline_err:
        logging.error(f"Pipeline execution failed: {pipeline_err}",
                      exc_info=True)
    finally:
        # --- Step 5: Cleanup Temporary Local Directories ---
        logging.info("Cleaning up temporary local directories...")
        try:
            shutil.rmtree(TEMP_DOWNLOAD_DIR)
            shutil.rmtree(TEMP_CHROMA_DB_PATH)
            logging.info("Temporary directories cleaned up.")
        except OSError as e:
            logging.warning(f"Could not remove temporary directories: {e}")

    logging.info("--- GCS Data Indexing Pipeline Finished ---")


if __name__ == "__main__":
    main()