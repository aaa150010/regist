const test = require('node:test');
const assert = require('node:assert/strict');

const policy = require('../background/existing-plus-run-policy.js');

function account(id, overrides = {}) {
  return {
    id,
    email: `${id}@outlook.com`,
    refreshToken: `token-${id}`,
    used: false,
    ...overrides,
  };
}

test('candidate ranking permanently excludes used and successful accounts', () => {
  const ranked = policy.rankExistingPlusHotmailCandidates([
    account('used', { used: true }),
    account('success'),
    account('ready'),
    account('missing-token', { refreshToken: '' }),
  ], {
    success: { status: 'success', updatedAt: 10 },
  });

  assert.deepEqual(ranked.map((item) => item.id), ['ready']);
});

test('candidate ranking tries untouched accounts before retrying oldest failures', () => {
  const ranked = policy.rankExistingPlusHotmailCandidates([
    account('recent-failure'),
    account('untouched'),
    account('old-failure'),
    account('interrupted'),
  ], {
    'recent-failure': { status: 'failed', updatedAt: 300 },
    'old-failure': { status: 'failed', updatedAt: 100 },
    interrupted: { status: 'running', updatedAt: 200 },
  });

  assert.deepEqual(ranked.map((item) => item.id), [
    'untouched',
    'old-failure',
    'interrupted',
    'recent-failure',
  ]);
});

test('auth rate limit detection is narrow and does not match SMS provider limits', () => {
  assert.equal(policy.isAuthRateLimitFailure('RATE_LIMIT_EXCEEDED::认证页请求过多'), true);
  assert.equal(policy.isAuthRateLimitFailure(new Error('state=rate_limit_exceeded_page')), true);
  assert.equal(policy.isAuthRateLimitFailure('SMSBower rate limit (429)'), false);
  assert.equal(policy.isAuthRateLimitFailure('普通网络请求失败'), false);
});

test('auth rate limit policy waits 20 seconds and retries at most once', () => {
  assert.equal(policy.AUTH_RATE_LIMIT_COOLDOWN_MS, 20000);
  assert.equal(policy.AUTH_RATE_LIMIT_MAX_ATTEMPTS, 2);
});
