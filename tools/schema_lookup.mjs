// Consulta o schema da Admin API que vem junto com a skill shopify-admin.
// Mais confiavel que a busca por documentacao: le o schema em si.
//
// Uso:
//   node tools/schema_lookup.mjs type OrderCreateOrderInput
//   node tools/schema_lookup.mjs enum LocalizedFieldKey BR
//   node tools/schema_lookup.mjs find TAX_CREDENTIAL_BR

import { existsSync, readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const [mode, name, filter] = process.argv.slice(2);
const VERSION = process.env.SCHEMA_VERSION || "2026-07";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const candidates = [
  join(root, ".claude", "skills", "shopify-admin", "assets", `admin_${VERSION}.json.gz`),
  join(homedir(), ".claude", "skills", "shopify-admin", "assets", `admin_${VERSION}.json.gz`),
];
const file = candidates.find(existsSync);
if (!file) {
  console.error(`schema ${VERSION} nao encontrado em:\n  ${candidates.join("\n  ")}`);
  process.exit(2);
}

const schema = JSON.parse(gunzipSync(readFileSync(file)).toString("utf8"));
const types = schema.data?.__schema?.types || schema.__schema?.types || schema.types || [];
console.error(`[schema] ${file}  (${types.length} tipos)\n`);

const find = (n) => types.find((t) => t.name === n);

if (mode === "type") {
  const type = find(name);
  if (!type) { console.error(`tipo ${name} nao encontrado`); process.exit(1); }
  const fields = type.inputFields || type.fields || [];
  console.log(`${type.kind} ${type.name}  (${fields.length} campos)\n`);
  for (const f of fields) {
    const t = f.type;
    const render = (x) => !x ? "?" :
      x.kind === "NON_NULL" ? render(x.ofType) + "!" :
      x.kind === "LIST" ? "[" + render(x.ofType) + "]" : x.name;
    const match = filter && !f.name.toLowerCase().includes(filter.toLowerCase());
    if (!match) console.log(`  ${f.name.padEnd(28)} ${render(t)}`);
  }
} else if (mode === "enum") {
  const type = find(name);
  if (!type) { console.error(`enum ${name} nao encontrado`); process.exit(1); }
  const values = (type.enumValues || []).filter(
    (v) => !filter || v.name.includes(filter)
  );
  console.log(`enum ${type.name}  (${values.length} de ${(type.enumValues || []).length})\n`);
  for (const v of values) console.log(`  ${v.name}`);
} else if (mode === "find") {
  const needle = name.toLowerCase();
  for (const t of types) {
    for (const v of t.enumValues || []) {
      if (v.name.toLowerCase().includes(needle)) console.log(`enum ${t.name}.${v.name}`);
    }
    for (const f of [...(t.inputFields || []), ...(t.fields || [])]) {
      if (f.name.toLowerCase().includes(needle)) console.log(`${t.kind} ${t.name}.${f.name}`);
    }
  }
} else {
  console.error("uso: node tools/schema_lookup.mjs <type|enum|find> <nome> [filtro]");
  process.exit(2);
}
