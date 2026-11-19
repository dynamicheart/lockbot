import { mkdir, readFile, writeFile } from 'node:fs/promises';

const root = new URL('./', import.meta.url);
const read = (name) => readFile(new URL(name, root), 'utf8');
const uiRoot = new URL('../../frontend/src/features/agent-traces/', root);
const readViewer = (name) => readFile(new URL(name, uiRoot), 'utf8');
const [mode, tracePath, outputPath, ...extra] = process.argv.slice(2);
const online = mode === '--online';
const local = mode === '--local-trace';
if (extra.length || (local ? !tracePath || !outputPath : outputPath || tracePath || (mode && !online))) {
  throw new Error('Usage: node build.mjs [--online | --local-trace <trace.json> <private-viewer.html>]');
}

async function browserScript(scriptName, licenseNames) {
  const [script, ...licenses] = await Promise.all([read(scriptName), ...licenseNames.map(read)]);
  const bundle = script.replace(/^\/\/# sourceMappingURL=.*$/gm, '');
  const notice = licenses.join('\n\n');
  if (/<\/script/i.test(bundle) || /<\/script|\*\//i.test(notice)) {
    throw new Error(`Cannot safely inline ${scriptName}`);
  }
  return `/*\n${notice}\n*/\n${bundle}`;
}

const [template, sample, jsonViewer, markdown, theme, review, reviewStyles, modelLabels] = await Promise.all([
  readViewer('viewer.html'),
  local ? readFile(tracePath, 'utf8') : read('sample.json'),
  browserScript('node_modules/@alenaksu/json-viewer/dist/json-viewer.bundle.js', [
    'node_modules/@alenaksu/json-viewer/LICENSE',
    'node_modules/lit/LICENSE',
  ]),
  browserScript('node_modules/marked/lib/marked.umd.js', ['node_modules/marked/LICENSE']),
  readFile(new URL('../../frontend/src/assets/styles/theme.css', root), 'utf8'),
  readViewer('interactions.js'),
  readViewer('viewer.css'),
  readViewer('model-labels.js'),
]);

const sampleJson = JSON.stringify(JSON.parse(sample)).replace(/</g, '\\u003c');
const replacements = {
  __TRACE_PAYLOAD__: sampleJson,
  __TRACE_CONNECT_SRC__: online ? " connect-src 'self';" : '',
  __JSON_VIEWER_BUNDLE__: jsonViewer,
  __MARKDOWN_BUNDLE__: markdown,
  __LOCKBOT_THEME__: theme,
  __REVIEW_BUNDLE__: review,
  __REVIEW_STYLES__: reviewStyles,
  __MODEL_LABELS__: modelLabels.replace(/^export /gm, ''),
};
const html = template.replace(
  /__TRACE_PAYLOAD__|__TRACE_CONNECT_SRC__|__JSON_VIEWER_BUNDLE__|__MARKDOWN_BUNDLE__|__LOCKBOT_THEME__|__REVIEW_BUNDLE__|__REVIEW_STYLES__|__MODEL_LABELS__/g,
  (marker) => replacements[marker],
);
if (local) {
  await writeFile(outputPath, html, { mode: 0o600, flag: 'wx' });
} else {
  await mkdir(new URL('dist/', root), { recursive: true });
  await writeFile(new URL(online ? 'dist/online.html' : 'dist/index.html', root), html);
}
