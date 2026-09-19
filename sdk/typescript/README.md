# Klyrow TypeScript client

The existing fetch-based client now exposes typed template history:

```ts
import { Klyrow } from "./src/index";

const client = new Klyrow(token);
const page = await client.templateVersions(templateId, { limit: 25 });
const version = await client.templateVersion(templateId, page.items[0].id);
```

`src/schema.d.ts` is generated from the implemented public OpenAPI export.
Regenerate after changing API handlers:

```sh
python scripts/export-api-contracts.py
npx --yes --package=openapi-typescript@7.10.1 openapi-typescript schemas/openapi/klyrow-public-api.yaml -o sdk/typescript/src/schema.d.ts
npx --yes --package=typescript@5.9.2 tsc --noEmit --strict --lib es2022,dom sdk/typescript/src/index.ts
```

Run commands from the repository root. CI regenerates to a temporary file,
compares the result and compiles the client against it. Existing send/message
methods keep their current contract; generated types do not imply that every
target-blueprint endpoint has been implemented.

Usage history reads the metering ledger, with inclusive `from` and exclusive
`to` UTC dates. Dates are `YYYY-MM-DD`; windows cannot exceed 366 days.

```ts
const daily = await client.usageDaily({ from: "2026-08-01", to: "2026-09-01", limit: 10 });
const next = daily.next_cursor
  ? await client.usageDaily({ cursor: daily.next_cursor, limit: 10 })
  : undefined;
const monthly = await client.usageMonthly({ from: "2026-01-01", to: "2026-09-01" });
```

The default unit is `accepted_message`, which measures accepted messages, not
recipient deliveries. When paginating a custom unit, pass the same `unit` with
each cursor. Periods without entries are omitted. Totals can change when late
ledger records arrive. Project filtering is unavailable until the ledger owns
project identity; unsupported filters are rejected by the API.
