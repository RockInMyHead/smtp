#!/bin/zsh
cd "${0:A:h}"
if /usr/bin/curl -fsS -o /dev/null http://127.0.0.1:8765/; then
  open http://127.0.0.1:8765/
else
  python3 local_service.py
fi
