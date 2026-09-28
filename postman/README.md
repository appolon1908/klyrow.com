# Postman API authority

OpenAPI is the source of truth. Postman collections are derived test/evidence artifacts and must not become an independent API contract.

Workflow:
1. Update API implementation and OpenAPI together.
2. Regenerate/refresh the Postman collection from committed OpenAPI.
3. Keep secrets out of collections and environments.
4. Run static contract tests plus Newman against local/staging-safe targets.
5. Never use Postman to bypass CI, approval, staging, runtime readback, rollback, or production gates.
6. Production writes/effects remain disabled unless the repository's protected production gate explicitly authorizes them.

If a collection drifts from OpenAPI, the OpenAPI contract wins and the collection must be regenerated.
