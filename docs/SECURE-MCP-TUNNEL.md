# Secure MCP Tunnel: read-only Mac Bridge status

This integration exposes one read-only MCP tool, `bridge_status`, through
OpenAI Secure MCP Tunnel. It returns the local Bridge heartbeat age, aggregate
task counts by status, and aggregate counts by configured worker. It does not
return issue identifiers, task bodies, prompts, logs, filesystem paths, process
IDs, repository configuration, or credentials.

The MCP server has no approval, dispatch, cancellation, retry, Git, shell, or
filesystem-selection tools. Human approval remains the existing
`bridge-approved` issue label, and execution remains inside the existing
Bridge's isolated worktrees and worker sandbox. A tunnel or MCP failure does
not change Bridge state or retry policy. Use the Bridge's local state and logs
for diagnosis; do not infer task correctness from a heartbeat or status count.

## Local setup

1. Install the official client using OpenAI's supported macOS Homebrew tap:

   ```sh
   brew install openai/tools/tunnel-client
   tunnel-client --version
   ```

2. Create a runtime API key with Tunnels Read + Use access to the intended
   tunnel. Store it in macOS Keychain. Do not use an admin key for the runtime.
   The daemon reads the secret from Keychain at launch, exports it only to its
   own process environment as `CONTROL_PLANE_API_KEY`, and never writes it to
   the tunnel profile or logs. The stdio launcher must unset both
   `CONTROL_PLANE_API_KEY` and `OPENAI_API_KEY` before starting the MCP server,
   so the read-only child does not inherit the control-plane credential.

3. Create a local profile with the intended tunnel ID and this server's stdio
   command. Keep the profile in the user's private Application Support area,
   not in this repository. The API-key reference must be
   `env:CONTROL_PLANE_API_KEY`.

4. Use a per-user LaunchAgent to run the Keychain wrapper and
   `tunnel-client run --profile <name>`. Keep the health listener bound to
   `127.0.0.1`; do not enable remote UI access. Set owner-only permissions on
   the LaunchAgent, wrapper, profile directory, and private logs.

5. Run `tunnel-client doctor --profile <name> --explain` with the Keychain
   wrapper's environment, then start the client and check its local
   `/healthz`, `/readyz`, and `/ui`. OpenAI's guide requires the client to be
   running during connector discovery and MCP calls.

6. In the intended ChatGPT workspace, add a custom MCP app using the existing
   tunnel. Confirm that its workspace association is the intended one, then
   invoke `bridge_status` and compare the aggregate result with the local
   Bridge state. Keep the Mac Bridge and Desktop Commander running until this
   end-to-end call succeeds.

## Credential wrapper shape

The private wrapper should retrieve the configured Keychain generic password
without printing it, fail closed when it is unavailable, and then replace
itself with the tunnel process. For example, replace the Keychain service name
and profile name with the local values:

```sh
#!/bin/sh
set -eu
key=$(/usr/bin/security find-generic-password -s 'KEYCHAIN_SERVICE_NAME' -w 2>/dev/null) || exit 1
[ -n "$key" ] || exit 1
export CONTROL_PLANE_API_KEY="$key"
unset key
exec /opt/homebrew/bin/tunnel-client run --profile PROFILE_NAME
```

Do not enable shell tracing, print the environment, put the API key in
`EnvironmentVariables`, or pass it as a command-line argument. On Apple
Silicon, the Homebrew path is `/opt/homebrew/bin`; verify the installed path
locally rather than assuming it.

## Recovery

- If the local client is not healthy/ready, run `doctor` through the same
  Keychain wrapper and inspect its redacted local diagnostics.
- If the API key is missing or rejected, stop at that authentication failure;
  do not substitute an admin key or broaden tunnel permissions.
- If the MCP server cannot read the fixed Bridge state file or its schema is
  invalid, the tool reports unavailable status without exposing local details.
- Restarting the tunnel client does not restart or modify the Mac Bridge.
- Never automatically retry or reapprove Bridge tasks from this MCP service.

## Official references

- [OpenAI Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
- [OpenAI tunnel-client](https://github.com/openai/tunnel-client)
- [ChatGPT custom MCP server setup](https://developers.openai.com/api/docs/guides/custom-mcp-server)
