#!/bin/bash
# Save LockBot Docker image for offline transfer
# Usage: ./scripts/save_image.sh [tag] [output_path]
# Then on target machine: docker load -i <output_path>

set -e
TAG="${1:-latest}"
OUTPUT="${2:-lockbot-${TAG}.tar}"
IMAGE="ghcr.io/dynamicheart/lockbot:${TAG}"
echo "Saving ${IMAGE} -> ${OUTPUT} ..."
docker save "$IMAGE" -o "$OUTPUT"
echo "Done. $(du -h "$OUTPUT" | cut -f1)"
echo "Transfer to target machine and run: docker load -i ${OUTPUT}"
