# !/bin/bash

export WORKSPACE=/workspace

# Run the Docker container with the specified configurations
docker run -itd --rm --gpus '"device=3"'  \
    --name tritonserver-nv-embed-v2 \
    -v ${PWD}/../../Huggingface/NV-Embed-v2:${WORKSPACE}/model/NV-Embed-v2 \
    -v ${PWD}/repository:${WORKSPACE}/repository \
    -v ${PWD}:${WORKSPACE}/src \
    -p 8000:8000 \
    -p 8001:8001 \
    -p 8002:8002 \
    -w $WORKSPACE \
    tritonserver:25.04-nv-embed-v2 \
    tritonserver --model-repository=${WORKSPACE}/repository \
                 --model-control-mode=poll \
                 --repository-poll-secs=1