// Keep the runtime asset reproducible without requiring Node.js on user machines.
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const packageRoot = path.dirname(require.resolve("markdown-it/package.json"));
const pkg = require("markdown-it/package.json");
function packageDirectory(name) {
  // Resolve from markdown-it: a dependency may be nested instead of hoisted.
  let directory = path.dirname(require.resolve(name, { paths: [packageRoot] }));
  while (path.dirname(directory) !== directory) {
    const manifest = path.join(directory, "package.json");
    if (fs.existsSync(manifest) && JSON.parse(fs.readFileSync(manifest, "utf8")).name === name)
      return directory;
    directory = path.dirname(directory);
  }
  throw new Error(`Cannot locate package metadata for ${name}`);
}
const assets = {
  "markdown-it.min.js": fs.readFileSync(require.resolve("markdown-it/browser")),
  "markdown-it-LICENSE.txt": Buffer.from(
    ["markdown-it", "mdurl", "uc.micro", "entities", "linkify-it", "punycode.js"]
      .map((name) => {
        const directory = packageDirectory(name);
        const metadata = JSON.parse(fs.readFileSync(path.join(directory, "package.json"), "utf8"));
        const license = ["LICENSE", "LICENSE.txt", "LICENSE-MIT.txt"].find((file) => fs.existsSync(path.join(directory, file)));
        if (!license) throw new Error(`Missing license for ${name}`);
        return `${name} ${metadata.version}\n${fs.readFileSync(path.join(directory, license), "utf8").trim()}\n`;
      })
      .join("\n"),
  ),
};
const check = process.argv.includes("--check");
if (!check) fs.mkdirSync(path.join(root, "vendor"), { recursive: true });
for (const [name, bytes] of Object.entries(assets)) {
  const destination = path.join(root, "vendor", name);
  if (check) {
    if (!fs.existsSync(destination) || !fs.readFileSync(destination).equals(bytes))
      throw new Error(`Run npm run vendor:markdown to update vendor/${name}`);
  } else fs.writeFileSync(destination, bytes);
}
console.log(`markdown-it ${pkg.version}: local assets ${check ? "verified" : "updated"}`);
