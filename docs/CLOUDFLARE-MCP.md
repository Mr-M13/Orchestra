# Cloudflare MCP publication

Choose an existing Cloudflare-managed domain and create only `mcp.<domain>`.
No router forwarding, public PixelBox bind, DNS migration, or new domain is required.

1. Install the ARM64 `cloudflared` package from Cloudflare's supported Debian
   repository, then run `cloudflared tunnel login` as the owner and authorize
   the existing zone.
2. Create a named tunnel, for example `orchestra-mcp`, and create DNS route
   `mcp.<domain>` for that tunnel.
3. Create owner-only Cloudflare configuration with ingress
   `hostname: mcp.<domain>` and `service: http://127.0.0.1:8090`; include a
   final 404 ingress rule. Install Cloudflare's generated systemd service so it
   reconnects on boot.
4. Keep `orchestra-mcp-http.service` loopback-only. Put a generated bearer
   token in `/etc/orchestra/mcp-http.env` mode 0600, owned by `hamed`; never in
   this repository. Run `verify_mcp_http.sh` locally, then verify
   `https://mcp.<domain>/healthz` and authenticated `/mcp`.

ChatGPT remote MCP authentication must be confirmed in the owner’s current
OpenAI connector UI/documentation before use. If that UI cannot attach a static
Bearer token, retain this gateway closed and configure the supported OAuth flow
at Cloudflare Access or the supported ChatGPT mechanism; never make `/mcp`
anonymous. The Secure MCP Tunnel remains rollback transport until live tests pass.
