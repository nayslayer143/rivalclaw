#!/bin/bash
# Start the rivalclaw-bridge Cloudflare tunnel
# This exposes the FastAPI bridge at api.eternalrevenueservice.com
#
# Prerequisites:
#   1. cloudflared tunnel create rivalclaw-bridge (done)
#   2. cloudflared tunnel route dns rivalclaw-bridge api.eternalrevenueservice.com
#   3. Domain eternalrevenueservice.com on Cloudflare with active nameservers

TUNNEL_ID="3abb0859-da2e-4715-b617-7a01d041b1b1"
CREDS="/Users/nayslayer/.cloudflared/${TUNNEL_ID}.json"

exec cloudflared tunnel --url http://localhost:8400 \
  --name rivalclaw-bridge \
  --credentials-file "$CREDS"
