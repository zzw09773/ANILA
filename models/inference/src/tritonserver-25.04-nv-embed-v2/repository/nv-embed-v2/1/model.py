import sys
import os
import json
import numpy as np
import glob
import triton_python_backend_utils as pb_utils
import torch
import torch.nn.functional as F
from transformers import AutoModel, AutoTokenizer


def average_pool(last_hidden_states: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    """Average pooling with attention mask."""

    last_hidden_states = last_hidden_states.to(torch.float32)
    last_hidden_states_masked = last_hidden_states.masked_fill(
        ~attention_mask[..., None].bool(), 0.0)
    embedding = last_hidden_states_masked.sum(
        dim=1) / attention_mask.sum(dim=1)[..., None]
    embedding = F.normalize(embedding, dim=-1)

    return embedding

# Define task and queries


def get_instruction(task_instruction: str, query: str) -> str:
    return f"Instruct: {task_instruction}\nQuery: {query}"


class TritonPythonModel:

    def initialize(self, args):
        """
        This function allows the model to initialize any state associated with this model.

        Parameters
        ----------
        args : dict
          Both keys and values are strings. The dictionary keys and values are:
          * model_config: A JSON string containing the model configuration
          * model_instance_kind: A string containing model instance kind
          * model_instance_device_id: A string containing model instance device ID
          * model_repository: Model repository path
          * model_version: Model version
          * model_name: Model name
        """
        # Parse model configs
        model_config = json.loads(args["model_config"])
        model_path = model_config["parameters"]["model_path"]["string_value"]
        model_device = model_config["parameters"]["device"]["string_value"]

        # Load model on specified device directly
        print(f"Initializing NV-Embed-v2 model directly on device: {model_device}")
        self.model = AutoModel.from_pretrained(
            model_path,
            trust_remote_code=True,
            torch_dtype=torch.float16,
            device_map=model_device if torch.cuda.is_available() else "cpu"
        )
        self.model.eval()

    def execute(self, requests):
        """
        This function is called when an inference is requested for this model. 

        Parameters
        ----------
        requests : list
          A list of pb_utils.InferenceRequest

        Returns
        -------
        list
          A list of pb_utils.InferenceResponse. The length of this list must
          be the same as `requests`
        """

        responses = []

        # Every Python backend must iterate over everyone of the requests
        # and create a pb_utils.InferenceResponse for each of them.
        for idx, request in enumerate(requests):
            try:
                # Get query as a string (dims: [1])
                # With max_batch_size: 0, no batch dimension is added by Triton
                query = pb_utils.get_input_tensor_by_name(request, "query")
                if query is not None:
                    query_array = query.as_numpy()

                    # Extract the string from shape (1,)
                    query = query_array[0].decode("utf8") if isinstance(
                        query_array[0], bytes) else str(query_array[0])

                    # Get embedding — use max_length=512 to match LlamaIndex chunk size
                    # and minimize VRAM usage from eager attention matrices
                    query_prefix = (
                        f"Instruct: Given a question, retrieve passages that answer the question\n"
                        f"Query: {query}\n"
                    )
                    with torch.inference_mode():
                        embeddings = self.model.encode(
                            [query],
                            instruction=query_prefix,
                            max_length=8192
                        )
                    # Aggressively free PyTorch cached memory after each encode
                    torch.cuda.empty_cache()

                 # Get documents as a list of strings (dims: [1, -1])
                documents = pb_utils.get_input_tensor_by_name(
                    request, "documents")
                if documents is not None:
                    documents_array = documents.as_numpy()
                    # For dims [1, -1]: if shape is (1, n), extract from first dimension
                    # For dims [-1]: if shape is (n,), use directly
                    if documents_array.ndim == 2:
                        # Shape: (1, n) - extract strings from first row
                        input_texts = [doc.decode("utf8") if isinstance(doc, bytes) else str(doc)
                                       for doc in documents_array[0]]
                    else:
                        # Shape: (n,) - use directly
                        input_texts = [doc.decode("utf8") if isinstance(doc, bytes) else str(doc)
                                       for doc in documents_array]
                    
                    # Process one document at a time to prevent PyTorch CUDA OOM
                    all_embeddings = []
                    for i in range(len(input_texts)):
                        with torch.inference_mode():
                            sub_emb = self.model.encode(
                                [input_texts[i]],
                                instruction="",
                                max_length=8192
                            )
                        all_embeddings.append(sub_emb)
                        # Free intermediate GPU memory after each encode
                        torch.cuda.empty_cache()
                    
                    if isinstance(all_embeddings[0], torch.Tensor):
                        embeddings = torch.cat(all_embeddings, dim=0)
                    else:
                        embeddings = np.concatenate(all_embeddings, axis=0)

                # normalize embeddings
                embeddings = F.normalize(embeddings, p=2, dim=1)

                # Convert to numpy if it's a torch tensor and ensure it's on CPU
                if isinstance(embeddings, torch.Tensor):
                    embeddings_np = embeddings.detach().cpu().numpy()
                    del embeddings
                    torch.cuda.empty_cache()
                    embeddings = embeddings_np

                # Ensure embeddings is 2D with shape (1, embedding_dim) for output dims: [1, -1]
                if embeddings.ndim == 1:
                    embeddings = embeddings.reshape(1, -1)

                # Prepare results - output shape should be (1, embedding_dim)
                context_output = pb_utils.Tensor(
                    "embeddings", embeddings.astype(np.float32)
                )
                inference_response = pb_utils.InferenceResponse(
                    output_tensors=[context_output]
                )
                responses.append(inference_response)

            except Exception as error:
                print(sys.exc_info()[2])
                # Clean up GPU memory even on error
                torch.cuda.empty_cache()
                responses.append(pb_utils.InferenceResponse(output_tensors=[],
                                                            error=pb_utils.TritonError(error)))

        # You should return a list of pb_utils.InferenceResponse. Length
        # of this list must match the length of `requests` list.
        return responses

    def finalize(self):
        """
        This function allows the model to perform any necessary clean ups before exit.
        """
        print("Cleaning up...")
