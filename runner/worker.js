(function profileRunnerWorker() {
  'use strict';

  const POLL_INTERVAL_MS = 1500;
  const MESSAGE_RETRY_ATTEMPTS = 30;
  const MESSAGE_RETRY_DELAY_MS = 400;
  const SMSBOWER_COUNTRIES = Object.freeze([
    { id: 33, label: 'Colombia' },
    { id: 151, label: 'Chile' },
    { id: 73, label: 'Brazil' },
    { id: 16, label: 'United Kingdom' },
    { id: 31, label: 'South Africa' },
    { id: 4, label: 'Philippines' },
    { id: 6, label: 'Indonesia' },
    { id: 187, label: 'USA Physical' },
    { id: 46, label: 'Sweden' },
    { id: 117, label: 'Portugal' },
  ]);

  const dom = {
    accountEmail: document.getElementById('accountEmail'),
    currentNode: document.getElementById('currentNode'),
    flowPhase: document.getElementById('flowPhase'),
    outputFile: document.getElementById('outputFile'),
    statusBand: document.querySelector('.status-band'),
    statusDetail: document.getElementById('statusDetail'),
    statusTitle: document.getElementById('statusTitle'),
    workerIdentity: document.getElementById('workerIdentity'),
    workerLogs: document.getElementById('workerLogs'),
  };

  let callbackBaseUrl = '';
  let config = null;
  let terminalReported = false;
  let pollTimer = null;

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function setStatus(title, detail, tone = '') {
    dom.statusTitle.textContent = title || '运行中';
    dom.statusDetail.textContent = detail || '';
    dom.statusBand.classList.remove('success', 'error');
    if (tone) dom.statusBand.classList.add(tone);
  }

  function appendLocalLog(message) {
    const text = String(message || '').trim();
    if (!text) return;
    const item = document.createElement('li');
    item.textContent = text;
    dom.workerLogs.prepend(item);
    while (dom.workerLogs.children.length > 8) {
      dom.workerLogs.lastElementChild.remove();
    }
  }

  function normalizeLogMessage(entry) {
    if (typeof entry === 'string') return entry.trim();
    if (!entry || typeof entry !== 'object') return '';
    return String(entry.message || entry.text || entry.detail || '').trim();
  }

  function getRecentLogs(state) {
    return (Array.isArray(state?.logs) ? state.logs : [])
      .slice(-8)
      .map(normalizeLogMessage)
      .filter(Boolean);
  }

  function getCurrentAccountStatus(state) {
    const statuses = state?.existingPlusAccountStatuses;
    if (!statuses || typeof statuses !== 'object') return null;
    if (config?.account?.id && statuses[config.account.id]) {
      return statuses[config.account.id];
    }
    const expectedEmail = String(config?.account?.email || '').trim().toLowerCase();
    return Object.values(statuses).find((entry) => (
      String(entry?.email || '').trim().toLowerCase() === expectedEmail
    )) || null;
  }

  function getFailureReason(state, accountStatus) {
    if (accountStatus?.reason) return String(accountStatus.reason);
    const summaries = Array.isArray(state?.autoRunRoundSummaries)
      ? state.autoRunRoundSummaries
      : [];
    const lastSummary = summaries[summaries.length - 1] || null;
    if (lastSummary?.finalFailureReason) return String(lastSummary.finalFailureReason);
    const failureReasons = Array.isArray(lastSummary?.failureReasons)
      ? lastSummary.failureReasons
      : [];
    if (failureReasons.length) return String(failureReasons[failureReasons.length - 1]);
    const recentLogs = getRecentLogs(state);
    return recentLogs[recentLogs.length - 1] || '流程结束，但没有生成 JSON 文件。';
  }

  function summarizeState(state) {
    const accountStatus = getCurrentAccountStatus(state);
    const logs = getRecentLogs(state);
    const outputFile = String(
      accountStatus?.filePath
      || state?.existingPlusJsonFilePath
      || ''
    ).trim();
    return {
      accountStatus: accountStatus?.status || '',
      currentNodeId: String(state?.currentNodeId || ''),
      existingPlusJsonFilePath: outputFile,
      logs,
      nodeStatuses: state?.nodeStatuses && typeof state.nodeStatuses === 'object'
        ? state.nodeStatuses
        : {},
      phase: String(state?.autoRunPhase || 'idle'),
      reason: accountStatus?.reason ? String(accountStatus.reason) : '',
      running: Boolean(state?.autoRunning),
    };
  }

  async function postEvent(kind, payload = {}) {
    if (!callbackBaseUrl || !config?.jobToken) return null;
    const response = await fetch(`${callbackBaseUrl}/api/worker/event`, {
      method: 'POST',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        token: config.jobToken,
        kind,
        payload,
      }),
    });
    if (!response.ok) {
      throw new Error(`调度器事件上报失败：HTTP ${response.status}`);
    }
    return response.json();
  }

  async function sendRuntimeMessage(message) {
    let lastError = null;
    for (let attempt = 1; attempt <= MESSAGE_RETRY_ATTEMPTS; attempt += 1) {
      try {
        const response = await chrome.runtime.sendMessage(message);
        if (response?.error) throw new Error(response.error);
        return response;
      } catch (error) {
        lastError = error;
        if (attempt < MESSAGE_RETRY_ATTEMPTS) await sleep(MESSAGE_RETRY_DELAY_MS);
      }
    }
    throw lastError || new Error('扩展后台未响应。');
  }

  function summarizeProxyApplyResponse(response) {
    const routing = response?.proxyRouting || null;
    const state = response?.state || {};
    return [
      `ok=${response?.ok === true ? '1' : '0'}`,
      `routing=${routing ? (routing.reason || (routing.applied ? 'applied' : 'unknown')) : 'missing'}`,
      `enabled=${state?.ipProxyEnabled ? '1' : '0'}`,
      `locked=${state?.profileRunnerProxyLocked ? '1' : '0'}`,
      `host=${state?.ipProxyHost ? 'set' : 'empty'}`,
      `port=${state?.ipProxyPort ? 'set' : 'empty'}`,
      `protocol=${String(state?.ipProxyProtocol || '').trim() || 'empty'}`,
    ].join(', ');
  }

  function buildSettingsPayload(workerConfig) {
    const smsSettings = workerConfig.smsSettings && typeof workerConfig.smsSettings === 'object'
      ? workerConfig.smsSettings
      : {};
    const selectedCountryIds = Array.isArray(smsSettings.countryIds)
      ? smsSettings.countryIds.map(Number)
      : SMSBOWER_COUNTRIES.map((entry) => entry.id);
    const countryById = new Map(SMSBOWER_COUNTRIES.map((entry) => [entry.id, entry]));
    const countries = selectedCountryIds
      .map((countryId) => countryById.get(countryId))
      .filter(Boolean)
      .map((entry) => ({ ...entry }));
    if (!countries.length) {
      throw new Error('SMSBower 国家优先级为空，无法启动接码流程。');
    }
    const proxy = workerConfig.proxy || null;
    return {
      activeFlowId: 'openai',
      panelMode: 'existing-plus-oauth-json',
      existingPlusAccountsText: '',
      existingPlusJsonOutputDir: workerConfig.outputDir,
      plusModeEnabled: true,
      plusAccountAccessStrategy: 'oauth',
      mailProvider: 'hotmail',
      hotmailServiceMode: 'local',
      hotmailLocalBaseUrl: workerConfig.helperBaseUrl,
      accountRunHistoryHelperBaseUrl: workerConfig.helperBaseUrl,
      phoneVerificationEnabled: true,
      phoneSmsProvider: 'smsbower',
      phoneSmsProviderOrder: ['smsbower'],
      phoneSmsReuseEnabled: false,
      heroSmsReuseEnabled: false,
      freePhoneReuseEnabled: false,
      freePhoneReuseAutoEnabled: false,
      smsBowerApiKey: workerConfig.smsBowerApiKey,
      smsBowerBaseUrl: 'https://smsbower.app/stubs/handler_api.php',
      smsBowerServiceCode: 'dr',
      smsBowerCountryId: countries[0].id,
      smsBowerCountryLabel: countries[0].label,
      smsBowerCountryFallback: countries.slice(1),
      smsBowerMinPrice: String(smsSettings.minPrice || ''),
      smsBowerMaxPrice: String(smsSettings.maxPrice || ''),
      smsBowerPreferredPrice: String(smsSettings.preferredPrice || ''),
      heroSmsAcquirePriority: String(smsSettings.acquirePriority || 'country'),
      heroSmsCountryId: countries[0].id,
      heroSmsCountryLabel: countries[0].label,
      heroSmsCountryFallback: countries.slice(1),
      verificationResendCount: Number(smsSettings.verificationResendCount ?? 0),
      phoneVerificationReplacementLimit: Number(smsSettings.replacementLimit ?? 3),
      whatsappPhoneVerificationRestartEnabled: smsSettings.whatsappRestartEnabled !== false,
      whatsappPhoneVerificationRestartMaxAttempts: Number(smsSettings.whatsappRestartMaxAttempts ?? 5),
      phoneCodeWaitSeconds: Number(smsSettings.codeWaitSeconds ?? 45),
      phoneCodeTimeoutWindows: Number(smsSettings.timeoutWindows ?? 2),
      phoneCodePollIntervalSeconds: Number(smsSettings.pollIntervalSeconds ?? 3),
      phoneCodePollMaxRounds: Number(smsSettings.pollMaxRounds ?? 4),
      heroSmsActivationRetryRounds: Number(smsSettings.activationRetryRounds ?? 3),
      phoneAutoReleaseOnStopEnabled: smsSettings.autoReleaseOnStop !== false,
      profileRunnerProxyLocked: Boolean(proxy),
      ipProxyEnabled: Boolean(proxy),
      ipProxyService: '711proxy',
      ipProxyMode: 'account',
      ipProxyHost: proxy?.host || '',
      ipProxyPort: proxy?.port ? String(proxy.port) : '',
      ipProxyProtocol: proxy?.protocol || 'http',
      ipProxyUsername: proxy?.username || '',
      ipProxyPassword: proxy?.password || '',
      ipProxyAccountList: '',
      ipProxyAutoSyncEnabled: false,
      autoRunDelayEnabled: false,
      operationDelayEnabled: false,
      autoStepDelaySeconds: 0,
      autoRunSkipFailures: false,
      autoRunRetryNonFreeTrial: false,
      autoRunRetryPaypalCallback: false,
      autoRunRetryShortLinkError: false,
    };
  }

  async function configureAndStart() {
    setStatus('配置 Profile', '正在写入当前 worker 的独立扩展存储');
    appendLocalLog('重置当前 Profile 的运行状态');
    await sendRuntimeMessage({ type: 'RESET', source: 'profile-runner' });

    const settingsPayload = buildSettingsPayload(config);
    const saveWorkerSettings = () => sendRuntimeMessage({
      type: 'SAVE_SETTING',
      source: 'profile-runner',
      payload: settingsPayload,
    });
    let settingsResponse = await saveWorkerSettings();

    if (config.proxy) {
      setStatus('检测独立出口', `正在检测 ${config.proxy.label || '当前代理'} 的实际出口 IP`);
      appendLocalLog('代理已写入当前 Profile，开始出口探测');
      let routing = null;
      let exitIp = '';
      let proxyFailure = null;
      for (let attempt = 1; attempt <= 2; attempt += 1) {
        if (attempt > 1) {
          appendLocalLog('代理初始化状态发生变化，正在重新写入并复测');
          await sleep(500);
          settingsResponse = await saveWorkerSettings();
        }
        const applied = settingsResponse?.proxyRouting;
        if (applied && !applied.applied) {
          proxyFailure = Object.assign(
            new Error(applied?.error || `扩展未能应用当前 worker 的代理（${summarizeProxyApplyResponse(settingsResponse)}）。`),
            { workerPhase: 'proxy_apply' }
          );
          continue;
        }
        if (!applied) {
          appendLocalLog(`保存设置未返回代理应用状态，改由出口探测接口应用代理（${summarizeProxyApplyResponse(settingsResponse)}）`);
        }
        try {
          const probeResponse = await sendRuntimeMessage({
            type: 'PROBE_IP_PROXY_EXIT',
            source: 'profile-runner',
            payload: {
              timeoutMs: 15000,
              avoidOpenAiEndpoints: true,
              skipTargetReachability: true,
            },
          });
          routing = probeResponse?.proxyRouting || {};
          exitIp = String(routing.exitIp || '').trim();
          if (routing.applied && exitIp && routing.reason !== 'connectivity_failed') {
            proxyFailure = null;
            break;
          }
          proxyFailure = Object.assign(
            new Error(routing.exitError || routing.error || '代理出口探测失败。'),
            { workerPhase: 'proxy_probe' }
          );
        } catch (error) {
          proxyFailure = Object.assign(error, { workerPhase: 'proxy_probe' });
        }
      }
      if (proxyFailure || !routing || !exitIp) {
        throw proxyFailure || Object.assign(
          new Error('代理出口探测失败。'),
          { workerPhase: 'proxy_probe' }
        );
      }
      const guard = await postEvent('proxy-ready', {
        proxyId: config.proxy.id,
        exitIp,
        exitRegion: String(routing.exitRegion || ''),
      });
      if (!guard?.proxyRelease) {
        throw Object.assign(
          new Error(guard?.reason || `出口 IP ${exitIp} 未通过并发去重保护。`),
          { workerPhase: 'proxy_guard' }
        );
      }
      appendLocalLog(`出口 ${exitIp} 已通过并发唯一性校验`);
    } else {
      appendLocalLog('当前为单并发直连模式');
    }

    const account = {
      id: config.account.id,
      email: config.account.email,
      clientId: config.account.clientId,
      refreshToken: config.account.refreshToken,
      password: config.account.password || '',
      status: 'authorized',
      enabled: true,
      used: false,
      lastError: '',
    };
    await sendRuntimeMessage({
      type: 'UPSERT_HOTMAIL_ACCOUNT',
      source: 'profile-runner',
      payload: account,
    });

    const preparedState = await sendRuntimeMessage({ type: 'GET_STATE', source: 'profile-runner' });
    const matchingAccount = (preparedState?.hotmailAccounts || []).find((item) => item?.id === account.id);
    if (!matchingAccount?.refreshToken || matchingAccount.used) {
      throw new Error('账号写入当前 Profile 后校验失败。');
    }

    setStatus('启动 OAuth', '扩展即将打开 OpenAI OAuth 登录页');
    appendLocalLog('账号与 SMSBower 设置已写入，开始自动流程');
    await postEvent('started', { phase: 'starting' });
    await sendRuntimeMessage({
      type: 'AUTO_RUN',
      source: 'profile-runner',
      payload: {
        totalRuns: 1,
        mode: 'restart',
        autoRunSkipFailures: false,
        autoRunRetryNonFreeTrial: false,
        autoRunRetryPaypalCallback: false,
        autoRunRetryShortLinkError: false,
      },
    });
    pollTimer = setInterval(() => pollState().catch(handlePollingError), POLL_INTERVAL_MS);
    await pollState();
  }

  async function reportTerminal(kind, payload) {
    if (terminalReported) return;
    terminalReported = true;
    if (pollTimer) clearInterval(pollTimer);
    const success = kind === 'success';
    setStatus(
      success ? '导出成功' : '任务失败',
      success ? (payload.outputFile || 'JSON 已生成') : (payload.reason || '自动流程失败'),
      success ? 'success' : 'error'
    );
    await postEvent(kind, payload);
  }

  async function pollState() {
    if (terminalReported) return;
    const state = await sendRuntimeMessage({ type: 'GET_STATE', source: 'profile-runner' });
    const summary = summarizeState(state);
    dom.flowPhase.textContent = summary.phase || '-';
    dom.currentNode.textContent = summary.currentNodeId || '-';
    dom.outputFile.textContent = summary.existingPlusJsonFilePath || '-';
    if (summary.logs.length) {
      dom.workerLogs.replaceChildren();
      summary.logs.slice().reverse().forEach(appendLocalLog);
    }
    setStatus('运行中', summary.logs[summary.logs.length - 1] || `阶段：${summary.phase}`);
    await postEvent('heartbeat', summary);

    if (
      summary.phase === 'complete'
      && summary.accountStatus === 'success'
      && summary.existingPlusJsonFilePath
    ) {
      await reportTerminal('success', {
        outputFile: summary.existingPlusJsonFilePath,
        phase: summary.phase,
      });
      return;
    }

    if (summary.phase === 'stopped' || summary.phase === 'complete') {
      await reportTerminal('failed', {
        phase: summary.phase,
        reason: getFailureReason(state, getCurrentAccountStatus(state)),
      });
    }
  }

  async function handlePollingError(error) {
    appendLocalLog(`状态轮询异常：${error?.message || error}`);
  }

  async function runIsolationDiagnostic() {
    const marker = String(config.diagnosticMarker || '');
    setStatus('隔离自检', '正在校验扩展存储、Cookie 和标签页作用域');
    await sendRuntimeMessage({ type: 'RESET', source: 'profile-runner-diagnostic' });
    const runtimeState = await sendRuntimeMessage({ type: 'GET_STATE', source: 'profile-runner-diagnostic' });
    await chrome.storage.local.set({ profileRunnerIsolationMarker: marker });
    const stored = await chrome.storage.local.get('profileRunnerIsolationMarker');

    const cookieUrl = `${callbackBaseUrl}/runner-isolation-cookie`;
    await chrome.cookies.set({
      url: cookieUrl,
      name: 'gujumpgate_profile_runner_marker',
      value: marker,
      path: '/',
      expirationDate: Math.floor(Date.now() / 1000) + 600,
    });
    await postEvent('diagnostic-ready', { marker });
    const releaseDeadline = Date.now() + 45000;
    let released = false;
    while (Date.now() < releaseDeadline) {
      const response = await fetch(
        `${callbackBaseUrl}/api/worker/diagnostic-release/${encodeURIComponent(config.jobToken)}`,
        { cache: 'no-store' }
      );
      const payload = await response.json().catch(() => ({}));
      if (response.ok && payload?.release) {
        released = true;
        break;
      }
      await sleep(250);
    }
    if (!released) throw new Error('隔离自检同步屏障超时。');
    const cookie = await chrome.cookies.get({
      url: cookieUrl,
      name: 'gujumpgate_profile_runner_marker',
    });
    const tabs = await chrome.tabs.query({});
    const passed = stored.profileRunnerIsolationMarker === marker
      && cookie?.value === marker
      && chrome.runtime.id === config.expectedExtensionId
      && runtimeState?.automationWindowId !== 0;
    const payload = {
      diagnostic: true,
      marker,
      storageMarker: stored.profileRunnerIsolationMarker || '',
      cookieMarker: cookie?.value || '',
      extensionId: chrome.runtime.id,
      tabCount: tabs.length,
      automationWindowId: runtimeState?.automationWindowId ?? null,
      passed,
      reason: passed ? '' : 'Profile 隔离标记或自动任务窗口状态不一致。',
    };
    await reportTerminal(passed ? 'success' : 'failed', payload);
  }

  async function loadConfig() {
    const params = new URLSearchParams(location.search);
    const token = String(params.get('token') || '').trim();
    const callback = String(params.get('callback') || '').trim();
    if (!token || !/^http:\/\/127\.0\.0\.1:\d+$/.test(callback)) {
      throw new Error('Worker 启动参数无效。');
    }
    callbackBaseUrl = callback;
    const response = await fetch(`${callbackBaseUrl}/api/worker/config/${encodeURIComponent(token)}`, {
      cache: 'no-store',
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload?.ok || !payload?.config) {
      throw new Error(payload?.error || `无法读取任务配置：HTTP ${response.status}`);
    }
    config = payload.config;
    if (config.expectedExtensionId && config.expectedExtensionId !== chrome.runtime.id) {
      throw new Error(`扩展 ID 不一致：当前 ${chrome.runtime.id}`);
    }
  }

  async function main() {
    try {
      await loadConfig();
      dom.workerIdentity.textContent = `Worker ${config.workerId} / Job ${config.jobIndex}`;
      dom.accountEmail.textContent = config.account?.email || (config.mode === 'diagnostic' ? '隔离自检' : '-');
      await postEvent('booted', {
        extensionId: chrome.runtime.id,
        mode: config.mode,
      });
      if (config.mode === 'diagnostic') {
        await runIsolationDiagnostic();
        return;
      }
      await configureAndStart();
    } catch (error) {
      const reason = error?.message || String(error || 'Worker 初始化失败');
      appendLocalLog(reason);
      setStatus('启动失败', reason, 'error');
      if (config?.jobToken) {
        await reportTerminal('failed', {
          phase: String(error?.workerPhase || 'bootstrap'),
          reason,
        }).catch(() => {});
      }
    }
  }

  main();
})();
