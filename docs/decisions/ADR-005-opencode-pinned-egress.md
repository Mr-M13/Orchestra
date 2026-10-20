# ADR-005: Endpoint-pinned OpenCode provider egress

Status: candidate; activation requires independent target-Mac verification.

## Context

The OpenCode V2 CLI needs loopback sockets for its private server. Its convenient `--standalone` mode chooses an ephemeral port internally, forcing a Seatbelt profile to allow wildcard loopback bind and outbound operations. macOS Seatbelt accepts `*` or `localhost` in these network filters but rejects a remote DNS hostname such as `api.deepseek.com`. Wildcard loopback access could expose unrelated local services, while remote `*:443` would let a compromised worker reach arbitrary TLS services. Disabling TLS verification would remove server authentication. None is acceptable.

OpenCode 2.0.32 expects the singular `provider` object with `npm` and `options` fields. The gateway configuration preserves that reviewed schema while replacing the remote base URL and real key with a task-local capability URL and synthetic key.

## Decision

Use a one-task reverse gateway owned by the trusted Python runner:

1. Validate the code-owned provider, model, endpoint and owner-only credential before binding.
2. Bind an ephemeral IPv4 loopback listener and generate a 256-bit random path capability.
3. Select a second loopback port, generate a random V2 server password, and start `opencode serve` on exactly that port under Seatbelt.
4. Generate the OpenCode policy with the gateway capability URL, a non-secret synthetic API key, and provider allow/deny rules in the reviewed top-level `policies` field.
5. Do not expose the credential file to either OpenCode Seatbelt profile or process environment.
6. Give the V2 server exact bind/inbound permission for its port and exact outbound permission for the gateway port. Give the CLI client exact outbound permission for the V2 server port. Give neither process wildcard loopback or remote network permission.
7. Accept only authenticated, fixed-host POST requests to the capability path ending in `/v1/chat/completions`; reject CONNECT and all endpoint, model, encoding, count and size deviations.
8. Re-serialize the bounded JSON request, inject the provider key in trusted code, and use Python's default CA roots, hostname verification and SNI to connect to the fixed host on port 443. Never follow redirects.
9. Forward only JSON or server-sent-event responses, bound each response, and retain neither body nor headers.
10. Reduce client output and private runtime logs in memory to an allowlisted diagnostic; discard server output. Stop the private server and gateway, delete runtime state, then write only the bounded completion diagnostic.

This pins provider egress in trusted code, like the direct DeepSeek adapter, while retaining OpenCode's workspace capability. The worker does not receive broad remote port-443 authority.

## Rejected alternatives

- Remote `*:443` in Seatbelt: too broad; provider configuration is not an OS boundary.
- Remote hostname in Seatbelt: rejected by macOS profile compilation.
- Disabled certificate checks, raw TCP relay or CONNECT proxy: removes or delegates endpoint authentication and can become general egress.
- `run --standalone` with wildcard loopback access: avoids managing a server but unnecessarily exposes unrelated loopback services.
- Static provider IPs: incomplete for DNS rotation/CDNs and breaks hostname-authenticated service operation.
- Passing the real provider key to OpenCode: unnecessary after the gateway exists and increases exposure.

## Verification gate

Safe checks before any real provider request:

- run the complete Python unit suite and `compileall`;
- verify the generated policy has singular `provider` / `npm` / `options` fields, top-level `policies`, only the random `http://127.0.0.1:<port>/<capability>/v1` base URL, and the synthetic key;
- verify the generated Seatbelt profiles contain only their exact V2-server and gateway loopback ports, no wildcard loopback rule, no remote hostname, no `*:443`, and no credential-file read rule;
- compile and launch that profile on an independently controlled target-Mac test account;
- with a synthetic credential and disposable workspace, confirm the gateway rejects wrong paths, models, encodings, oversized bodies and excess requests and that OpenCode receives a neutral failure;
- observe sockets externally and confirm the OpenCode process has only loopback sockets while the trusted runner connects only to the selected provider on 443.

This Orchestra worker cannot complete the target-Mac socket/profile checks because its enclosing sandbox denies `sandbox_apply` and local socket creation. Those are blockers to activation, not reasons to weaken the candidate policy.

A credentialed harmless test is a separate step. It requires fresh local approval, a disposable clone and candidate state, no live tunnel or configuration changes, and inspection proving that the credential is absent from arguments, environment, generated policy, state, audit, logs and results. Merge, deployment, tunnel restart and production activation remain separately reviewed operations.
