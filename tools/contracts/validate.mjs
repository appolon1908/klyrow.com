import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { Parser } from '@asyncapi/parser';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';

const root = fileURLToPath(new URL('../../', import.meta.url));
const read = path => JSON.parse(readFileSync(root + path, 'utf8'));
const parser = new Parser();
for (const path of ['schemas/asyncapi/codestra-events.yaml', 'schemas/asyncapi/klyrow-events.yaml']) {
  const { document, diagnostics } = await parser.parse(readFileSync(root + path, 'utf8'));
  const failures = diagnostics.filter(item => item.severity === 0);
  if (!document || failures.length) throw new Error(JSON.stringify({ path, failures }));
}
const ajv = new Ajv2020({ strict: false, allErrors: true });
addFormats(ajv);
const validate = ajv.compile(read('schemas/json-schema/event-envelope.json'));
const example = read('schemas/examples/usage-daily.json');
if (!validate(example)) throw new Error(JSON.stringify(validate.errors));
for (const invalid of [
  { ...example, tenant_id: '' }, { ...example, version: 2 },
  { ...example, occurred_at: '2026-09-12T12:00:00' },
  { ...example, data: { ...example.data, quantity: -1 } },
  { ...example, password: 'forbidden-example-property' },
]) if (validate(invalid)) throw new Error('Invalid event passed schema validation');
console.log('PASS: AsyncAPI 3, JSON Schema, example and negative event validation');
