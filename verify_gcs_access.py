# --- START: Replit Service Account Key Handling ---
import os
import sys
import tempfile
import json
import atexit
import time

# Check if running in Replit and the JSON content secret is set
gcp_json_key_content = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
temp_key_file_path = None

if gcp_json_key_content:
    try:
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

# --- GCS Verification Logic ---
from google.cloud import storage
from google.api_core import exceptions as google_exceptions # Import exceptions

print("\n[INFO] Attempting to Initialize GCS Client...")

try:
    # Explicitly create client - it should now use the env var set above
    storage_client = storage.Client()
    print("[SUCCESS] GCS Storage Client initialized successfully.")

    bucket_name = os.environ.get("GCS_BUCKET_NAME")
    if not bucket_name:
        print("[ERROR] GCS_BUCKET_NAME secret/environment variable not set.", file=sys.stderr)
        sys.exit(1)

    print(f"[DEBUG] Value read from GCS_BUCKET_NAME secret: '{bucket_name}'")

    print(f"[INFO] Target Bucket: {bucket_name}")

    # 1. Get Bucket (Checks basic connectivity and bucket existence)
    print(f"\n[INFO] Attempting to access bucket '{bucket_name}'...")
    try:
        bucket = storage_client.get_bucket(bucket_name)
        print(f"[SUCCESS] Successfully accessed bucket '{bucket_name}'.")
    except google_exceptions.NotFound:
        print(f"[ERROR] Bucket '{bucket_name}' not found.", file=sys.stderr)
        sys.exit(1)
    except google_exceptions.Forbidden as e:
        print(f"[ERROR] Permission denied accessing bucket '{bucket_name}'. Check IAM permissions for the service account.", file=sys.stderr)
        print(f"Details: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Failed to get bucket '{bucket_name}': {e}", file=sys.stderr)
        sys.exit(1)


    # 2. Attempt to Write (Upload) a test file
    test_blob_name = f"replit_write_test_{int(time.time())}.txt"
    test_content = f"This is a test file uploaded from Replit at {time.time()}."
    print(f"\n[INFO] Attempting to upload test file '{test_blob_name}' to bucket '{bucket_name}'...")
    try:
        blob = bucket.blob(test_blob_name)
        blob.upload_from_string(test_content)
        print(f"[SUCCESS] Successfully uploaded test file '{test_blob_name}'.")
    except google_exceptions.Forbidden as e:
        print(f"[ERROR] Permission denied uploading file '{test_blob_name}'. Service account needs write permissions (e.g., Storage Object Creator/Admin).", file=sys.stderr)
        print(f"Details: {e}", file=sys.stderr)
        # Don't exit yet, try delete if file might exist from previous run
    except Exception as e:
        print(f"[ERROR] Failed to upload test file '{test_blob_name}': {e}", file=sys.stderr)
        sys.exit(1) # Exit on general upload failure


    # 3. Attempt to Read the test file (Verify upload)
    print(f"\n[INFO] Attempting to read test file '{test_blob_name}'...")
    try:
        blob_to_read = bucket.blob(test_blob_name)
        downloaded_content = blob_to_read.download_as_text()
        if downloaded_content == test_content:
            print(f"[SUCCESS] Successfully read back test file '{test_blob_name}' with matching content.")
        else:
            print(f"[WARN] Read back test file '{test_blob_name}', but content did not match!")
    except google_exceptions.NotFound:
         print(f"[WARN] Could not read back test file '{test_blob_name}' (might indicate upload failed or timing issue).")
    except google_exceptions.Forbidden as e:
        print(f"[ERROR] Permission denied reading file '{test_blob_name}'. Service account needs read permissions (e.g., Storage Object Viewer).", file=sys.stderr)
        print(f"Details: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[ERROR] Failed to read test file '{test_blob_name}': {e}", file=sys.stderr)


    # 4. Attempt to Delete the test file
    print(f"\n[INFO] Attempting to delete test file '{test_blob_name}'...")
    try:
        blob_to_delete = bucket.blob(test_blob_name)
        blob_to_delete.delete()
        print(f"[SUCCESS] Successfully deleted test file '{test_blob_name}'.")
    except google_exceptions.NotFound:
         print(f"[WARN] Could not delete test file '{test_blob_name}' as it was not found (might indicate upload failed or was already deleted).")
    except google_exceptions.Forbidden as e:
        print(f"[ERROR] Permission denied deleting file '{test_blob_name}'. Service account needs delete permissions (e.g., Storage Object Admin).", file=sys.stderr)
        print(f"Details: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[ERROR] Failed to delete test file '{test_blob_name}': {e}", file=sys.stderr)

    print("\n[INFO] GCS Access Verification Script Finished.")


except Exception as e:
    print(f"\n[CRITICAL ERROR] An unexpected error occurred during GCS client initialization or operation: {e}", file=sys.stderr)
    print("[INFO] Check previous logs for authentication or configuration issues.", file=sys.stderr)
    sys.exit(1)