// Chama o validate.mjs da skill shopify-admin passando o conteudo do arquivo
// como argumento. Necessario porque o PowerShell 5.1 corrompe aspas ao repassar
// argumentos para executaveis nativos.
//
// Prefere a skill do PROJETO (.claude/skills), que tem precedencia sobre a
// global e costuma estar mais atualizada.
//
// Uso: node tools/validate_gql.mjs <arquivo.graphql> [revision] [apiVersion]

import { existsSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const [file, revision = "1", apiVersion = "2026-07"] = process.argv.slice(2);
if (!file) {
  console.error("uso: node tools/validate_gql.mjs <arquivo.graphql> [revision] [apiVersion]");
  process.exit(2);
}

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const candidates = [
  join(root, ".claude", "skills", "shopify-admin", "scripts", "validate.mjs"),
  join(homedir(), ".claude", "skills", "shopify-admin", "scripts", "validate.mjs"),
];
const validator = candidates.find(existsSync);
if (!validator) {
  console.error("validate.mjs nao encontrado em:\n  " + candidates.join("\n  "));
  process.exit(2);
}
console.error(`[validador] ${validator}`);
console.error(`[versao]    ${apiVersion}\n`);
const code = readFileSync(file, "utf8").replace(/^﻿/, "");

const prompt =
  "boa le dnvo o api-keys que eu botei todos os escopos e a url da loja, sobre cpf e " +
  "essas coisas pode coloar no notes, pode dividir esse mocks em outros 3 json validos " +
  "se ficar melhor pra voce, sobre estoques nao rpecisa decrementar, e se quiser pode " +
  "criar mais dados dummys para simular mais 2 paginas e talz pra rodar o script lizo";

const result = spawnSync(
  process.execPath,
  [
    validator,
    "--code", code,
    "--user-prompt-base64", Buffer.from(prompt, "utf8").toString("base64"),
    "--session-id", "64549f34-865e-421d-8be0-ff98be271b68",
    "--model", "claude-opus-5",
    "--client-name", "claude-code",
    "--client-version", "2.0",
    "--artifact-id", "bagy2shopify-ordercreate",
    "--revision", revision,
    "--version", apiVersion,
  ],
  { encoding: "utf8" }
);

process.stdout.write(result.stdout || "");
process.stderr.write(result.stderr || "");
process.exit(result.status ?? 1);
