#!/usr/bin/env bash
# Install the localhost card/video render service as a systemd unit on the n8n host.
# Run this ON the n8n machine, after copying render_service.py there.
# Prereqs on the host:  python3, chromium, ffmpeg, fontconfig, and the Sora font (see README).
#
# Usage:  ./deploy_render_service.sh [path/to/render_service.py] [run-user] [linkedin-handle]
#   linkedin-handle defaults to $LINKEDIN_HANDLE; it is only the fallback watermark text,
#   n8n sends the configured handle with every request anyway.
set -e
SRC="${1:-render_service.py}"          # path to render_service.py
RUN_USER="${2:-n8n}"                    # user the service runs as (must match your n8n user)
HANDLE="${3:-${LINKEDIN_HANDLE:-your-handle}}"

mkdir -p /opt/render
cp "$SRC" /opt/render/render_service.py
chown -R "$RUN_USER":"$RUN_USER" /opt/render

cat > /etc/systemd/system/linkedin-render.service <<UNIT
[Unit]
Description=LinkedIn agent card/video render service (localhost only)
After=network.target
[Service]
Type=simple
User=${RUN_USER}
Environment=LINKEDIN_HANDLE=${HANDLE}
ExecStart=/usr/bin/python3 /opt/render/render_service.py
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now linkedin-render
sleep 1
systemctl is-active linkedin-render
echo "--- health ---"
curl -s http://127.0.0.1:8099/health || echo "HEALTH_FAIL"
echo
