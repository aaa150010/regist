const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadStepDefinitions() {
  const filePath = path.join(__dirname, '..', 'data', 'step-definitions.js');
  const context = {};
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(filePath, 'utf8'), context, { filename: filePath });
  return context.MultiPageStepDefinitions;
}

test('existing Plus mode contains only the six OAuth export nodes', () => {
  const definitions = loadStepDefinitions();
  const steps = definitions.getSteps({
    activeFlowId: 'openai',
    panelMode: 'existing-plus-oauth-json',
    plusModeEnabled: false,
  });

  assert.deepEqual(Array.from(steps, (step) => step.key), [
    'existing-plus-prepare-oauth',
    'oauth-email-login',
    'fetch-login-code',
    'phone-verification',
    'confirm-oauth',
    'existing-plus-save-json',
  ]);
  assert.equal(steps.some((step) => /checkout|paypal|payment|proxy/i.test(step.key)), false);
});

test('side panel exposes a target input and only SMSBower provider selection', () => {
  const projectRoot = path.join(__dirname, '..');
  const html = fs.readFileSync(path.join(projectRoot, 'sidepanel', 'sidepanel.html'), 'utf8');
  const css = fs.readFileSync(path.join(projectRoot, 'sidepanel', 'sidepanel.css'), 'utf8');
  const sidepanelSource = fs.readFileSync(path.join(projectRoot, 'sidepanel', 'sidepanel.js'), 'utf8');
  const backgroundSource = fs.readFileSync(path.join(projectRoot, 'background.js'), 'utf8');
  const providerSelect = html.match(/<select id="select-phone-sms-provider"[\s\S]*?<\/select>/)?.[0] || '';
  const providerValues = Array.from(providerSelect.matchAll(/<option value="([^"]+)"/g), (match) => match[1]);

  assert.match(html, /<label class="run-count-label" for="input-run-count">目标数<\/label>/);
  assert.match(html, /id="input-run-count"[^>]+aria-label="目标成功导出数量"/);
  assert.deepEqual(providerValues, ['smsbower']);
  assert.match(html, /id="input-hero-sms-min-price"[^>]+value="0\.03"/);
  assert.match(html, /id="input-hero-sms-max-price"[^>]+value="0\.15"/);
  assert.match(html, /id="steps-progress"[^>]*>0 \/ 6<\/span>/);
  assert.match(sidepanelSource, /const FIXED_PLUS_MODE_ENABLED = false;/);
  assert.match(backgroundSource, /smsBowerMinPrice: '0\.03'/);
  assert.match(backgroundSource, /smsBowerMaxPrice: '0\.15'/);
  assert.match(css, /#settings-card,[\s\S]*display:\s*none\s*!important/);
  assert.match(css, /#row-signup-method,[\s\S]*display:\s*none\s*!important/);
  assert.match(css, /#row-free-reusable-phone,[\s\S]*display:\s*none\s*!important/);
  assert.match(css, /#row-hotmail-local-base-url,[\s\S]*display:\s*flex\s*!important/);
});

test('OAuth email inputs suppress browser form-history suggestions', () => {
  const projectRoot = path.join(__dirname, '..');
  const source = fs.readFileSync(path.join(projectRoot, 'content', 'signup-page.js'), 'utf8');

  assert.match(source, /function suppressEmailInputSuggestions\(input\)/);
  assert.match(source, /input\.setAttribute\('autocomplete', 'off'\)/);
  assert.match(source, /input\.setAttribute\('data-lpignore', 'true'\)/);
  assert.match(source, /document\.activeElement === input[\s\S]*input\.blur\(\)/);
  assert.match(source, /async function step6LoginFromEmailPage[\s\S]*suppressEmailInputSuggestions\(emailInput\)/);
});
