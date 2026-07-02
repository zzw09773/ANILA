#!/bin/bash
docker save tritonserver:25.04-transformers-4.42.4 | gzip > tritonserver-25.04-transformers-4.42.4.tar.gz