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
4. Keep `orchestra-mcp-http.service` loopback-only. The installer creates the
   `orchestra-mcp` group and `orchestra-mcp-http` service identity before
   staging gateway configuration. The gateway token file
   `/etc/orchestra/mcp-http.env` is owned by `orchestra-mcp-http`, group
   `orchestra-mcp`, mode `0600`. The installer preserves existing token
   contents, grants only group traverse access on `/etc/orchestra`, and never
   prints the token. Generate a token of at least 32 characters from the
   URL-safe/header-safe alphabet `[A-Za-z0-9._~+/=-]`, for example:
   `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`.
5. The service unit binds only `127.0.0.1:8090`, denies all network addresses
   except IPv4 loopback, restricts socket families to `AF_UNIX AF_INET`,
   restricts `bind()` to IPv4 TCP port 8090 on systemd >= 249, and drops all
   capabilities. It reaches the local OMCP socket
   `/run/orchestra/omcp.sock` through the `orchestra-mcp` group only. Do not
   change the listener to `0.0.0.0`, `::`, or a LAN address, and do not expose
   the port through any other proxy.
6. Run `verify_mcp_http.sh` locally (it prefers the installer-managed
   `/usr/local/lib/orchestra/http_gateway_verify.py`), then verify
   `https://mcp.<domain>/healthz` and authenticated `/mcp`. `/readyz` also
   requires the bearer token; use `/healthz` for an unauthenticated liveness
   probe. The verifier checks the exact loopback listener, the gateway
   identity's read/write access to the coordinator socket, and 20 authenticated
   initialize/tools/delete cycles.
7. Apply Cloudflare WAF/rate limiting to the published hostname. The gateway
   itself applies an absolute request deadline, bounded HTTP concurrency, and a
   bounded session count, but edge rate limiting is still part of the intended
   ingress boundary.

The gateway expires idle MCP sessions after 15 minutes, sweeps them in the
background, caps concurrent sessions, and closes every backend socket on
shutdown or `DELETE`. It emits token-free JSON audit events for authentication
failures, request rejection, session limits/expiry and backend failures.
Cloudflare ingress is not a substitute for gateway authentication. A compromised
gateway process can reach other loopback services because the service needs
IPv4 loopback; keep the process minimal and monitor it as part of the authority
boundary.

ChatGPT remote MCP authentication must be confirmed in the owner’s current
OpenAI connector UI/documentation before use. If that UI cannot attach a static
Bearer token, retain this gateway closed and configure the supported OAuth flow
at Cloudflare Access or the supported ChatGPT mechanism; never make `/mcp`
anonymous. The Secure MCP Tunnel remains rollback transport until live tests pass.
