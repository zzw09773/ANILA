# !/bin/bash

# Load environment variables from .env file
# if [ -f .env ]; then
#     export $(grep -v '^#' .env | xargs)
# fi
export WORKSPACE=/workspace
 export NCCL_DEBUG=WARN
# Run the Docker container with the specified configurations
docker run -itd --rm --gpus '"device=2"' \
    --name tensorrtllm-gpt-oss-20b \
    -v ${PWD}/../../Huggingface/gpt-oss-20b:${WORKSPACE}/model/gpt-oss-20b \
    -v ${PWD}:${WORKSPACE}/src \
    -p 8000:8000 \
    -w $WORKSPACE \
    -e NCCL_DEBUG=WARN \
    --ipc=host \
    --ulimit memlock=-1 \
    --ulimit stack=67108864 \
    --env HF_HUB_OFFLINE=1 \
    --env TRANSFORMERS_OFFLINE=1 \
    --env HF_DATASETS_OFFLINE=1 \
    tensorrt-llm-hf:1.3.0rc6 \
    trtllm-serve ${WORKSPACE}/model/gpt-oss-20b \
        --extra_llm_api_options ${WORKSPACE}/src/gpt-oss-20b-throughput.yaml \
        --host 0.0.0.0 \
        --port 8000