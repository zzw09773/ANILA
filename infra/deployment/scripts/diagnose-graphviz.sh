#!/usr/bin/env bash
# Diagnose Studio diagram path failures.
# Usage: ./scripts/diagnose-graphviz.sh [csp-container-name]
# Default container: anila-platform-dev-csp-1
set -u
CONTAINER="${1:-anila-platform-dev-csp-1}"

echo "=== 1. dot binary present in container ==="
docker exec "$CONTAINER" which dot && docker exec "$CONTAINER" dot -V 2>&1 | head -3 || echo "MISSING: rebuild csp image (Dockerfile has apt-get install graphviz)"

echo
echo "=== 2. CJK fonts available ==="
docker exec "$CONTAINER" sh -c 'fc-list 2>/dev/null | grep -i noto.*cjk | head -3' || echo "MISSING: rebuild csp image (Dockerfile has fonts-noto-cjk)"

echo
echo "=== 3. Manual render_dot_to_png test ==="
docker exec "$CONTAINER" python -c "
import asyncio
from app.services.diagram_renderer import render_dot_to_png
dot = '''
digraph G {
  rankdir=LR;
  node [shape=box, fontname=\"Noto Sans CJK TC\"];
  Supervisor -> \"Worker A\" [label=\"任務分派\"];
  Supervisor -> \"Worker B\";
}
'''
result = asyncio.run(render_dot_to_png(dot))
print(f'Result: {len(result) if result else None} bytes')
" 2>&1 | head -10

echo
echo "=== 4. Recent diagram-path logs ==="
docker logs --since 1h "$CONTAINER" 2>&1 | grep -i "diagram\|graphviz\|hydrat" | tail -15 || echo "(no recent diagram-path activity)"

echo
echo "=== diagnostic complete ==="
