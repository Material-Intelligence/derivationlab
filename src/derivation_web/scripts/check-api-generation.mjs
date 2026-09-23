import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, relative, resolve } from "node:path";
import { spawnSync } from "node:child_process";

const projectRoot = resolve(import.meta.dirname, "..");
const committedRoot = resolve(projectRoot, "src/api/generated");
const temporaryRoot = await mkdtemp(join(tmpdir(), "derivation-api-generated-"));

async function filesUnder(root) {
  const files = [];
  async function visit(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) await visit(path);
      else files.push(relative(root, path));
    }
  }
  await visit(root);
  return files.sort();
}

try {
  const result = spawnSync(
    process.execPath,
    [resolve(projectRoot, "node_modules/@hey-api/openapi-ts/bin/run.js")],
    {
      cwd: projectRoot,
      env: { ...process.env, DERIVATION_API_GENERATED_DIR: temporaryRoot },
      encoding: "utf8",
    },
  );
  if (result.status !== 0) {
    process.stderr.write(result.stdout);
    process.stderr.write(result.stderr);
    process.exit(result.status ?? 1);
  }

  const committedFiles = await filesUnder(committedRoot);
  const generatedFiles = await filesUnder(temporaryRoot);
  if (JSON.stringify(committedFiles) !== JSON.stringify(generatedFiles)) {
    throw new Error(`Generated API file list drifted:\ncommitted=${committedFiles.join(",")}\ngenerated=${generatedFiles.join(",")}`);
  }
  for (const file of committedFiles) {
    const [committed, generated] = await Promise.all([
      readFile(join(committedRoot, file)),
      readFile(join(temporaryRoot, file)),
    ]);
    if (!committed.equals(generated)) throw new Error(`Generated API file drifted: ${file}`);
  }
  process.stdout.write("Generated API contracts match OpenAPI.\n");
} finally {
  await rm(temporaryRoot, { recursive: true, force: true });
}
