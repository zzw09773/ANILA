# Functions v1 E2E

Pre-reqs:

* Sprint 2.5 prototype gate has passed (capability landing verified)
* docker-compose stack is up: csp + worker-api + sandbox-{exec,extract}
  + egress proxy + anila-ui
* ANILA UI is reachable at $ANILA_UI_URL (default http://localhost:3001)
* Three test users seeded: `dev1` (developer), `admin1` (admin), `user1` (user)
  all with password `dev-password`

Run:

    cd ANILA_UI/anila-ui
    npx playwright install --with-deps  # one-time
    npx playwright test e2e/

The scaffold in `functions.spec.js` covers the spec §8.4 must-have
scenarios (happy path, RBAC, verb whitelist injection rejection,
ownership 403). Add per-host_command verb tests as the verb set grows
in v1.x.
