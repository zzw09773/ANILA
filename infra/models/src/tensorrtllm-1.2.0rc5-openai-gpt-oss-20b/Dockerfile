# FROM nvcr.io/nvidia/tensorrt-llm/release:1.1.0rc5
FROM nvcr.io/nvidia/tensorrt-llm/release:1.3.0rc4
# Build-layer DCS cleanup; rationale and same-RUN rule: services/asr-decoder/Dockerfile.
RUN pip install --upgrade \
        nvidia-modelopt[hf] && \
    rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid
