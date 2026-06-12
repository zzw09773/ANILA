#! /usr/bin/env bash

# Runner: measure genai-perf profile across multiple request rate settings
set -euo pipefail

REQUEST_RATES=(1 5 10 15 20)
BASE_DIR="./request_rate_results"
rm -rf "${BASE_DIR}"
mkdir -p "${BASE_DIR}"

for rate in "${REQUEST_RATES[@]}"; do
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    OUTDIR="${BASE_DIR}/rate${rate}_${TIMESTAMP}"
    mkdir -p "${OUTDIR}"

    genai-perf profile \
        -m gpt-oss-20b \
        --tokenizer /workspace/model/gpt-oss-20b \
        --endpoint-type chat \
        --random-seed 123 \
        --synthetic-input-tokens-mean 128 \
        --synthetic-input-tokens-stddev 0 \
        --output-tokens-mean 128 \
        --output-tokens-stddev 0 \
        --stability-percentage 999 \
        --request-rate "${rate}" \
        --request-count 100 \
        --artifact-dir "${OUTDIR}" \
        --url localhost:8000 \
        --extra-inputs max_tokens:256 \
        --extra-inputs temperature:0.7 \
        2>&1 | tee "${OUTDIR}/perf_output.log"

    echo "Completed request rate ${rate}, output: ${OUTDIR}"
    # brief pause to let server stabilize
    sleep 2

done

echo "All runs complete. Results in: ${BASE_DIR}"
