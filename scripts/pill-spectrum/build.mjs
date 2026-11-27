import { build } from "esbuild";
import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const outfile = resolve(here, "../../ui/pill/spectrum.js");
const result = await build({
  absWorkingDir: here, entryPoints: ["entry.js"], outfile, bundle: true,
  format: "iife", target: ["safari16.4", "chrome110"], minify: true,
  legalComments: "eof", metafile: true,
});
const packages = new Map();
const modules = resolve(here, "node_modules") + "/";
for (const input of Object.keys(result.metafile.inputs)) {
  let path = dirname(resolve(here, input));
  let found = false;
  for (; path.startsWith(modules); path = dirname(path)) {
    const info = resolve(path, "package.json");
    if (!existsSync(info)) continue;
    const manifest = JSON.parse(readFileSync(info, "utf8"));
    if (!manifest.license || !manifest.name) continue;
    packages.set(manifest.name + "@" + manifest.version, [path, manifest.license]);
    found = true;
    break;
  }
  if (!found && resolve(here, input).startsWith(modules)) throw new Error("Missing dependency license: " + input);
}
const notices = [...packages.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([name, [path, license]]) => {
  const files = readdirSync(path).filter((file) => /^(licen[cs]e|notice)(\.|$)/i.test(file)).sort();
  return [name + " (" + license + ")", ...files.map((file) =>
    readFileSync(resolve(path, file), "utf8").trim().split(/\r?\n/).map((line) => line.trimEnd()).join("\n"))].join("\n");
});
const adobe = readFileSync(resolve(here, "Adobe-Spectrum-LICENSE.txt"), "utf8").trim();
writeFileSync(resolve(here, "../../ui/pill/spectrum.NOTICES.txt"),
  ["Adobe Spectrum Web Components 1.12.6 (upstream source license)\n\n" + adobe, ...notices].join("\n\n---\n\n") + "\n");
