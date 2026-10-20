// Native runtime plugins own their static assets in the deployed artifact.
import { mkdir, rename, rm } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
for (const [source, destination] of [
  ["static/dashboard", "plugins/ui/static/dashboard"],
  ["static/chat", "plugins/akashic_clients/static/chat"],
]) {
  const target = resolve(root, destination);
  await mkdir(dirname(target), { recursive: true });
  await rm(target, { recursive: true, force: true });
  await rename(resolve(root, source), target);
}
