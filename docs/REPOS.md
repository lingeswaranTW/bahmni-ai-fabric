# Where each piece lives

All changes are uncommitted working-tree changes in `~/Data/bahmni/<repo>`; review and commit per repo.

| Repo | Change |
|---|---|
| **bahmni-ai** (this repo) | Python service, its tests, Dockerfile, CI, docs. Uses its own Postgres (`bahmni-ai-db`), never the OpenMRS database. |
| **bahmni-core** | `bahmnicore-omod/.../web/v1_0/controller/AiServiceController.java` and `.../client/AiServiceClient.java` (+ tests): streaming proxy `POST /openmrs/ws/rest/v1/bahmnicore/ai/chat` and `/chat/confirm`. Run: `mvn -o -pl bahmnicore-omod -am test -Dtest='AiService*Test'`. |
| **standard-config** | `ai/mcp-servers.json` (default: no servers), `ai/mcp-servers.example.json`, `ai/README.md`; `package/docker/Dockerfile` copies `ai/` into `/etc/bahmni_config/ai`. |
| **openmrs-distro-bahmni** | `bahmnicore.properties.template`: `ai.service.url` and timeouts. Helm: `BAHMNI_AI_SERVICE_URL` in `values.yaml` and `configMap.yaml`. |
| **bahmni-docker** (`bahmni-standard`) | `docker-compose.yml`: `bahmni-ai` and `bahmni-ai-db` services (profile `bahmni-ai`, volume `bahmni-ai-db-data`), `BAHMNI_AI_SERVICE_URL` for `openmrs`. `.env`: `BAHMNI_AI_*`, `AI_MODEL`. `bahmni-lite` is not changed. |

Still manual: create the OpenMRS privileges (`Use AI Chatbot`, plus one per mapped action) and assign them to roles;
publish the `bahmni/bahmni-ai` image; supply secrets (`ANTHROPIC_API_KEY`, `BAHMNI_AI_DB_PASSWORD`, `MCP_*`) outside git;
check that `bahmni-proxy` does not buffer SSE for `/openmrs/...`.
Enable with `--profile bahmni-ai` (the profile is opt-in, so existing deployments are unaffected).
