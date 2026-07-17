(function attachExistingPlusRunPolicy(root, factory) {
  const api = factory();
  root.MultiPageExistingPlusRunPolicy = api;
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  }
})(typeof self !== 'undefined' ? self : globalThis, function createExistingPlusRunPolicy() {
  const AUTH_RATE_LIMIT_COOLDOWN_MS = 20000;
  const AUTH_RATE_LIMIT_MAX_ATTEMPTS = 2;

  function normalizeAccountId(value = '') {
    return String(value || '').trim();
  }

  function normalizeRuntimeStatus(value = '') {
    return String(value || '').trim().toLowerCase();
  }

  function getAttemptTimestamp(account = {}, runtimeStatus = {}) {
    const candidates = [
      runtimeStatus.updatedAt,
      runtimeStatus.finishedAt,
      runtimeStatus.startedAt,
      account.lastUsedAt,
      account.lastAuthAt,
    ];
    for (const value of candidates) {
      const timestamp = Number(value);
      if (Number.isFinite(timestamp) && timestamp > 0) {
        return timestamp;
      }
    }
    return 0;
  }

  function getCandidatePriority(account = {}, runtimeStatus = {}) {
    const status = normalizeRuntimeStatus(runtimeStatus.status);
    if (!status || status === 'pending') {
      return String(account.lastError || '').trim() ? 1 : 0;
    }
    return 1;
  }

  function rankExistingPlusHotmailCandidates(accounts = [], statuses = {}) {
    const statusMap = statuses && typeof statuses === 'object' && !Array.isArray(statuses)
      ? statuses
      : {};

    return (Array.isArray(accounts) ? accounts : [])
      .map((account, index) => {
        const accountId = normalizeAccountId(account?.id);
        const runtimeStatus = accountId && statusMap[accountId] && typeof statusMap[accountId] === 'object'
          ? statusMap[accountId]
          : {};
        return {
          account,
          index,
          runtimeStatus,
          priority: getCandidatePriority(account, runtimeStatus),
          attemptedAt: getAttemptTimestamp(account, runtimeStatus),
        };
      })
      .filter(({ account, runtimeStatus }) => (
        account
        && account.used !== true
        && Boolean(String(account.refreshToken || '').trim())
        && normalizeRuntimeStatus(runtimeStatus.status) !== 'success'
      ))
      .sort((left, right) => (
        left.priority - right.priority
        || left.attemptedAt - right.attemptedAt
        || left.index - right.index
      ))
      .map(({ account }) => account);
  }

  function isAuthRateLimitFailure(errorLike) {
    const message = String(
      typeof errorLike === 'string'
        ? errorLike
        : (errorLike?.message || errorLike || '')
    ).trim();
    return /RATE_LIMIT_EXCEEDED::|rate_limit_exceeded(?:_page)?|认证页[^\n]*(?:请求过多|请求次数过多|限流)/i.test(message);
  }

  function normalizeSmsBowerCountryEntry(entry = {}, fallbackId = 0, fallbackLabel = '') {
    const rawId = entry && typeof entry === 'object' ? (entry.id ?? entry.countryId) : entry;
    const parsedId = Math.floor(Number(rawId));
    const parsedFallbackId = Math.floor(Number(fallbackId));
    const id = Number.isFinite(parsedId) && parsedId > 0
      ? parsedId
      : (Number.isFinite(parsedFallbackId) && parsedFallbackId > 0 ? parsedFallbackId : 0);
    const rawLabel = entry && typeof entry === 'object'
      ? (entry.label ?? entry.countryLabel)
      : '';
    const label = String(rawLabel || fallbackLabel || '').trim() || (id ? `Country #${id}` : '');
    return id ? { id, label } : null;
  }

  function normalizeSmsBowerCountryList(value = []) {
    const source = Array.isArray(value) ? value : [];
    const seen = new Set();
    const normalized = [];
    for (const entry of source) {
      const parsed = normalizeSmsBowerCountryEntry(entry);
      if (!parsed || seen.has(parsed.id)) continue;
      seen.add(parsed.id);
      normalized.push(parsed);
    }
    return normalized;
  }

  function resolveSmsBowerCountrySelection(state = {}, defaultOrder = []) {
    const defaults = normalizeSmsBowerCountryList(defaultOrder);
    const defaultPrimary = defaults[0] || { id: 33, label: 'Colombia' };
    const configuredPrimary = normalizeSmsBowerCountryEntry({
      id: state.smsBowerCountryId ?? state.heroSmsCountryId,
      label: state.smsBowerCountryLabel || state.heroSmsCountryLabel,
    }, defaultPrimary.id, defaultPrimary.label) || defaultPrimary;
    const configuredFallback = state.smsBowerCountryFallback !== undefined
      ? state.smsBowerCountryFallback
      : (state.heroSmsCountryFallback !== undefined ? state.heroSmsCountryFallback : defaults.slice(1));
    const fallback = normalizeSmsBowerCountryList(configuredFallback)
      .filter((entry) => entry.id !== configuredPrimary.id);
    return {
      id: configuredPrimary.id,
      label: configuredPrimary.label,
      fallback,
    };
  }

  return {
    AUTH_RATE_LIMIT_COOLDOWN_MS,
    AUTH_RATE_LIMIT_MAX_ATTEMPTS,
    isAuthRateLimitFailure,
    rankExistingPlusHotmailCandidates,
    resolveSmsBowerCountrySelection,
  };
});
