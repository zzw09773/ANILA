FROM nvcr.io/nvidia/tritonserver:25.04-pyt-python-py3
# Install git to clone sentence-transformers repository
RUN apt-get update && apt-get install -y git

# # Install specific versions of transformers
# RUN pip3 uninstall -y transformer-engine && \
#     pip3 install torch==2.2.0 \
#                  transformers==4.42.4

RUN pip3 install torch==2.2.0 \
    transformers==4.42.4 \
    packaging \
    datasets


# Then install flash-attn (it will compile against the installed PyTorch)
RUN pip3 install --upgrade flash-attn==2.2.0
