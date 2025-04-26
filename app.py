#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py – Handles chat requests, interacts with LangChain RAG, and formats responses.
Runtime Version: Loads index from GCS, uses session management for chat history.
"""

# --- Standard Imports ---
import warnings
import logging
import os
import json
import asyncio
import shutil
import re
import sys
import time
import html
import pprint
import threading
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional
from operator import itemgetter

# --- Web Framework ---
from flask import Flask, request, jsonify, render_template, session
from flask_session import Session
from waitress import serve
import redis

# --- Google Cloud ---
from google.cloud import storage
from google.oauth2 import service_account
from dotenv import load_dotenv
# Load environment variables
load_dotenv()

# --- Replit Service Account Key Handling ---
# This code manages GCP credentials in the Replit environment
import json
import tempfile

# Get the service account key from Replit Secrets
gcp_key_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if gcp_key_json:
    try:
        # Parse to verify it's valid JSON
        json.loads(gcp_key_json)

        # Write to a temporary file that only this process can read
        temp_key_file = tempfile.NamedTemporaryFile(delete=False, mode='w')
        temp_key_file.write(gcp_key_json)
        temp_key_file.close()

        # Set environment variable to point to this file
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = temp_key_file.name
        print(f"GCP credentials loaded from GOOGLE_APPLICATION_CREDENTIALS_JSON to {temp_key_file.name}")
    except json.JSONDecodeError:
        print("ERROR: GOOGLE_APPLICATION_CREDENTIALS_JSON contains invalid JSON")
    except Exception as e:
        print(f"ERROR setting up GCP credentials: {e}")
else:
    print("WARNING: GOOGLE_APPLICATION_CREDENTIALS_JSON not found in environment")

# --- Document Parsing ---
from pdfminer.high_level import extract_text
from pptx import Presentation
import docx2txt
# Keep Pillow import in case needed by dependencies, ensure it's in requirements.txt
from PIL import Image

# --- LangChain Core Imports ---
try:
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.embeddings import Embeddings
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.runnables import RunnablePassthrough, RunnableParallel, Runnable, RunnableLambda
    from langchain_chroma import Chroma
    from langchain.text_splitter import RecursiveCharacterTextSplitter
    from langchain_community.document_loaders import DirectoryLoader, TextLoader
    from langchain_core.documents import Document
    from langchain_core.messages import HumanMessage, AIMessage
    from langchain.chains.combine_documents import create_stuff_documents_chain
    from langchain.retrievers.self_query.base import SelfQueryRetriever
    from langchain.retrievers.self_query.chroma import ChromaTranslator
    from langchain.chains.query_constructor.base import AttributeInfo
    from langchain.retrievers import ContextualCompressionRetriever
    from langchain_cohere import CohereRerank

except ImportError as e:
    print(f"ERROR: Required LangChain packages not found: {e}. \n"
          "Please ensure all packages from requirements.txt are installed.")
    sys.exit(1)

# --- Warnings ---
warnings.filterwarnings("ignore", message="^Attempting to load an old version of Faiss index", category=UserWarning)
warnings.filterwarnings("ignore", message="^Trying to unpickle estimator .* from version .*", category=UserWarning)
warnings.filterwarnings("ignore", message="Mixing V1 models and V2 models", category=UserWarning)
warnings.filterwarnings("ignore", message="This API is in beta and may change in the future", category=UserWarning)

# --- CONFIG ---
# GCS Configuration
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
GCS_INDEX_PATH = os.getenv("GCS_INDEX_PATH", "vector_index/chroma_db/") # Path IN BUCKET where index is stored
GCS_INDEX_PATH = GCS_INDEX_PATH.strip('/') + '/' if GCS_INDEX_PATH else ""

# Flask Session Configuration
# REQUIRED - Set a strong secret key in your environment!
SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "a-very-strong-secret-key-please-change")
SESSION_TYPE = os.getenv("SESSION_TYPE", "filesystem") # Use 'filesystem' for local sessions
SESSION_PERMANENT = os.getenv("SESSION_PERMANENT", "True").lower() == "true"
SESSION_USE_SIGNER = os.getenv("SESSION_USE_SIGNER", "True").lower() == "true" # Encrypt cookie
SESSION_FILE_DIR = tempfile.mkdtemp(prefix="flask_session_")
SESSION_FILE_THRESHOLD = 500  # Maximum number of items the session stores

# Optional: Signed URL expiration time (in seconds)
SIGNED_URL_EXPIRATION = int(os.getenv("SIGNED_URL_EXPIRATION", 900)) # 15 minutes default

# RAG Configuration
RETRIEVAL_TOP_K = 10  # How many docs to retrieve
MAX_HISTORY_TURNS = 5  # Max number of conversation turns to keep

# LLM Configuration
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "gpt-4o")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "text-embedding-3-small")

# --- LOGGING ---
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] [%(funcName)s:%(lineno)d] %(message)s")
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("chromadb").setLevel(logging.WARNING)  # Quieten Chroma logs unless debugging Chroma itself
logging.getLogger("langchain").setLevel(logging.INFO)
logging.getLogger("langchain_community").setLevel(logging.INFO)
meta_logger = logging.getLogger('MetadataDebug')
meta_logger.setLevel(logging.DEBUG)

# Check required environment variables
for var in ["OPENAI_API_KEY", "GCS_BUCKET_NAME", "GCS_INDEX_PATH", "FLASK_SECRET_KEY"]:
    if not os.getenv(var):
        logging.critical(f"Required environment variable {var} is not set.")
        if var in ["OPENAI_API_KEY", "GCS_BUCKET_NAME", "GCS_INDEX_PATH"]:
            sys.exit(1)

loop = asyncio.new_event_loop()
loop_thread = None

# --- ASYNC HELPER ---
def start_loop_thread():
    global loop_thread
    def run_loop_forever(loop): asyncio.set_event_loop(loop); loop.run_forever()
    if loop_thread is None or not loop_thread.is_alive():
        loop_thread = threading.Thread(target=run_loop_forever, args=(loop,), daemon=True)
        loop_thread.start()
        logging.info("Event loop thread started.")

def run_async(coro):
    if loop_thread is None or not loop_thread.is_alive() or loop.is_closed():
        start_loop_thread()
        time.sleep(0.1)
    if not loop.is_running():
        raise RuntimeError("Async event loop is not available.")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=300)  # Increased timeout
    except asyncio.TimeoutError:
        future.cancel()
        raise TimeoutError("Async operation timed out after 300 seconds.")
    except Exception as e:
        logging.error(f"Error during async execution: {e}", exc_info=False)
        raise

# --- FLASK APP & GLOBALS ---
app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
app.config['SESSION_TYPE'] = SESSION_TYPE
app.config['SESSION_PERMANENT'] = SESSION_PERMANENT
app.config['SESSION_USE_SIGNER'] = SESSION_USE_SIGNER
app.config['SESSION_FILE_DIR'] = SESSION_FILE_DIR
app.config['SESSION_FILE_THRESHOLD'] = SESSION_FILE_THRESHOLD

# Initialize the session extension
Session(app)

# --- LangChain Globals ---
llm_instance: Optional[BaseChatModel] = None
embeddings_model: Optional[Embeddings] = None
vector_store: Optional[Chroma] = None
rag_chain: Optional[Runnable] = None

# --- GCS CLIENT (for app runtime) ---
gcs_client_runtime = None
gcs_bucket_runtime = None

def get_gcs_runtime_client_and_bucket():
    """Initializes GCS client and bucket object using explicit credentials."""
    global gcs_client_runtime, gcs_bucket_runtime
    if gcs_client_runtime is None or gcs_bucket_runtime is None:
        if not GCS_BUCKET_NAME:
             raise ValueError("GCS_BUCKET_NAME not configured for runtime.")
        try:
            # Get the keyfile path set by the Replit secret handling code
            key_file_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            if not key_file_path or not os.path.exists(key_file_path):
                 raise ValueError(f"GOOGLE_APPLICATION_CREDENTIALS path ('{key_file_path}') not valid or file missing for runtime.")

            # Explicitly load credentials from the file
            credentials = service_account.Credentials.from_service_account_file(key_file_path)
            logging.info(f"Explicitly loaded runtime credentials from: {key_file_path}")

            # Initialize client with explicit project AND explicitly loaded credentials
            # Read project ID from env var set via secrets
            project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
            if not project_id:
                logging.warning("GOOGLE_CLOUD_PROJECT environment variable not set, using 'elevado' as fallback")
                project_id = "elevado"

            gcs_client_runtime = storage.Client(project=project_id, credentials=credentials)

            # Now get the bucket object using the initialized client
            gcs_bucket_runtime = gcs_client_runtime.get_bucket(GCS_BUCKET_NAME)
            logging.info(f"Runtime GCS client initialized and bucket '{GCS_BUCKET_NAME}' accessed.")

        except Exception as e:
            logging.critical(f"Failed explicit runtime GCS init for bucket '{GCS_BUCKET_NAME}': {e}", exc_info=True)
            raise # Re-raise to stop the app initialization
    return gcs_client_runtime, gcs_bucket_runtime

def download_gcs_directory(gcs_prefix: str, local_dest: Path):
    """Downloads files from a GCS prefix to a local directory."""
    _client, bucket = get_gcs_runtime_client_and_bucket()
    logging.info(f"Downloading index from gs://{GCS_BUCKET_NAME}/{gcs_prefix} to {local_dest}...")
    local_dest.mkdir(parents=True, exist_ok=True)
    blobs = list(bucket.list_blobs(prefix=gcs_prefix))
    if not blobs:
         raise FileNotFoundError(f"No index files found in GCS at gs://{GCS_BUCKET_NAME}/{gcs_prefix}")

    downloaded_count = 0
    for blob in blobs:
        # Avoid downloading "directory" markers if they exist
        if blob.name.endswith('/'):
            continue

        # Create local directory structure mirroring GCS prefix
        relative_path = Path(blob.name).relative_to(gcs_prefix.rstrip('/'))
        local_file_path = local_dest / relative_path
        local_file_path.parent.mkdir(parents=True, exist_ok=True)

        logging.debug(f"Downloading {blob.name} to {local_file_path}")
        try:
             blob.download_to_filename(str(local_file_path))
             downloaded_count += 1
        except Exception as e:
             logging.error(f"Failed to download index file {blob.name}: {e}", exc_info=True)
             raise # Fail fast if index download fails

    logging.info(f"Downloaded {downloaded_count} index files.")

def generate_gcs_signed_url(object_name: str, expiration: int = SIGNED_URL_EXPIRATION) -> Optional[str]:
    """Generates a signed URL for a GCS object."""
    if not object_name:
        logging.warning("generate_gcs_signed_url called with empty object_name.")
        return None

    # Ensure GCS path stored in metadata doesn't have gs:// prefix for blob lookup
    if object_name.startswith(f"gs://{GCS_BUCKET_NAME}/"):
         object_name = object_name[len(f"gs://{GCS_BUCKET_NAME}/"):]

    try:
        _client, bucket = get_gcs_runtime_client_and_bucket()
        blob = bucket.blob(object_name)

        # Check if blob exists before generating URL (optional, adds latency)
        # if not blob.exists():
        #     logging.warning(f"Cannot generate signed URL. Object not found: gs://{GCS_BUCKET_NAME}/{object_name}")
        #     return None

        url = blob.generate_signed_url(
            version="v4",
            # This expiration is read from env var, defaults to 900 seconds
            expiration=timedelta(seconds=expiration),
            method="GET",
        )
        logging.debug(f"Generated signed URL for: gs://{GCS_BUCKET_NAME}/{object_name}")
        return url
    except Exception as e:
        logging.error(f"Failed to generate signed URL for {object_name}: {e}", exc_info=True)
        return None

def load_vectorstore() -> Optional[Chroma]:
    """Downloads Chroma DB from GCS and loads it."""
    global embeddings_model
    if not embeddings_model:
        logging.critical("Embeddings model not initialized before vector store load.")
        return None # Or raise error / sys.exit

    if not GCS_BUCKET_NAME or not GCS_INDEX_PATH:
         logging.critical("GCS bucket/path for index not configured.")
         return None

    # Create a temporary local directory for the downloaded index
    # This directory will be cleaned up when the app exits if possible,
    # but in container environments it exists for the life of the container.
    local_temp_index_dir = Path(tempfile.mkdtemp(prefix="chroma_index_"))
    logging.info(f"Using temporary directory for index: {local_temp_index_dir}")

    try:
        # Step 1: Download index files from GCS
        download_gcs_directory(GCS_INDEX_PATH, local_temp_index_dir)

        # Step 2: Load Chroma from the local temporary directory
        logging.info(f"Attempting to load Chroma database from local path: {local_temp_index_dir}")
        vector_store_instance = Chroma(
            persist_directory=str(local_temp_index_dir),
            embedding_function=embeddings_model
        )

        # Optional: Perform a quick check
        vector_store_instance.get(limit=1)
        logging.info("Successfully loaded Chroma database from GCS download.")

        # Optional: Add cleanup hook for the temporary directory if needed,
        # though in containers it might not be strictly necessary/possible.
        # atexit.register(shutil.rmtree, local_temp_index_dir, ignore_errors=True)

        return vector_store_instance

    except FileNotFoundError as fnf_err:
         logging.critical(f"Index not found in GCS or download failed: {fnf_err}")
         try: shutil.rmtree(local_temp_index_dir) # Clean up temp dir on failure
         except OSError: pass
         return None
    except Exception as e:
        logging.critical(f"Failed to download or load Chroma DB from GCS: {e}", exc_info=True)
        try: shutil.rmtree(local_temp_index_dir) # Clean up temp dir on failure
        except OSError: pass
        return None

# --- QUERY & EXTRACTION ---

def extract_source_info(doc: Document) -> Dict[str, Any] | None:
    """Extract key information from a Document for display, using GCS path."""
    if not doc: return None
    try:
        metadata = doc.metadata if hasattr(doc, 'metadata') else {}
        content = doc.page_content if hasattr(doc, 'page_content') else ""

        # --- Get required metadata ---
        # Use original_filename if present, fallback to deriving from GCS path
        display_filename = metadata.get('original_file_name')
        gcs_path = metadata.get('source_gcs_path') # Key must match what pipeline stored

        if not gcs_path:
             logging.warning(f"Missing 'source_gcs_path' in metadata for document content: {content[:100]}...")
             # Fallback or error handling needed if path is essential
             return None # Or return with filename only if possible

        if not display_filename:
             # Derive filename from GCS path if not explicitly stored
             display_filename = Path(gcs_path).name

        # --- Generate Signed URL ---
        signed_link = generate_gcs_signed_url(gcs_path) # Pass the full gs:// path or just object path

        return {
            "filename": display_filename,
            "link": signed_link if signed_link else "#error-generating-link", # Provide fallback link
            "content": content if content else "[No content available]",
            # Add other metadata if needed
            "gcs_path": gcs_path # Include for debugging?
        }

    except Exception as e:
        logging.error(f"Unexpected error in extract_source_info: {e}", exc_info=True)
        return None

# --- RAG Chain Invocation ---
async def _ask_rag_chain(q: str, chat_history: List = []) -> Dict[str, Any]:
    """Async function to invoke the RAG chain with the provided question and chat history."""
    try:
        logging.debug(f"_ask_rag_chain: Starting for question: {q[:50]}...")

        # Convert chat history to LangChain format if needed
        formatted_history = []
        for i in range(0, len(chat_history), 2):
            if i+1 < len(chat_history):  # Ensure we have both human and AI messages
                formatted_history.append(chat_history[i])  # Human message
                formatted_history.append(chat_history[i+1])  # AI message

        logging.debug(f"_ask_rag_chain: Formatted {len(chat_history)} messages to {len(formatted_history)} for chain input")

        # Invoke the RAG chain
        logging.debug("_ask_rag_chain: About to invoke RAG chain...")
        chain_inputs = {"input": q, "chat_history": formatted_history}

        logging.debug(f"_ask_rag_chain: Chain inputs: {chain_inputs}")
        result = await rag_chain.ainvoke(chain_inputs)

        if result is None:
            logging.error("_ask_rag_chain: RAG chain returned None!")
            return {"answer": "Error: RAG chain returned None.", "context": []}

        logging.debug(f"_ask_rag_chain: Chain returned result of type {type(result)}")
        logging.debug(f"_ask_rag_chain: Result keys: {result.keys() if isinstance(result, dict) else 'not a dict'}")

        return result
    except Exception as e:
        logging.error(f"_ask_rag_chain: Exception during chain execution: {e}", exc_info=True)
        return {"answer": f"Error in RAG chain execution: {str(e)}", "context": []}

def _ask_blocking(q: str, chat_history: List = []) -> Dict[str, Any]:
    """Synchronous wrapper for the async RAG chain invocation."""
    try:
        logging.debug(f"_ask_blocking: Starting for question: {q[:50]}...")
        if rag_chain is None:
            logging.error("_ask_blocking: RAG chain is None!")
            return {"answer": "Error: RAG chain is not initialized.", "context": []}

        result = run_async(_ask_rag_chain(q, chat_history))

        if result is None:
            logging.error("_ask_blocking: run_async(_ask_rag_chain) returned None!")
            return {"answer": "Error: Async RAG chain invocation returned None.", "context": []}

        logging.debug(f"_ask_blocking: Successfully got result of type {type(result)}")
        return result
    except Exception as e:
        logging.error(f"_ask_blocking: Exception during chain execution: {e}", exc_info=True)
        return {"answer": f"Error during processing: {str(e)}", "context": []}

def log_query(q: str, resp: Dict[str, Any], main_rec: Optional[Dict] = None, other_src: Optional[List] = None):
    """Log query and response information to standard logging."""
    try:
        timestamp = datetime.now().isoformat()
        src_count = len(resp.get("context", [])) if isinstance(resp, dict) else 0
        answer_preview = resp.get("answer", "")[:100] + "..." if isinstance(resp, dict) else str(resp)[:100] + "..."

        main_src = f"{main_rec['filename']}" if main_rec and isinstance(main_rec, dict) else "None"
        other_count = len(other_src) if other_src else 0

        logging.info(f"QUERY_LOG: [Time: {timestamp}] [Question: '{q[:50]}...'] [Sources: {src_count}] [MainRec: {main_src}] [OtherSrc: {other_count}] [Answer: '{answer_preview}']")
    except Exception as e:
        logging.error(f"Error logging query: {e}", exc_info=True)

# --- CHAT ENDPOINT ---
@app.route("/")
def index():
    return render_template("index.html", bot_name="DocBot")

async def _targeted_search(client_name: str) -> List[Document]:
    """Perform a targeted search for documents specifically about a client."""
    try:
        logging.info(f"Attempting targeted search for client: '{client_name}'")
        
        # Determine if we're looking for B2C content
        is_b2c_search = client_name.lower() in ["b2c", "retail", "consumer", "ecommerce"] or "b2c" in client_name.lower()
        
        search_query = client_name
        if is_b2c_search:
            search_query = "B2C case study" if "case study" not in client_name.lower() else client_name
            logging.info(f"Detected B2C search, using query: '{search_query}'")

        # First search for client name in win reports
        filter_condition = {
            "document_type": {"$eq": "win_report"}
        }
        
        if is_b2c_search:
            # For B2C searches, don't filter by document_type initially
            docs = await vector_store.asimilarity_search(
                query=search_query,
                k=5  # Get top 5
            )
            logging.info(f"B2C targeted search found {len(docs)} documents for '{search_query}'")
        else:
            # Use vector store's similarity search with the client name
            docs = await vector_store.asimilarity_search(
                query=search_query,
                k=5,  # Get top 5
                filter=filter_condition
            )
            logging.info(f"Targeted search found {len(docs)} win_report documents for '{search_query}'")

        # If no win reports, try case studies
        if not docs:
            logging.info(f"No win reports found, trying case_study documents for '{search_query}'")
            filter_condition = {
                "document_type": {"$eq": "case_study"}
            }

            docs = await vector_store.asimilarity_search(
                query=search_query,
                k=5,
                filter=filter_condition
            )

            logging.info(f"Targeted search found {len(docs)} case_study documents for '{search_query}'")

        # If still nothing, try a broader search
        if not docs:
            logging.info(f"No specific documents found, trying without type filter for '{search_query}'")
            docs = await vector_store.asimilarity_search(
                query=search_query,
                k=5
            )
            logging.info(f"Broader targeted search found {len(docs)} documents for '{search_query}'")

        # Filter and log results
        client_in_filename_docs = []
        client_in_content_docs = []

        for doc in docs:
            filename = doc.metadata.get('original_file_name', '')
            content_sample = doc.page_content[:200].lower()

            if filename and client_name.lower() in filename.lower():
                client_in_filename_docs.append(doc)
                logging.debug(f"Found client in filename: {filename}")
            elif client_name.lower() in content_sample:
                client_in_content_docs.append(doc)
                logging.debug(f"Found client in content: {filename}")

        # Prioritize docs with client in filename, then fallback to content
        prioritized_docs = client_in_filename_docs + client_in_content_docs

        if prioritized_docs:
            return prioritized_docs
        else:
            return docs  # Return whatever we found, even if client name not directly matched

    except Exception as e:
        logging.error(f"Error during targeted search for '{client_name}': {e}", exc_info=True)
        return []  # Return empty list on error

@app.route("/api/chat", methods=["POST"])
def api_chat():
    global vector_store, rag_chain
    start_time = time.time(); data = request.json or {}; q = data.get("question", "").strip()
    if not q: return jsonify(error="Question cannot be empty."), 400
    logging.info(f"Received question: {q[:70]}...")

    if not vector_store or not rag_chain:
        logging.error("Vector store or RAG chain unavailable.")
        return jsonify(answer_html="<p>KB unavailable or not initialized.</p>", main_recommendation=None), 503

    # --- Session History Handling ---
    # Initialize history in session if it doesn't exist
    if 'chat_history' not in session:
        session['chat_history'] = []  # Store as list of dicts for JSON serialization

    # Load history from session (Convert back to LangChain messages if needed by chain)
    # The RAG chain expects BaseMessage objects (HumanMessage, AIMessage)
    # We store dicts in session, so convert back before passing to chain
    current_history_dicts = session['chat_history']
    current_history_messages = []
    for msg_dict in current_history_dicts:
        if msg_dict.get("type") == "human":
            current_history_messages.append(HumanMessage(content=msg_dict.get("content", "")))
        elif msg_dict.get("type") == "ai":
            current_history_messages.append(AIMessage(content=msg_dict.get("content", "")))
    # -----------------------------

    logging.debug(f"Sending history to chain ({len(current_history_messages)} messages)")

    # --- ENHANCED DEBUGGING & ERROR HANDLING ---
    rag_result = None  # Initialize to None
    logging.debug("Calling _ask_blocking...")
    try:
        rag_result = _ask_blocking(q, current_history_messages)  # Invoke the RAG chain
        logging.debug(f"_ask_blocking returned type: {type(rag_result)}")
        # Log first 100 chars if it's a dict, otherwise log the whole thing
        if isinstance(rag_result, dict):
            logging.debug(f"_ask_blocking returned value (repr): {repr(rag_result)[:200]}...")
        else:
            logging.debug(f"_ask_blocking returned value: {rag_result}")

        # --- ADD EXPLICIT NONE CHECK ---
        if rag_result is None:
            logging.error("_ask_blocking unexpectedly returned None!")
            # Handle the None case gracefully
            rag_result = {"answer": "Error: Failed to get response from RAG chain.", "context": []}
    except Exception as e:
        # This catches errors raised by run_async (like Timeout) that _ask_blocking might not catch
        logging.error(f"Exception caught directly in api_chat after calling _ask_blocking: {e}", exc_info=True)
        rag_result = {"answer": f"Error: An unexpected error occurred ({type(e).__name__}).", "context": []}
    # --- END ENHANCED DEBUGGING & ERROR HANDLING ---

    resp_answer = rag_result.get("answer", "Error: No answer found (processing error).")
    source_docs: List[Document] = rag_result.get("context", [])

    # --- ADD DEBUG LOGGING FOR SOURCE DOC 0 ---
    if source_docs:
        logging.debug(f"--- Debugging source_docs[0] ---")
        try:
            logging.debug(f"Type: {type(source_docs[0])}")
            logging.debug(f"Metadata: {pprint.pformat(source_docs[0].metadata)}")
            logging.debug(f"Content Preview: {repr(source_docs[0].page_content[:200])}...")
        except Exception as log_err:
            logging.error(f"Error logging source_docs[0]: {log_err}")
        logging.debug(f"--- End Debugging source_docs[0] ---")
    else:
        logging.debug("source_docs list is empty.")
    # --- END DEBUG LOGGING ---

    # --- Update Session History ---
    try:
        if not resp_answer.startswith("Error:"):
            # Append new messages as dicts for JSON serialization
            current_history_dicts.append({"type": "human", "content": q})
            current_history_dicts.append({"type": "ai", "content": resp_answer})

            # Trim history (dicts) if it gets too long
            history_limit = MAX_HISTORY_TURNS * 2
            if len(current_history_dicts) > history_limit:
                current_history_dicts = current_history_dicts[-history_limit:]

            # Save updated list of dicts back to session
            session['chat_history'] = current_history_dicts
            session.modified = True # Important to mark session as modified
            logging.debug(f"Session history updated. Size: {len(session['chat_history'])} entries")
        else:
            logging.warning("Skipping history update due to RAG error.")
    except Exception as hist_err:
        logging.error(f"Failed to update session chat history: {hist_err}")
    # --- End History Handling ---

    logging.debug(f"RAG Result: Answer='{resp_answer[:100]}...', Sources Used={len(source_docs)}")

    # --- NEW: Extract client(s) mentioned in the answer for targeted search ---
    mentioned_clients = []
    answer_lower = resp_answer.lower()
    known_clients = ["cdw", "lenovo", "mouser", "crate & barrel", "northwell health", "new pig",
                    "red hat", "vegas.com", "oriflame", "regeneron", "mintel", "us census",
                    "exxon", "honda", "footlocker", "itau", "morgan stanley", "thermo fisher",
                    "zola", "lululemon", "llbean", "costco", "fidelity", "northwestern mutual",
                    "american airlines", "t mobile", "toyota", "iqvia", "the hartford",
                    "blue cross", "te connectivity", "loyalty one", "restoration hardware",
                    "td ameritrade", "siemens", 
                    # B2C clients
                    "crate and barrel", "apparel", "activewear", "restoring hardware",
                    "footlocker", "walmart", "target", "best buy", "home depot", "ikea", 
                    "williams sonoma", "macys", "nordstrom", "lowes", "gap", "old navy", 
                    "b2c", "retail", "consumer", "ecommerce"]
    for client in known_clients:
        if client.lower() in answer_lower:
            mentioned_clients.append(client)
    target_client = mentioned_clients[0].lower() if mentioned_clients else None
    logging.debug(f"Clients mentioned in answer: {mentioned_clients}")

    # --- Post-processing for main recommendation and other sources ---
    main_recommendation = None
    processed_sources = []
    extracted_info_list = [extract_source_info(doc) for doc in source_docs]
    seen_files = set()

    # If a specific client is mentioned, do targeted search first
    targeted_docs = []
    if target_client and not resp_answer.startswith("Error:"):
        try:
            logging.info(f"Client '{target_client}' mentioned in answer. Attempting targeted search...")
            targeted_docs = run_async(_targeted_search(target_client))
            if targeted_docs:
                # Check if we found anything good (e.g., has the client name in filename)
                best_targeted_doc = None
                for doc in targeted_docs:
                    doc_info = extract_source_info(doc)
                    if not doc_info or not doc_info.get('filename'):
                        continue

                    filename_lower = doc_info['filename'].lower()
                    doc_type = doc.metadata.get('document_type', 'unknown')

                    # Check for documents with client in filename
                    if target_client in filename_lower and doc_type in ['win_report', 'case_study']:
                        if "win report" in filename_lower or doc_type == 'win_report':
                            # Prefer win reports with client in name (highest priority)
                            best_targeted_doc = doc
                            logging.info(f"Found targeted win report with client in filename: {filename_lower}")
                            break
                        elif not best_targeted_doc:
                            # Use this as backup
                            best_targeted_doc = doc
                            logging.info(f"Found targeted document with client in filename: {filename_lower}")

                # If client name search didn't find a good doc with name in filename, use the first one
                if not best_targeted_doc and targeted_docs:
                    best_targeted_doc = targeted_docs[0]
                    doc_info = extract_source_info(best_targeted_doc)
                    if doc_info and doc_info.get('filename'):
                        logging.info(f"Using first targeted result: {doc_info['filename']}")
                    else:
                        logging.warning("Could not extract info from first targeted result")
                        best_targeted_doc = None

                # Set the main recommendation from our targeted search if successful
                if best_targeted_doc:
                    doc_info = extract_source_info(best_targeted_doc)
                    if doc_info and doc_info.get('filename'):
                        main_recommendation = {
                            "filename": doc_info['filename'],
                            "link": doc_info['link'],
                            "preview_info": doc_info.get('content', '')[:200].strip() + "..."
                        }
                        seen_files.add(doc_info['filename'])
                        logging.info(f"Main recommendation set from targeted search: {doc_info['filename']}")
        except Exception as e:
            logging.error(f"Error during targeted search processing: {e}", exc_info=True)

    # Fallback to standard main recommendation if targeted search didn't work
    if not main_recommendation and source_docs and not resp_answer.startswith("Error:"):
        # Find the best match from the original RAG results
        best_match_idx = -1
        best_match_score = 4  # Start with worst score

        for i, doc_info in enumerate(extracted_info_list):
            if not doc_info or not doc_info.get('filename'):
                continue

            filename_lower = doc_info['filename'].lower()
            doc_type = source_docs[i].metadata.get('document_type', 'unknown')
            content_preview_lower = doc_info.get('content', '')[:500].lower()

            current_score = 4  # Default score

            client_in_filename = target_client and target_client in filename_lower
            client_in_content = target_client and target_client in content_preview_lower
            is_preferred_type = doc_type in ['case_study', 'win_report']

            if client_in_filename and is_preferred_type:
                current_score = 0  # Best: Client in filename, type is good
            elif client_in_content and is_preferred_type:
                current_score = 1  # Good: Client in content, type is good
            elif client_in_filename and not is_preferred_type:
                current_score = 2  # Okay: Client in filename, but type is off
            elif client_in_content and not is_preferred_type:
                current_score = 3  # Okay-ish: Client in content, but type is off

            logging.debug(f"  Scoring Doc {i}: Name='{doc_info['filename']}', Type='{doc_type}', ClientInName={client_in_filename}, ClientInContent={client_in_content}, PrefType={is_preferred_type} -> Score={current_score}")

            if current_score < best_match_score:
                best_match_score = current_score
                best_match_idx = i
                logging.debug(f"  New best match found: Index={i}, Score={current_score}")
                if best_match_score == 0:
                    break  # Optimization: If we found score 0, we likely won't do better

        # Use the best match from original results as main recommendation
        if best_match_idx != -1:
            best_match_doc_info = extracted_info_list[best_match_idx]
            if best_match_doc_info:
                main_recommendation = {
                    "filename": best_match_doc_info['filename'],
                    "link": best_match_doc_info['link'],
                    "preview_info": best_match_doc_info.get('content', '')[:200].strip() + "..."
                }
                seen_files.add(best_match_doc_info['filename'])
                logging.info(f"Main recommendation set from original results: {best_match_doc_info['filename']}")

        # Fallback to first document if needed
        if not main_recommendation and source_docs:
            logging.info("Falling back to first document for main recommendation.")
            fallback_info = extracted_info_list[0]
            if fallback_info and fallback_info.get('filename') and fallback_info['filename'] != "Unknown Source":
                main_recommendation = {
                    "filename": fallback_info['filename'],
                    "link": fallback_info['link'],
                    "preview_info": fallback_info.get('content', '')[:200].strip() + "..."
                }
                seen_files.add(fallback_info['filename'])
                logging.info(f"Main recommendation set from first doc (fallback): {fallback_info['filename']}")

    # Populate processed_sources with remaining docs from original search
    processed_sources = []
    for i, doc_info in enumerate(extracted_info_list):
        if not doc_info or not doc_info.get('filename') or doc_info['filename'] in seen_files or doc_info['filename'] == "Unknown Source":
            continue
        processed_sources.append({"filename": doc_info['filename'], "link": doc_info['link']})
        seen_files.add(doc_info['filename'])

        # Limit to reasonable number
        if len(processed_sources) >= 6:
            break

    # Format response HTML with proper HTML escaping
    logging.info(f"Query Processed. AnsLen={len(resp_answer)}. MainRec={main_recommendation is not None}. OtherSrc={len(processed_sources)}")
    final_html = html.escape(resp_answer).replace("\n", "<br>")

    # Add notes for no sources or processing failures only to the main answer (with HTML escaping)
    if not resp_answer.startswith("Error:") and not main_recommendation and not source_docs:
         final_html += "<br><i>(No specific sources identified for this answer)</i>"
    elif not resp_answer.startswith("Error:") and not main_recommendation and source_docs:
         final_html += "<br><i>(Could not process primary source details)</i>"

    # Log the query after processing sources
    log_query(q, rag_result, main_recommendation, processed_sources)
    end_time = time.time(); logging.info(f"Request processed in {end_time - start_time:.2f}s.")
    logging.debug(f"--> Returning Response: answer_html='{final_html[:150]}...', main_recommendation={pprint.pformat(main_recommendation)}, other_sources_count={len(processed_sources)}")

    # Modified return to include other_sources separately
    return jsonify(
        answer_html=final_html, 
        main_recommendation=main_recommendation,
        other_sources=processed_sources
    )

# --- BOOTSTRAP ---
def initialize_app():
    global llm_instance, embeddings_model, vector_store, rag_chain
    print("🚀 DocBot starting initialization...")
    logging.info("--- Application Starting Up ---")
    start_loop_thread()

    # Check for required environment variables
    if not os.getenv("OPENAI_API_KEY"):
        logging.critical("OPENAI_API_KEY environment variable not set.")
        sys.exit(1)

    if not GCS_BUCKET_NAME or not GCS_INDEX_PATH:
        logging.critical("GCS_BUCKET_NAME or GCS_INDEX_PATH environment variables not set.")
        sys.exit(1)

    # 1. Initialize LLM and Embeddings
    print(f"Initializing LLM ({LLM_MODEL_NAME})...")
    try:
        llm_instance = ChatOpenAI(temperature=LLM_TEMPERATURE, model_name=LLM_MODEL_NAME)
        print("LLM interface initialized successfully.")
        embeddings_model = OpenAIEmbeddings(model=EMBEDDING_MODEL_NAME)
        print(f"Embeddings model ({EMBEDDING_MODEL_NAME}) initialized successfully.")
    except Exception as e: 
        logging.critical(f"LLM/Embedding init failed: {e}", exc_info=True)
        sys.exit(1)

    # 2. Load Vector Store from GCS
    try:
        vector_store = load_vectorstore() # Downloads from GCS
        if not vector_store:
            raise RuntimeError("Failed to load vector store from GCS.")
        print(f"Vector store loaded successfully from GCS.")

        # --- START DEBUG BLOCK: Inspect 'document_type' metadata ---
        print("DEBUG: Inspecting vector store metadata...")
        logging.info("DEBUG: Inspecting vector store metadata...") # Also log
        try:
            # Use vector_store.get() to retrieve metadata without embeddings/documents
            # May retrieve all documents if no limit specified and store is large - use limit if needed
            # Adjust limit=None if your store is very large and this takes too long
            retrieved_data = vector_store.get(include=["metadatas"], limit=None)
            metadatas_list = retrieved_data.get('metadatas', [])

            if not metadatas_list:
                print("DEBUG: No metadata found in the vector store.")
                logging.warning("DEBUG: No metadata found in the vector store.")
            else:
                print(f"DEBUG: Found {len(metadatas_list)} metadata entries. Checking 'document_type'...")
                logging.info(f"DEBUG: Found {len(metadatas_list)} metadata entries. Checking 'document_type'...")
                unique_doc_types = set()
                none_count = 0
                key_missing_count = 0

                for meta_dict in metadatas_list:
                    if meta_dict is None: # Handle case where metadata itself is None
                        none_count +=1
                        continue
                    doc_type_value = meta_dict.get('document_type') # Use .get() for safety
                    if doc_type_value is None:
                         # Check if the key was missing or the value was actually None
                         if 'document_type' not in meta_dict:
                             key_missing_count += 1
                         else:
                             none_count += 1 # Value was explicitly None
                    # Add whatever value was found (including None) to the set
                    unique_doc_types.add(doc_type_value)

                print(f"DEBUG: Unique 'document_type' values found in index: {unique_doc_types}")
                logging.info(f"DEBUG: Unique 'document_type' values found in index: {unique_doc_types}")
                if none_count > 0:
                     print(f"DEBUG: Note - {none_count} entries had 'document_type' explicitly set to None.")
                     logging.info(f"DEBUG: Note - {none_count} entries had 'document_type' explicitly set to None.")
                if key_missing_count > 0:
                     print(f"DEBUG: Note - {key_missing_count} entries were missing the 'document_type' key.")
                     logging.info(f"DEBUG: Note - {key_missing_count} entries were missing the 'document_type' key.")

        except Exception as e:
            print(f"DEBUG: Error during metadata inspection: {e}")
            logging.error(f"DEBUG: Error during metadata inspection: {e}", exc_info=True)
        print("DEBUG: Finished inspecting vector store metadata.")
        logging.info("DEBUG: Finished inspecting vector store metadata.")
        # --- END DEBUG BLOCK ---
    except Exception as e:
        logging.critical(f"Vector store initialization error: {e}", exc_info=True)
        sys.exit(1)

    # 3. Define Metadata for Self-Query
    metadata_field_info = [
        AttributeInfo(
            name="original_file_name",
            description="The original filename of the source document, including the extension (e.g., 'case_study_client_x.pdf', 'pitch_deck_v3.pptx'). Use this for filtering based on filename.",
            type="string",
        ),
        AttributeInfo(
            name="document_type",
            description="The category derived from the document's parent folder name or filename keywords (e.g., 'case_study', 'win_report', 'pitch_deck', 'discovery_guide', 'report', 'presentation', 'unknown'). Both 'case_study' and 'win_report' can contain customer success information and may be relevant when asked for a case study.",
            type="string",
        ),
        AttributeInfo(
            name="source_gcs_path",
            description="The full GCS path to the source document.",
            type="string",
        ),
    ]
    document_content_description = "Various business documents including case studies, reports, presentations, pitch decks, and discovery guides."
    print("Metadata fields defined for Self-Query.")

    # 4. Create Self-Query Retriever
    try:
        print("Creating Self-Query Retriever...")
        retriever = SelfQueryRetriever.from_llm(
            llm=llm_instance,
            vectorstore=vector_store,
            document_contents=document_content_description,
            metadata_field_info=metadata_field_info,
            search_kwargs={"k": RETRIEVAL_TOP_K},
            verbose=True,
            enable_limit=True,
            structured_query_translator=ChromaTranslator()
        )
        print("Self-Query Retriever created.")

        # Re-introduce Re-ranking
        if not os.getenv("COHERE_API_KEY"):
            print("Warning: COHERE_API_KEY not set. Skipping Re-ranking.")
            final_retriever = retriever
        else:
            try:
                print("Creating Cohere Re-ranker...")
                compressor = CohereRerank(top_n=5, model="rerank-english-v3.0")
                compression_retriever = ContextualCompressionRetriever(
                    base_compressor=compressor,
                    base_retriever=retriever
                )
                print("Contextual Compression Retriever with Cohere Re-ranker created.")
                final_retriever = compression_retriever
            except Exception as rerank_err:
                logging.error(f"Failed to create Cohere ReRanker: {rerank_err}. Falling back to non-reranked retriever.")
                final_retriever = retriever

        # 5. Define QA Prompt (includes chat_history placeholder)
        print("Defining main QA prompt...")
        qa_system_prompt = (
            "You are a helpful assistant answering questions based ONLY on the following context provided. "
            "If the context doesn't contain the answer, say you don't have enough information. "
            "Be concise and refer to the information found in the context. Do not make up information.\n\n"
            "Context:\n{context}"
        )
        qa_prompt = ChatPromptTemplate.from_messages(
            [
                ("system", qa_system_prompt),
                MessagesPlaceholder("chat_history"),
                ("human", "{input}"),
            ]
        )
        print("QA prompt template defined.")

        # 6. Create QA Chain (stuffs docs into prompt)
        doc_qa_chain = create_stuff_documents_chain(llm_instance, qa_prompt)
        print("Question answering chain created.")

        # 7. Define the Full RAG Chain
        print("Building final RAG chain...")

        # Function to prepare input for the QA chain (mapping keys)
        def map_to_qa_input(info: Dict):
            return {
                "input": info["question"],
                "chat_history": info["chat_history"],
                "context": info["context_docs"]
            }

        rag_chain_with_sources = RunnableParallel(
            {"context_docs": itemgetter("input") | final_retriever,
            "question": itemgetter("input"),
            "chat_history": itemgetter("chat_history")}
        ).assign(answer=RunnableLambda(map_to_qa_input) | doc_qa_chain)

        # Select only 'answer' and 'context' for the final output dict
        final_rag_chain = rag_chain_with_sources | RunnableLambda(
            lambda x: {"answer": x["answer"], "context": x["context_docs"]}
        )

        # Assign the final chain to the global variable
        rag_chain = final_rag_chain
        print("Full RAG chain with Self-Query, Re-ranking, and history awareness created.")

    except Exception as e:
        logging.critical(f"Failed to create retriever or RAG chain: {e}", exc_info=True)
        rag_chain = None
        sys.exit(1)

    print("Initialization complete.")
    logging.info("--- Application Initialization Complete ---")


if __name__ == "__main__":
    try:
        initialize_app()
    except Exception as e:
        logging.critical(f"Initialization failed: {e}")
        print(f"[SERVER] ERROR: App initialization failed: {e}")
        sys.exit(1)

    print("[SERVER] Launching server...")
    port = int(os.getenv("PORT", "8080"))
    host = "0.0.0.0"  # Always use 0.0.0.0 to ensure external accessibility
    print(f"[SERVER] Host: {host} Port: {port}")
    print(f"[SERVER] Local: http://127.0.0.1:{port}")

    # Use waitress for production-ready serving
    serve(app, host=host, port=port, threads=8)