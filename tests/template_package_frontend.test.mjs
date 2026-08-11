import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const appSource = readFileSync(resolve(projectRoot, "static/app.js"), "utf8");
const htmlSource = readFileSync(resolve(projectRoot, "templates/index.html"), "utf8");

test("ROTPL upload and selection controls are exposed accessibly", () => {
  assert.match(htmlSource, /id="templatePackageInput"[^>]+accept="\.rotpl,application\/zip"/);
  assert.match(htmlSource, /id="installedTemplates"[^>]+aria-live="polite"/);
  assert.match(htmlSource, /id="templateUploadProgress"[^>]+role="progressbar"/);
  assert.match(htmlSource, /name="template_id" id="selectedTemplateId"/);
  assert.match(htmlSource, /name="template_version" id="selectedTemplateVersion"/);
  assert.match(htmlSource, /name="sha256" id="selectedTemplateSha256"/);
  for (const field of ["coordinates_text", "oxidizer", "fuel", "ablative_material"]) {
    assert.match(htmlSource, new RegExp(`name="${field}"`));
  }
});

test("frontend uses the versioned template REST contract", () => {
  assert.match(appSource, /fetch\("\/api\/templates"/);
  assert.match(appSource, /body\.append\("template", file, file\.name\)/);
  assert.match(appSource, /`\/api\/templates\/\$\{encodeURIComponent\(normalized\.id\)\}\/activate`/);
  assert.match(appSource, /JSON\.stringify\(\{ version: normalized\.version \}\)/);
});

test("selected template id and version are sent to preview and export", () => {
  assert.match(appSource, /\.\.\.selectedTemplatePayload\(\),\s*\.\.\.dimensions/);
  assert.match(appSource, /body\.set\("template_id", selectedTemplatePackage\.id\)/);
  assert.match(appSource, /body\.set\("template_version", selectedTemplatePackage\.version\)/);
  assert.match(appSource, /body\.set\("sha256", selectedTemplatePackage\.sha256\)/);
  assert.match(appSource, /body\.delete\("template_id"\)/);
  assert.match(appSource, /body\.delete\("template_version"\)/);
});

test("production metadata normalization requires an immutable id and version", () => {
  const start = appSource.indexOf("function templatePackageKey(");
  const end = appSource.indexOf("\nfunction setTemplateFeedback", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const productionFunctions = appSource.slice(start, end);
  const context = vm.createContext({});
  vm.runInContext(`
    let selectedTemplatePackage = null;
    ${productionFunctions}
    globalThis.api = {
      key: templatePackageKey,
      name: templatePackageName,
      normalize: normalizeTemplatePackage,
      payload: selectedTemplatePayload,
      select(value) { selectedTemplatePackage = value; },
    };
  `, context);

  const normalized = context.api.normalize({
    template_id: "stellar.duqm",
    template_version: "1.2.0",
    name: { ar: "قالب الدقم", en: "DUQM" },
    sha256: "f".repeat(64),
    validation: { valid: true, errors: [], warnings: ["missing optional logo"] },
  });
  assert.equal(normalized.id, "stellar.duqm");
  assert.equal(normalized.version, "1.2.0");
  assert.equal(normalized.validation.valid, true);
  assert.equal(context.api.key(normalized), "stellar.duqm@1.2.0");
  assert.equal(context.api.name(normalized), "قالب الدقم");
  assert.equal(normalized.sha256, "f".repeat(64));
  context.api.select(normalized);
  assert.equal(JSON.stringify(context.api.payload()), JSON.stringify({
    template_id: "stellar.duqm",
    template_version: "1.2.0",
    sha256: "f".repeat(64),
  }));
  assert.equal(context.api.normalize({ id: "missing-version" }), null);
  const blocked = context.api.normalize({
    id: "stellar.blocked",
    version: "1.0.0",
    validation: {
      valid: true,
      activatable: false,
      blocked_reasons: ["Missing final TTF files"],
    },
  });
  assert.equal(blocked.blocked, true);
  assert.equal(blocked.validation.valid, false);
  assert.deepEqual(
    Array.from(blocked.validation.errors),
    ["Missing final TTF files"],
  );
});
