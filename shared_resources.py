from misc_utility_functions import  is_redis_running, start_redis_server, build_faiss_indexes
from database_functions import DatabaseWriter, initialize_db, AsyncSessionLocal, delete_expired_rows 
from ramdisk_functions import setup_ramdisk, copy_models_to_ramdisk, check_that_user_has_required_permissions_to_manage_ramdisks
from logger_config import setup_logger
from aioredlock import Aioredlock
import aioredis
import asyncio
import urllib.request
import os
import glob
import json
from filelock import FileLock, Timeout
import traceback
import llama_cpp
from typing import List, Tuple, Dict
from decouple import config
from fastapi import HTTPException
from apscheduler.schedulers.asyncio import AsyncIOScheduler
try:
    import nvgpu
    GPU_AVAILABLE = True
except ImportError:
    GPU_AVAILABLE = False
logger = setup_logger()

embedding_model_cache = {} # Model cache to store loaded models
text_completion_model_cache = {} # Model cache to store loaded text completion models

SWISS_ARMY_LLAMA_SERVER_LISTEN_PORT = config("SWISS_ARMY_LLAMA_SERVER_LISTEN_PORT", default=8089, cast=int)
DEFAULT_MODEL_NAME = config("DEFAULT_MODEL_NAME", default="openchat_v3.2_super", cast=str) 
LLM_CONTEXT_SIZE_IN_TOKENS = config("LLM_CONTEXT_SIZE_IN_TOKENS", default=512, cast=int)
TEXT_COMPLETION_CONTEXT_SIZE_IN_TOKENS = config("TEXT_COMPLETION_CONTEXT_SIZE_IN_TOKENS", default=4000, cast=int)
DEFAULT_MAX_COMPLETION_TOKENS = config("DEFAULT_MAX_COMPLETION_TOKENS", default=100, cast=int)
DEFAULT_NUMBER_OF_COMPLETIONS_TO_GENERATE = config("DEFAULT_NUMBER_OF_COMPLETIONS_TO_GENERATE", default=4, cast=int)
DEFAULT_COMPLETION_TEMPERATURE = config("DEFAULT_COMPLETION_TEMPERATURE", default=0.7, cast=float)
MINIMUM_STRING_LENGTH_FOR_DOCUMENT_EMBEDDING = config("MINIMUM_STRING_LENGTH_FOR_DOCUMENT_EMBEDDING", default=15, cast=int)
USE_PARALLEL_INFERENCE_QUEUE = config("USE_PARALLEL_INFERENCE_QUEUE", default=False, cast=bool)
MAX_CONCURRENT_PARALLEL_INFERENCE_TASKS = config("MAX_CONCURRENT_PARALLEL_INFERENCE_TASKS", default=10, cast=int)
USE_RAMDISK = config("USE_RAMDISK", default=False, cast=bool)
USE_VERBOSE = config("USE_VERBOSE", default=False, cast=bool)
RAMDISK_PATH = config("RAMDISK_PATH", default="/mnt/ramdisk", cast=str)
BASE_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
USE_AUTOMATIC_PURGING_OF_EXPIRED_RECORDS = config("USE_AUTOMATIC_PURGING_OF_EXPIRED_RECORDS", default=1, cast=bool)

if USE_AUTOMATIC_PURGING_OF_EXPIRED_RECORDS:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(delete_expired_rows(AsyncSessionLocal), 'interval', hours=1)
    scheduler.start()

def is_gpu_available():
    if not GPU_AVAILABLE:
        return {
            "gpu_found": False,
            "num_gpus": 0,
            "first_gpu_vram": 0,
            "total_vram": 0,
            "error": "nvgpu module not found"
        }
    try:
        gpu_info = nvgpu.gpu_info()
        num_gpus = len(gpu_info)
        if num_gpus == 0:
            return {
                "gpu_found": False,
                "num_gpus": 0,
                "first_gpu_vram": 0,
                "total_vram": 0
            }
        first_gpu_vram = gpu_info[0]['mem_total']
        total_vram = sum(gpu['mem_total'] for gpu in gpu_info)
        return {
            "gpu_found": True,
            "num_gpus": num_gpus,
            "first_gpu_vram": first_gpu_vram,
            "total_vram": total_vram,
            "gpu_info": gpu_info
        }
    except Exception as e:
        return {
            "gpu_found": False,
            "num_gpus": 0,
            "first_gpu_vram": 0,
            "total_vram": 0,
            "error": str(e)
        }
        
async def initialize_globals():
    global db_writer, faiss_indexes, associated_texts_by_model_and_pooling_method, redis, lock_manager
    if not is_redis_running():
        logger.info("Starting Redis server...")
        start_redis_server()
        await asyncio.sleep(1)  # Sleep for 1 second to give Redis time to start
    redis = await aioredis.create_redis_pool('redis://localhost')
    lock_manager = Aioredlock([redis])
    lock_manager.default_lock_timeout = 20000  # Lock timeout in milliseconds (20 seconds)
    lock_manager.retry_count = 5  # Number of retries
    lock_manager.retry_delay_min = 100  # Minimum delay between retries in milliseconds
    lock_manager.retry_delay_max = 1000  # Maximum delay between retries in milliseconds
    await initialize_db()
    queue = asyncio.Queue()
    db_writer = DatabaseWriter(queue)
    await db_writer.initialize_processing_hashes()
    asyncio.create_task(db_writer.dedicated_db_writer())
    global USE_RAMDISK
    if USE_RAMDISK and not check_that_user_has_required_permissions_to_manage_ramdisks():
        USE_RAMDISK = False
    elif USE_RAMDISK:
        setup_ramdisk()
    list_of_downloaded_model_names, download_status = download_models()
    faiss_indexes, associated_texts_by_model_and_pooling_method = await build_faiss_indexes()


# other shared variables and methods
db_writer = None
faiss_indexes = None
associated_texts_by_model_and_pooling_method = None
redis = None
lock_manager = None

def download_models() -> Tuple[List[str], List[Dict[str, str]]]:
    download_status = []    
    json_path = os.path.join(BASE_DIRECTORY, "model_urls.json")
    if not os.path.exists(json_path):
        initial_model_urls = [
            "https://huggingface.co/NousResearch/Hermes-2-Theta-Llama-3-8B-GGUF/resolve/main/Hermes-2-Pro-Llama-3-Instruct-Merged-DPO-Q4_K_M.gguf",
            "https://huggingface.co/QuantFactory/Meta-Llama-3-8B-Instruct-GGUF/resolve/main/Meta-Llama-3-8B-Instruct.Q3_K_S.gguf",
            "https://huggingface.co/vonjack/bge-m3-gguf/resolve/main/bge-m3-q8_0.gguf"
        ]
        with open(json_path, "w") as f:
            json.dump(initial_model_urls, f)
    with open(json_path, "r") as f:
        list_of_model_download_urls = json.load(f)
    model_names = [os.path.basename(url) for url in list_of_model_download_urls]
    current_file_path = os.path.abspath(__file__)
    base_dir = os.path.dirname(current_file_path)
    models_dir = os.path.join(base_dir, 'models')
    logger.info("Checking models directory...")
    if USE_RAMDISK:
        ramdisk_models_dir = os.path.join(RAMDISK_PATH, 'models')
        if not os.path.exists(RAMDISK_PATH):
            setup_ramdisk()
        if all(os.path.exists(os.path.join(ramdisk_models_dir, llm_model_name)) for llm_model_name in model_names):
            logger.info("Models found in RAM Disk.")
            for url in list_of_model_download_urls:
                download_status.append({"url": url, "status": "success", "message": "Model found in RAM Disk."})
            return model_names, download_status
    if not os.path.exists(models_dir):
        os.makedirs(models_dir)
        logger.info(f"Created models directory: {models_dir}")
    else:
        logger.info(f"Models directory exists: {models_dir}")
    lock = FileLock(os.path.join(models_dir, "download.lock"))
    for url, model_name_with_extension in zip(list_of_model_download_urls, model_names):
        status = {"url": url, "status": "success", "message": "File already exists."}
        filename = os.path.join(models_dir, model_name_with_extension)
        try:
            with lock.acquire(timeout=1200): # Wait up to 20 minutes for the file to be downloaded before returning failure
                if not os.path.exists(filename):
                    logger.info(f"Downloading model {model_name_with_extension} from {url}...")
                    urllib.request.urlretrieve(url, filename)
                    file_size = os.path.getsize(filename) / (1024 * 1024)  # Convert bytes to MB
                    if file_size < 100:
                        os.remove(filename)
                        status["status"] = "failure"
                        status["message"] = "Downloaded file is too small, probably not a valid model file."
                    else:
                        logger.info(f"Downloaded: {filename}")
                else:
                    logger.info(f"File already exists: {filename}")
        except Timeout:
            logger.warning(f"Could not acquire lock for downloading {model_name_with_extension}")
            status["status"] = "failure"
            status["message"] = "Could not acquire lock for downloading."
        download_status.append(status)
    if USE_RAMDISK:
        copy_models_to_ramdisk(models_dir, ramdisk_models_dir)
    logger.info("Model downloads completed.")
    return model_names, download_status

def load_model(llm_model_name: str, raise_http_exception: bool = True):
    global USE_VERBOSE
    model_instance = None
    try:
        models_dir = os.path.join(RAMDISK_PATH, 'models') if USE_RAMDISK else os.path.join(BASE_DIRECTORY, 'models')
        if llm_model_name in embedding_model_cache:
            return embedding_model_cache[llm_model_name]
        matching_files = glob.glob(os.path.join(models_dir, f"{llm_model_name}*"))
        if not matching_files:
            logger.error(f"No model file found matching: {llm_model_name}")
            raise FileNotFoundError
        matching_files.sort(key=os.path.getmtime, reverse=True)
        model_file_path = matching_files[0]
        gpu_info = is_gpu_available()
        if 'llava' in llm_model_name:
            is_llava_multimodal_model = 1
        else:
            is_llava_multimodal_model = 0
        if not is_llava_multimodal_model:
            if gpu_info['gpu_found']:
                try:
                    model_instance = llama_cpp.Llama(model_path=model_file_path, embedding=True, n_ctx=LLM_CONTEXT_SIZE_IN_TOKENS, verbose=USE_VERBOSE, n_gpu_layers=-1) # Load the model with GPU acceleration
                except Exception as e:  # noqa: F841
                    model_instance = llama_cpp.Llama(model_path=model_file_path, embedding=True, n_ctx=LLM_CONTEXT_SIZE_IN_TOKENS, verbose=USE_VERBOSE)
            else:
                model_instance = llama_cpp.Llama(model_path=model_file_path, embedding=True, n_ctx=LLM_CONTEXT_SIZE_IN_TOKENS, verbose=USE_VERBOSE) # Load the model without GPU acceleration        
            embedding_model_cache[llm_model_name] = model_instance
        return model_instance
    except TypeError as e:
        logger.error(f"TypeError occurred while loading the model: {e}")
        raise
    except Exception as e:
        logger.error(f"Exception occurred while loading the model: {e}")
        traceback.print_exc()
        if raise_http_exception:
            raise HTTPException(status_code=404, detail="Model file not found")
        else:
            raise FileNotFoundError(f"No model file found matching: {llm_model_name}")