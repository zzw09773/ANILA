#!/usr/bin/env python
"""
Test script for NV-Embed-v2 model on Triton Inference Server.
Tests both query and document embedding generation.
"""
import sys
import os
import numpy as np
import tritonclient.grpc as grpcclient


def test_query_embedding(triton_client, model_name, query):
    """Test query embedding generation."""
    print("\n" + "="*80)
    print("TEST: Query Embedding Generation")
    print("="*80)
    print(f"Query: {query}")
    
    # Prepare input - query expects dims [1]
    input_query = grpcclient.InferInput("query", [1], "BYTES")
    
    # Create numpy array with proper dtype for string data
    query_data = np.array([query], dtype=object)
    
    print(f"Input array shape: {query_data.shape}")
    print(f"Input array dtype: {query_data.dtype}")
    
    input_query.set_data_from_numpy(query_data)
    
    # Prepare output
    output_embeddings = grpcclient.InferRequestedOutput("embeddings")
    
    # Perform inference
    print("Sending query to Triton server...")
    results = triton_client.infer(
        model_name=model_name,
        inputs=[input_query],
        outputs=[output_embeddings]
    )
    
    # Get output embeddings
    embeddings = results.as_numpy("embeddings")
    
    print(f"\n{'='*80}")
    print("QUERY EMBEDDING RESULTS:")
    print(f"{'='*80}")
    print(f"Embedding shape: {embeddings.shape}")
    print(f"Embedding dtype: {embeddings.dtype}")
    
    # Calculate norms for each embedding in the batch
    if embeddings.ndim == 1:
        norm = np.linalg.norm(embeddings)
        print(f"Embedding norm: {norm:.6f}")
        print(f"First 10 values: {embeddings[:10]}")
    else:
        for i in range(embeddings.shape[0]):
            norm = np.linalg.norm(embeddings[i])
            print(f"Embedding {i+1} norm: {norm:.6f}")
        print(f"First embedding - first 10 values: {embeddings[0][:10]}")
    
    print(f"{'='*80}")
    
    # Validate embeddings are normalized (L2 norm should be ~1.0)
    if embeddings.ndim == 1:
        norm = np.linalg.norm(embeddings)
        assert abs(norm - 1.0) < 0.01, f"Expected normalized embeddings (norm ~1.0), got {norm:.6f}"
    else:
        for i in range(embeddings.shape[0]):
            norm = np.linalg.norm(embeddings[i])
            assert abs(norm - 1.0) < 0.01, f"Expected normalized embeddings (norm ~1.0), got {norm:.6f} for embedding {i+1}"
    
    print("\n✓ Query embedding test PASSED")
    
    return embeddings


def test_document_embeddings(triton_client, model_name, documents):
    """Test document embeddings generation."""
    print("\n" + "="*80)
    print("TEST: Document Embeddings Generation")
    print("="*80)
    print(f"Number of documents: {len(documents)}")
    for i, doc in enumerate(documents, 1):
        print(f"Document {i}: {doc[:100]}{'...' if len(doc) > 100 else ''}")
    
    # Prepare input - documents expects dims [1, -1]
    input_documents = grpcclient.InferInput("documents", [1, len(documents)], "BYTES")
    
    # Create numpy array with proper dtype for string data
    # Shape should be (1, num_documents)
    documents_data = np.array([documents], dtype=object)
    
    print(f"\nInput array shape: {documents_data.shape}")
    print(f"Input array dtype: {documents_data.dtype}")
    
    input_documents.set_data_from_numpy(documents_data)
    
    # Prepare output
    output_embeddings = grpcclient.InferRequestedOutput("embeddings")
    
    # Perform inference
    print("Sending documents to Triton server...")
    results = triton_client.infer(
        model_name=model_name,
        inputs=[input_documents],
        outputs=[output_embeddings]
    )
    
    # Get output embeddings
    embeddings = results.as_numpy("embeddings")
    
    print(f"\n{'='*80}")
    print("DOCUMENT EMBEDDINGS RESULTS:")
    print(f"{'='*80}")
    print(f"Embeddings shape: {embeddings.shape}")
    print(f"Embeddings dtype: {embeddings.dtype}")
    print(f"Number of embeddings: {embeddings.shape[0]}")
    for i in range(embeddings.shape[0]):
        norm = np.linalg.norm(embeddings[i])
        print(f"Document {i+1} embedding norm: {norm:.6f}")
    print(f"{'='*80}")
    
    # Validate all embeddings are normalized (L2 norm should be ~1.0)
    for i in range(embeddings.shape[0]):
        norm = np.linalg.norm(embeddings[i])
        assert abs(norm - 1.0) < 0.01, f"Expected normalized embeddings (norm ~1.0), got {norm:.6f} for document {i+1}"
    
    print("\n✓ Document embeddings test PASSED")
    
    return embeddings


def test_similarity_search(triton_client, model_name, query, documents):
    """Test semantic similarity search using embeddings."""
    print("\n" + "="*80)
    print("TEST: Semantic Similarity Search")
    print("="*80)
    print(f"Query: {query}")
    print(f"Searching across {len(documents)} documents")
    
    # Get query embedding
    print("\nGenerating query embedding...")
    query_embedding = test_query_embedding(triton_client, model_name, query)
    
    # Get document embeddings
    print("\nGenerating document embeddings...")
    doc_embeddings = test_document_embeddings(triton_client, model_name, documents)
    
    # Calculate similarity scores (cosine similarity via dot product, since embeddings are normalized)
    print("\nCalculating similarity scores...")
    # Reshape query embedding if needed
    if query_embedding.ndim == 2:
        if query_embedding.shape[0] == 1:
            query_embedding = query_embedding[0]
        else:
            # Multiple query embeddings - use the first one
            query_embedding = query_embedding[0]
    
    # Calculate dot product for each document (cosine similarity since vectors are normalized)
    scores = []
    for i in range(len(documents)):
        doc_emb = doc_embeddings[i]
        # Dot product of normalized vectors = cosine similarity
        score = np.dot(query_embedding, doc_emb)
        scores.append(score)
    
    # Validate similarity scores are in valid range [-1, 1]
    for i, score in enumerate(scores):
        assert -1.0 <= score <= 1.0, f"Invalid similarity score {score:.6f} for document {i+1}"
    
    # Sort documents by score
    ranked_indices = np.argsort(scores)[::-1]  # Descending order
    
    print(f"\n{'='*80}")
    print("SIMILARITY SEARCH RESULTS:")
    print(f"{'='*80}")
    print("Ranked documents (most relevant first):")
    print(f"{'-'*80}")
    
    for rank, idx in enumerate(ranked_indices, 1):
        score = scores[idx]
        doc_preview = documents[idx][:100] + "..." if len(documents[idx]) > 100 else documents[idx]
        print(f"\nRank {rank} (Score: {score:.6f}):")
        print(f"Document: {doc_preview}")
        print(f"{'-'*80}")
    
    print("\n✓ Similarity search test PASSED")
    
    return scores, ranked_indices


def main():
    """Main function to run tests."""
    # Configuration
    triton_url = os.getenv("TRITON_URL", "localhost:8001")
    model_name = "nv-embed-v2"
    
    print("="*80)
    print("NVIDIA NV-Embed-v2 - Triton Inference Server Tests")
    print("="*80)
    print(f"Triton Server URL: {triton_url}")
    print(f"Model Name: {model_name}")
    
    # Test data - semantic similarity search example
    query = "How do neural networks learn patterns from examples?"
    
    documents = [
        "Neural networks learn through iterative training, where each example helps adjust internal parameters to better recognize patterns and make accurate predictions.",
        "Deep learning models adjust their weights through backpropagation, using gradient descent to minimize error on training data and improve predictions over time.",
        "Market prices are determined by the relationship between how much people want to buy a product and how much is available for sale, with scarcity driving prices up and abundance driving them down.",
        "The recipe for chocolate chip cookies includes flour, sugar, butter, eggs, vanilla extract, baking soda, salt, and chocolate chips mixed together and baked at 375°F.",
    ]
    
    try:
        # Create Triton client
        triton_client = grpcclient.InferenceServerClient(url=triton_url)
        
        # Check server health
        if not triton_client.is_server_live():
            print("ERROR: Triton server is not live!")
            sys.exit(1)
        
        if not triton_client.is_server_ready():
            print("ERROR: Triton server is not ready!")
            sys.exit(1)
        
        # Check if model is ready
        if not triton_client.is_model_ready(model_name):
            print(f"ERROR: Model '{model_name}' is not ready!")
            print("\nTrying to get model metadata...")
            try:
                metadata = triton_client.get_model_metadata(model_name)
                print(f"Model metadata: {metadata}")
            except Exception as e:
                print(f"Failed to get model metadata: {e}")
            sys.exit(1)
        
        print("✓ Server is live and ready")
        print(f"✓ Model '{model_name}' is ready")
        
        # Get and display model metadata
        try:
            metadata = triton_client.get_model_metadata(model_name)
            print(f"\nModel Metadata:")
            print(f"  Name: {metadata.name}")
            print(f"  Versions: {metadata.versions}")
            print(f"  Platform: {metadata.platform}")
        except Exception as e:
            print(f"Warning: Could not retrieve model metadata: {e}")
        
        # Run tests
        tests_passed = 0
        tests_failed = 0
        
        # Test 1: Query embedding
        try:
            test_query_embedding(triton_client, model_name, query)
            tests_passed += 1
        except Exception as e:
            print(f"\n✗ Query embedding test FAILED: {e}")
            import traceback
            traceback.print_exc()
            tests_failed += 1
        
        # Test 2: Document embeddings
        try:
            test_document_embeddings(triton_client, model_name, documents)
            tests_passed += 1
        except Exception as e:
            print(f"\n✗ Document embeddings test FAILED: {e}")
            import traceback
            traceback.print_exc()
            tests_failed += 1
        
        # Test 3: Similarity search
        try:
            test_similarity_search(triton_client, model_name, query, documents)
            tests_passed += 1
        except Exception as e:
            print(f"\n✗ Similarity search test FAILED: {e}")
            import traceback
            traceback.print_exc()
            tests_failed += 1
        
        # Print summary
        print("\n" + "="*80)
        print("TEST SUMMARY")
        print("="*80)
        print(f"Tests Passed: {tests_passed}")
        print(f"Tests Failed: {tests_failed}")
        print("="*80)
        
        if tests_failed > 0:
            sys.exit(1)
        else:
            print("\n✓ All tests PASSED!")
            sys.exit(0)
            
    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()