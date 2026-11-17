import { mkdir, readFile, writeFile } from 'node:fs/promises';

const root = new URL('./', import.meta.url);
const read = (name) => readFile(new URL(name, root), 'utf8');

async function browserScript(scriptName, licenseNames) {
  const [script, ...licenses] = await Promise.all([read(scriptName), ...licenseNames.map(read)]);
  const bundle = script.replace(/^\/\/# sourceMappingURL=.*$/gm, '');
  const notice = licenses.join('\n\n');
  if (/<\/script/i.test(bundle) || /<\/script|\*\//i.test(notice)) {
    throw new Error(`Cannot safely inline ${scriptName}`);
  }
  return `/*\n${notice}\n*/\n${bundle}`;
}

const [template, sample, jsonViewer, markdown] = await Promise.all([
  read('template.html'),
  read('sample.json'),
  browserScript('node_modules/@alenaksu/json-viewer/dist/json-viewer.bundle.js', [
    'node_modules/@alenaksu/json-viewer/LICENSE',
    'node_modules/lit/LICENSE',
  ]),
  browserScript('node_modules/marked/lib/marked.umd.js', ['node_modules/marked/LICENSE']),
]);

const sampleJson = JSON.stringify(JSON.parse(sample)).replace(/</g, '\\u003c');
const replacements = {
  __TRACE_PAYLOAD__: sampleJson,
  __JSON_VIEWER_BUNDLE__: jsonViewer,
  __MARKDOWN_BUNDLE__: markdown,
};
const html = template.replace(
  /__TRACE_PAYLOAD__|__JSON_VIEWER_BUNDLE__|__MARKDOWN_BUNDLE__/g,
  (marker) => replacements[marker],
);
await mkdir(new URL('dist/', root), { recursive: true });
await writeFile(new URL('dist/index.html', root), html);
