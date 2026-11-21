# Private live read path

The interim dashboard listener exposes a **private owner-only Unix-domain socket**. It does not listen on a TCP port on the server, implement login, trust forwarded identity headers, or publish arbitrary JSON/files. The application's security boundary is the approved SSH account and operating-system file permissions. It is a read-only path and does not control services.

Use an already authorized administrator SSH route and a dedicated existing socket directory with mode `0700`, owned by the listener UID. The aggregate database must be a private readable copy of sanitized aggregates, not the collector's raw inputs/contact data. Launch only through an authorized deployment or a foreground test:

```sh
python -m operations.private_http --db /private/example/aggregate-read.sqlite --socket /private/example/dashboard.sock
```

The socket is mode `0600`. The listener refuses any existing socket path rather than unlinking an unknown endpoint. Use an approved service runtime-directory lifecycle or explicitly reconcile an interrupted service's exact socket before restart. A graceful shutdown removes only the socket it created. The reader has no TCP bind flag.

On a trusted administrator PC, forward through the approved SSH host alias. The browser sees a local-only connection:

```sh
ssh -N -L 127.0.0.1:8765:/private/example/dashboard.sock approved-host
```

Open `http://127.0.0.1:8765/`. Use the actual deployed private socket path, not the examples. Do not bind the local tunnel to `0.0.0.0`. Close the tunnel when finished. Other processes on the local administrator PC are within the interim local trust boundary; do not use a shared or untrusted workstation. The server's private socket is inaccessible to other unprivileged server users. No public reverse-proxy route to this interim listener is allowed.

The listener accepts only `GET /` and `HEAD /`, validates the local Host and rejects cross-site browser requests, does not accept a body, and sends no-store, no-referrer and content security policy headers. It recomputes source freshness using the current clock for every read. An unavailable/corrupt aggregate store returns generic HTTP 503; it never creates a new database or exposes the raw source/exception. It serves only the fixed HTML aggregate view, without remote asset calls or browser scripts.

## Future shared sign-in integration

The existing sign-in staff console checks an owner-approved staff allowlist and recent authentication. Its ordinary GET page is **not** a strict authorization subrequest: it can return 200 for a stale proof while showing a reauthentication prompt, and it redirects unauthenticated users. Standard OIDC/session endpoints are not staff-role gates. Do not use successful access to the HTML page or a generic session as administrator authorization and do not scrape the page.

A future authenticated web route must use a dedicated strict server-side fresh-staff verifier: only a valid current allowed staff session with qualifying recent proof receives 204, no session receives 401, nonstaff/stale receives 403. The verifier must return no private payload and no redirect; failure/unavailability must fail closed. It is not a staff action and should not renew, revoke or alter the browser's session or invoke administrative operations. The trusted reverse proxy must call this verifier before each fixed dashboard request, strip all client-supplied authorization headers relevant to the viewer, prevent direct backend exposure and suppress caching. Explicitly review approved staff-role policy, proxy route, cookie path/scope, no-store, denial and direct-backend isolation on Test before any public route deployment. Do not conflate the SSH interim route with that eventual shared-login authorization path.
