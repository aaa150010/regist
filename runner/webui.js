(function profileRunnerWebUi() {
  'use strict';

  const dom = Object.fromEntries([
    'accountCount', 'accountFile', 'accountsText', 'chromePath', 'cleanupProfiles',
    'concurrency', 'diagnosticButton', 'failedCount', 'formMessage', 'helperBaseUrl',
    'helperDot', 'helperState', 'importAccountsButton', 'jobRows', 'outputDir', 'profileRoot', 'proxyCount',
    'phoneActivationRetryRounds', 'phoneAutoReleaseOnStop', 'phoneCodePollIntervalSeconds',
    'phoneCodePollMaxRounds', 'phoneCodeTimeoutWindows', 'phoneCodeWaitSeconds',
    'phoneReplacementLimit', 'proxyDefaultProtocol', 'proxyPoolText', 'queuedCount', 'runBadge', 'runnerForm',
    'retryFailedAccountsButton', 'runningCount', 'runMeta', 'runTitle', 'showApiKey', 'smsBowerAcquirePriority',
    'querySmsBowerBalanceButton', 'smsBowerApiKey', 'smsBowerBalance', 'smsBowerCountryIds', 'smsBowerMaxPrice', 'smsBowerMinPrice',
    'smsBowerPreferredPrice', 'startButton', 'stopButton', 'successCount', 'timeoutMinutes',
    'totalCount', 'verificationResendCount', 'whatsappRestartEnabled',
    'whatsappRestartMaxAttempts',
  ].map((id) => [id, document.getElementById(id)]));

  const STATUS_LABELS = Object.freeze({
    complete: '已完成',
    failed: '失败',
    queued: '排队',
    running: '运行中',
    starting: '启动中',
    stopped: '已停止',
    stopping: '停止中',
    success: '成功',
  });

  let csrfToken = '';
  let config = null;
  let currentRun = null;
  let pollTimer = null;
  let saveTimer = null;
  let initialized = false;
  let accountPool = { total: 0, available: 0, used: 0, invalid: 0, running: 0, entries: [] };

  function countNonEmptyLines(value) {
    return String(value || '').split(/\r?\n/).filter((line) => line.trim() && !line.trim().startsWith('#')).length;
  }

  function getImportedAccountEmails(value) {
    const emails = [];
    const seen = new Set();
    String(value || '').split(/\r?\n/).forEach((rawLine) => {
      const line = rawLine.trim();
      if (!line || line.startsWith('#')) return;
      const parts = line.split('----').map((part) => part.trim());
      if (parts.length !== 3 && parts.length !== 4) return;
      const email = parts[0].toLowerCase();
      const clientId = parts.length === 3 ? parts[1] : parts[2];
      const refreshToken = parts.length === 3 ? parts[2] : parts[3];
      if (!email.includes('@') || !clientId || !refreshToken || seen.has(email)) return;
      seen.add(email);
      emails.push(email);
    });
    return emails;
  }

  function refreshInputCounts() {
    const accountEmails = getImportedAccountEmails(dom.accountsText.value);
    const proxies = countNonEmptyLines(dom.proxyPoolText.value);
    const draftText = accountEmails.length ? ` · 待导入 ${accountEmails.length}` : '';
    dom.accountCount.textContent = [
      `总数 ${accountPool.total || 0}`,
      `可用 ${accountPool.available || 0}`,
      `已导出 ${accountPool.used || 0}`,
      `无效 ${accountPool.invalid || 0}`,
      accountPool.running ? `运行中 ${accountPool.running}` : '',
    ].filter(Boolean).join(' · ') + draftText;
    dom.proxyCount.textContent = `${proxies} 条代理`;
  }

  function showMessage(message, tone = 'error') {
    const text = String(message || '').trim();
    dom.formMessage.hidden = !text;
    dom.formMessage.textContent = text;
    dom.formMessage.className = `form-message ${tone}`;
  }

  function setServiceStatus(helper) {
    const healthy = Boolean(helper?.healthy);
    dom.helperDot.className = `service-dot ${healthy ? 'ok' : 'error'}`;
    dom.helperState.textContent = healthy
      ? `Hotmail helper 已连接 · ${helper.baseUrl}`
      : `Hotmail helper 不可用${helper?.error ? ` · ${helper.error}` : ''}`;
  }

  function readNonSecretPreferences() {
    try {
      return JSON.parse(localStorage.getItem('gujumpgate-profile-runner-prefs') || '{}');
    } catch (_) {
      return {};
    }
  }

  function persistNonSecretPreferences() {
    const value = {
      chromePath: dom.chromePath.value,
      cleanupProfiles: dom.cleanupProfiles.checked,
      concurrency: dom.concurrency.value,
      helperBaseUrl: dom.helperBaseUrl.value,
      outputDir: dom.outputDir.value,
      profileRoot: dom.profileRoot.value,
      proxyDefaultProtocol: dom.proxyDefaultProtocol.value,
      phoneActivationRetryRounds: dom.phoneActivationRetryRounds.value,
      phoneAutoReleaseOnStop: dom.phoneAutoReleaseOnStop.checked,
      phoneCodePollIntervalSeconds: dom.phoneCodePollIntervalSeconds.value,
      phoneCodePollMaxRounds: dom.phoneCodePollMaxRounds.value,
      phoneCodeTimeoutWindows: dom.phoneCodeTimeoutWindows.value,
      phoneCodeWaitSeconds: dom.phoneCodeWaitSeconds.value,
      phoneReplacementLimit: dom.phoneReplacementLimit.value,
      smsBowerAcquirePriority: dom.smsBowerAcquirePriority.value,
      smsBowerCountryIds: Array.from(dom.smsBowerCountryIds.selectedOptions, (option) => Number(option.value)),
      smsBowerMaxPrice: dom.smsBowerMaxPrice.value,
      smsBowerMinPrice: dom.smsBowerMinPrice.value,
      smsBowerPreferredPrice: dom.smsBowerPreferredPrice.value,
      targetSuccessCount: dom.targetSuccessCount.value,
      timeoutMinutes: dom.timeoutMinutes.value,
      verificationResendCount: dom.verificationResendCount.value,
      whatsappRestartEnabled: dom.whatsappRestartEnabled.checked,
      whatsappRestartMaxAttempts: dom.whatsappRestartMaxAttempts.value,
    };
    localStorage.setItem('gujumpgate-profile-runner-prefs', JSON.stringify(value));
  }

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.method && options.method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      headers['X-Profile-Runner-Token'] = csrfToken;
    }
    const response = await fetch(path, { cache: 'no-store', ...options, headers });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload?.ok === false) {
      throw new Error(payload?.error || `请求失败：HTTP ${response.status}`);
    }
    return payload;
  }

  function buildRunPayload(includeSecrets = true) {
    return {
      accountsText: includeSecrets ? dom.accountsText.value : '',
      chromePath: dom.chromePath.value.trim(),
      cleanupProfiles: dom.cleanupProfiles.checked,
      concurrency: Number(dom.concurrency.value),
      helperBaseUrl: dom.helperBaseUrl.value.trim(),
      outputDir: dom.outputDir.value.trim(),
      profileRoot: dom.profileRoot.value.trim(),
      proxyDefaultProtocol: dom.proxyDefaultProtocol.value,
      proxyPoolText: includeSecrets ? dom.proxyPoolText.value : '',
      phoneActivationRetryRounds: Number(dom.phoneActivationRetryRounds.value),
      phoneAutoReleaseOnStop: dom.phoneAutoReleaseOnStop.checked,
      phoneCodePollIntervalSeconds: Number(dom.phoneCodePollIntervalSeconds.value),
      phoneCodePollMaxRounds: Number(dom.phoneCodePollMaxRounds.value),
      phoneCodeTimeoutWindows: Number(dom.phoneCodeTimeoutWindows.value),
      phoneCodeWaitSeconds: Number(dom.phoneCodeWaitSeconds.value),
      phoneReplacementLimit: Number(dom.phoneReplacementLimit.value),
      smsBowerApiKey: includeSecrets ? dom.smsBowerApiKey.value.trim() : '',
      smsBowerAcquirePriority: dom.smsBowerAcquirePriority.value,
      smsBowerCountryIds: Array.from(dom.smsBowerCountryIds.selectedOptions, (option) => Number(option.value)),
      smsBowerMaxPrice: dom.smsBowerMaxPrice.value.trim(),
      smsBowerMinPrice: dom.smsBowerMinPrice.value.trim(),
      smsBowerPreferredPrice: dom.smsBowerPreferredPrice.value.trim(),
      timeoutMinutes: Number(dom.timeoutMinutes.value),
      verificationResendCount: Number(dom.verificationResendCount.value),
      whatsappRestartEnabled: dom.whatsappRestartEnabled.checked,
      whatsappRestartMaxAttempts: Number(dom.whatsappRestartMaxAttempts.value),
    };
  }

  function setRunControls(run) {
    const active = run?.status === 'running' || run?.status === 'stopping';
    dom.startButton.disabled = active || Boolean(config?.diagnosticOnly);
    dom.diagnosticButton.disabled = active;
    dom.stopButton.disabled = !currentRun;
  }

  function setCell(row, value, className = '') {
    const cell = document.createElement('td');
    cell.textContent = String(value ?? '');
    if (className) cell.className = className;
    row.appendChild(cell);
    return cell;
  }

  function renderJobs(jobs) {
    dom.jobRows.replaceChildren();
    if (!Array.isArray(jobs) || !jobs.length) {
      const row = document.createElement('tr');
      row.className = 'empty-row';
      const cell = document.createElement('td');
      cell.colSpan = 8;
      cell.textContent = '暂无任务';
      row.appendChild(cell);
      dom.jobRows.appendChild(row);
      return;
    }

    jobs.forEach((job) => {
      const row = document.createElement('tr');
      setCell(row, job.index);
      setCell(row, job.email);
      setCell(row, job.workerId || '-');
      setCell(row, job.proxy || '-', job.proxy === '直连' ? 'muted' : '');
      setCell(row, job.exitIp || '-', job.exitIp ? '' : 'muted');
      setCell(row, job.currentNode || job.phase || '-');
      const statusCell = document.createElement('td');
      const pill = document.createElement('span');
      pill.className = `status-pill ${job.status || ''}`;
      pill.textContent = STATUS_LABELS[job.status] || job.status || '-';
      statusCell.appendChild(pill);
      row.appendChild(statusCell);
      const result = job.outputFile || job.reason || (job.proxyGuardPassed ? '出口 IP 已放行' : '-');
      setCell(row, result, job.outputFile ? 'result-path' : (job.reason ? 'result-error' : 'muted'));
      dom.jobRows.appendChild(row);
    });
  }

  function renderRun(run) {
    currentRun = run || null;
    refreshInputCounts();
    setRunControls(currentRun);
    const counts = currentRun?.counts || {};
    dom.totalCount.textContent = String(currentRun?.total || 0);
    dom.queuedCount.textContent = String(counts.queued || 0);
    dom.runningCount.textContent = String((counts.running || 0) + (counts.starting || 0));
    dom.successCount.textContent = String(counts.success || 0);
    dom.failedCount.textContent = String((counts.failed || 0) + (counts.stopped || 0));
    renderJobs(currentRun?.jobs || []);

    if (!currentRun) {
      dom.runTitle.textContent = '等待启动';
      dom.runMeta.textContent = '导入账号、代理和 SMSBower Key 后启动';
      dom.runBadge.textContent = '空闲';
      dom.runBadge.className = 'run-badge idle';
      return;
    }
    const isDiagnostic = currentRun.mode === 'diagnostic';
    dom.runTitle.textContent = isDiagnostic ? 'Profile 隔离自检' : `批次 ${currentRun.id}`;
    dom.runMeta.textContent = `${currentRun.concurrency} 并发 · ${currentRun.elapsedSeconds} 秒 · ${currentRun.outputDir}`;
    dom.runBadge.textContent = STATUS_LABELS[currentRun.status] || currentRun.status;
    dom.runBadge.className = `run-badge ${currentRun.status}`;
  }

  async function refreshStatus() {
    try {
      const payload = await api('/api/status');
      if (payload.accountPool) accountPool = payload.accountPool;
      setServiceStatus(payload.helper);
      renderRun(payload.run);
    } catch (error) {
      setServiceStatus({ healthy: false, error: error.message });
    }
  }

  async function startRun(event) {
    event.preventDefault();
    showMessage('');
    persistNonSecretPreferences();
    dom.startButton.disabled = true;
    try {
      const payload = await api('/api/run/start', {
        method: 'POST',
        body: JSON.stringify(buildRunPayload(true)),
      });
      showMessage('批次已启动。敏感配置仅在本机处理，不会进入 Chrome 命令行或状态接口。', 'success');
      renderRun(payload.run);
    } catch (error) {
      showMessage(error.message);
      setRunControls(currentRun);
    }
  }

  async function startDiagnostic() {
    showMessage('');
    persistNonSecretPreferences();
    dom.diagnosticButton.disabled = true;
    try {
      const payload = await api('/api/diagnostics/start', {
        method: 'POST',
        body: JSON.stringify(buildRunPayload(false)),
      });
      showMessage('隔离自检已启动。所有 Profile 会在同步屏障后互相校验 storage 与 Cookie。', 'info');
      renderRun(payload.run);
    } catch (error) {
      showMessage(error.message);
      setRunControls(currentRun);
    }
  }

  async function stopRun() {
    dom.stopButton.disabled = true;
    try {
      const payload = await api('/api/workers/close-all', {
        method: 'POST',
        body: JSON.stringify({}),
      });
      const closedCount = Number(payload.closedCount || 0);
      showMessage(`已请求关闭 ${closedCount} 个由本调度器启动的 Chrome Worker。`, 'info');
      renderRun(payload.run);
    } catch (error) {
      showMessage(error.message);
      setRunControls(currentRun);
    }
  }

  async function loadAccountFile() {
    const file = dom.accountFile.files?.[0];
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) {
      showMessage('账号文件不能超过 5 MB。');
      return;
    }
    dom.accountsText.value = await file.text();
    dom.accountFile.value = '';
    refreshInputCounts();
  }

  async function saveSettingsToFile(silent = false) {
    persistNonSecretPreferences();
    try {
      await api('/api/settings/save', {
        method: 'POST',
        body: JSON.stringify(buildRunPayload(true)),
      });
      if (!silent) showMessage('配置已保存到本机配置文件。', 'success');
      return true;
    } catch (error) {
      if (!silent) showMessage(error.message);
      return false;
    }
  }

  function scheduleSettingsSave() {
    if (!initialized) return;
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      saveTimer = null;
      saveSettingsToFile(true);
    }, 700);
  }

  async function importAccounts() {
    const accountsText = dom.accountsText.value.trim();
    if (!accountsText) {
      showMessage('请先粘贴账号或选择 TXT 文件。');
      return;
    }
    dom.importAccountsButton.disabled = true;
    try {
      await saveSettingsToFile(true);
      const payload = await api('/api/accounts/import', {
        method: 'POST',
        body: JSON.stringify({ accountsText }),
      });
      accountPool = payload.pool || accountPool;
      dom.accountsText.value = '';
      refreshInputCounts();
      const details = [
        `新增 ${Number(payload.imported || 0)}`,
        `更新 ${Number(payload.updated || 0)}`,
        `已用跳过 ${Number(payload.skippedUsed || 0)}`,
      ].join(' · ');
      showMessage(`账户池导入完成：${details}。`, 'success');
    } catch (error) {
      showMessage(error.message);
    } finally {
      dom.importAccountsButton.disabled = false;
    }
  }

  async function retryFailedAccounts() {
    dom.retryFailedAccountsButton.disabled = true;
    try {
      const payload = await api('/api/accounts/retry-failed', {
        method: 'POST',
        body: JSON.stringify({}),
      });
      accountPool = payload.pool || accountPool;
      refreshInputCounts();
      showMessage(
        `已恢复 ${Number(payload.retried || 0)} 个可重试账号；跳过 ${Number(payload.skippedInvalidToken || 0)} 个无效邮箱令牌账号。`,
        'success'
      );
    } catch (error) {
      showMessage(error.message);
    } finally {
      dom.retryFailedAccountsButton.disabled = false;
    }
  }

  async function querySmsBowerBalance() {
    dom.querySmsBowerBalanceButton.disabled = true;
    dom.smsBowerBalance.textContent = '正在查询...';
    dom.smsBowerBalance.classList.remove('ok');
    try {
      await saveSettingsToFile(true);
      const payload = await api('/api/smsbower/balance', {
        method: 'POST',
        body: JSON.stringify({ smsBowerApiKey: dom.smsBowerApiKey.value.trim() }),
      });
      dom.smsBowerBalance.textContent = `$${payload.balance} ${payload.currency || 'USD'}`;
      dom.smsBowerBalance.classList.add('ok');
    } catch (error) {
      dom.smsBowerBalance.textContent = error.message;
    } finally {
      dom.querySmsBowerBalanceButton.disabled = false;
    }
  }

  async function initialize() {
    try {
      config = await api('/api/config');
      csrfToken = config.csrfToken;
      const saved = await api('/api/settings/load', {
        method: 'POST',
        body: JSON.stringify({}),
      });
      const prefs = { ...readNonSecretPreferences(), ...(saved.settings || {}) };
      dom.chromePath.value = prefs.chromePath || config.chromePath || '';
      dom.cleanupProfiles.checked = prefs.cleanupProfiles !== undefined ? Boolean(prefs.cleanupProfiles) : true;
      dom.concurrency.value = prefs.concurrency || config.defaultConcurrency || 10;
      dom.helperBaseUrl.value = prefs.helperBaseUrl || config.helperBaseUrl || '';
      dom.outputDir.value = prefs.outputDir || config.outputDir || '';
      dom.profileRoot.value = prefs.profileRoot || config.profileRoot || '';
      dom.proxyDefaultProtocol.value = prefs.proxyDefaultProtocol || 'http';
      dom.proxyPoolText.value = prefs.proxyPoolText || '';
      dom.phoneActivationRetryRounds.value = prefs.phoneActivationRetryRounds || 3;
      dom.phoneAutoReleaseOnStop.checked = prefs.phoneAutoReleaseOnStop !== undefined
        ? Boolean(prefs.phoneAutoReleaseOnStop)
        : true;
      dom.phoneCodePollIntervalSeconds.value = prefs.phoneCodePollIntervalSeconds || 3;
      dom.phoneCodePollMaxRounds.value = prefs.phoneCodePollMaxRounds || 4;
      dom.phoneCodeTimeoutWindows.value = prefs.phoneCodeTimeoutWindows || 2;
      dom.phoneCodeWaitSeconds.value = prefs.phoneCodeWaitSeconds || 45;
      dom.phoneReplacementLimit.value = prefs.phoneReplacementLimit || 3;
      dom.smsBowerAcquirePriority.value = prefs.smsBowerAcquirePriority || 'country';
      dom.smsBowerMaxPrice.value = Object.prototype.hasOwnProperty.call(prefs, 'smsBowerMaxPrice')
        ? prefs.smsBowerMaxPrice
        : '0.15';
      dom.smsBowerMinPrice.value = Object.prototype.hasOwnProperty.call(prefs, 'smsBowerMinPrice')
        ? prefs.smsBowerMinPrice
        : '0.03';
      dom.smsBowerPreferredPrice.value = prefs.smsBowerPreferredPrice ?? '';
      dom.smsBowerApiKey.value = prefs.smsBowerApiKey || '';
      if (Array.isArray(prefs.smsBowerCountryIds)) {
        const selectedCountryIds = new Set(prefs.smsBowerCountryIds.map(Number));
        Array.from(dom.smsBowerCountryIds.options).forEach((option) => {
          option.selected = selectedCountryIds.has(Number(option.value));
        });
      }
      dom.timeoutMinutes.value = prefs.timeoutMinutes || config.defaultTimeoutMinutes || 25;
      dom.verificationResendCount.value = prefs.verificationResendCount ?? 0;
      dom.whatsappRestartEnabled.checked = prefs.whatsappRestartEnabled !== undefined
        ? Boolean(prefs.whatsappRestartEnabled)
        : true;
      dom.whatsappRestartMaxAttempts.value = prefs.whatsappRestartMaxAttempts || 5;
      dom.concurrency.max = String(config.maxConcurrency || 10);
      setServiceStatus(config.helper);
      if (config.diagnosticOnly) {
        dom.startButton.disabled = true;
        showMessage('当前为仅隔离自检模式，账号 OAuth 启动接口已禁用。', 'info');
      }
      await refreshStatus();
      initialized = true;
      pollTimer = setInterval(refreshStatus, 1000);
    } catch (error) {
      showMessage(`WebUI 初始化失败：${error.message}`);
    }
  }

  dom.runnerForm.addEventListener('submit', startRun);
  dom.diagnosticButton.addEventListener('click', startDiagnostic);
  dom.stopButton.addEventListener('click', stopRun);
  dom.importAccountsButton.addEventListener('click', importAccounts);
  dom.retryFailedAccountsButton.addEventListener('click', retryFailedAccounts);
  dom.querySmsBowerBalanceButton.addEventListener('click', querySmsBowerBalance);
  dom.accountFile.addEventListener('change', loadAccountFile);
  dom.accountsText.addEventListener('input', refreshInputCounts);
  dom.proxyPoolText.addEventListener('input', refreshInputCounts);
  dom.runnerForm.addEventListener('input', (event) => {
    if (event.target !== dom.accountsText) scheduleSettingsSave();
  });
  dom.runnerForm.addEventListener('change', scheduleSettingsSave);
  dom.showApiKey.addEventListener('change', () => {
    dom.smsBowerApiKey.type = dom.showApiKey.checked ? 'text' : 'password';
  });
  window.addEventListener('beforeunload', () => {
    if (pollTimer) clearInterval(pollTimer);
    if (saveTimer) clearTimeout(saveTimer);
  });

  refreshInputCounts();
  initialize();
})();
