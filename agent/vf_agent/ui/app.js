// VoiceForever agent window. Talks to Python through window.pywebview.api (see gui/api.py);
// Python pushes connection, game and message events into window.vf.onEvent.
// Every word on screen comes from t(): Python sends the strings of the chosen language
// (ui/locales/<code>.json), and the page redraws itself when the language changes.
'use strict';

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  function h(tag, props = {}, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (key === 'class') el.className = value;
      else if (key === 'style') el.style.cssText = value;
      else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
      else if (value === true) el.setAttribute(key, '');
      else if (value !== false && value != null) el.setAttribute(key, value);
    }
    for (const child of children.flat()) if (child != null) el.append(child);
    return el;
  }

  // Static icon markup only; text from Python or the server always goes through textContent.
  function icon(name) {
    const tpl = document.createElement('template');
    tpl.innerHTML = ICONS[name];
    return tpl.content.firstElementChild;
  }

  const ICONS = {
    ok: '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3 3 7-7" fill="none" stroke="#5ed474" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    wait: '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 2h7M4.5 14h7M5 2.5c0 3 6 3.5 6 5.5s-6 2.5-6 5.5M11 2.5c0 3-6 3.5-6 5.5s6 2.5 6 5.5" fill="none" stroke="#f2ad42" stroke-width="1.6" stroke-linecap="round"/></svg>',
    bad: '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 3.5l9 9M12.5 3.5l-9 9" fill="none" stroke="#ff7563" stroke-width="2.2" stroke-linecap="round"/></svg>',
    remove: '<svg width="10" height="10" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
    warn: '<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true" style="flex: none"><path d="M9 2l7.5 13h-15z" fill="none" stroke="#f2ad42" stroke-width="1.6" stroke-linejoin="round"/><path d="M9 7v4M9 13.2v.3" stroke="#f2ad42" stroke-width="1.8" stroke-linecap="round"/></svg>',
    mic: '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><rect x="5.5" y="1.5" width="5" height="8.5" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M3.5 7.5a4.5 4.5 0 0 0 9 0M8 12v2.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
    info: '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.6" fill="none" stroke="currentColor" stroke-width="1.3"/><circle cx="8" cy="4.9" r="1" fill="currentColor"/><path d="M8 7.2v4.4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    tg: '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M14.5 2L1.5 7.2l4.3 1.6L12 4.5 7.2 9.6v4l2.4-2.6 3 2.2z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>',
  };

  // ------------------------------------------------------------------ words
  let L = {}; // the strings of the current language

  function t(key, values) {
    const text = L[key] ?? key;
    return values ? text.replace(/\{(\w+)\}/g, (m, name) => (name in values ? String(values[name]) : m)) : text;
  }

  // The same, where a {placeholder} may be a node: a bold button name inside a sentence.
  function tNodes(key, values) {
    return (L[key] ?? key).split(/(\{\w+\})/).filter(Boolean).map((part) => {
      const m = /^\{(\w+)\}$/.exec(part);
      return m && m[1] in values ? values[m[1]] : part;
    });
  }

  const cap = (text) => (text ? text[0].toUpperCase() + text.slice(1) : '');

  const STATES = {
    connected: { c1: '#5ed474', c2: '#1d6b2c', glow: 'rgba(94,212,116,.55)' },
    connecting: { c1: '#f2ad42', c2: '#7a4a0c', glow: 'rgba(242,173,66,.5)', pulse: true },
    reconnecting: { c1: '#f2ad42', c2: '#7a4a0c', glow: 'rgba(242,173,66,.45)' },
    disconnected: { c1: '#a59a88', c2: '#3a342b', glow: 'rgba(0,0,0,0)' },
    auth_failed: { c1: '#ff7563', c2: '#7a1a10', glow: 'rgba(255,117,99,.5)' },
    replaced: { c1: '#f2ad42', c2: '#7a4a0c', glow: 'rgba(242,173,66,.35)' },
  };

  const GAME_COLORS = { active: '#5ed474', background: '#f2ad42', closed: '#a59a88' };

  const CHANNEL_NAMES = { 1: 'chan.general', 2: 'chan.trade' };

  function channelLabel(channel, target) {
    switch (channel) {
      case 'say': return [t('chan.say'), '#f3eee6'];
      case 'yell': return [t('chan.yell'), '#ff6a5a'];
      case 'emote': return [t('chan.emote'), '#ff9a5a'];
      case 'group': case 'party': return [t('chan.group'), '#aab4ff'];
      case 'raid': return [t('chan.raid'), '#ff9a4a'];
      case 'raid_warning': return [t('chan.raid_warning'), '#ff7a3a'];
      case 'instance': return [t('chan.instance'), '#ff9a4a'];
      case 'guild': return [t('chan.guild'), '#56e06a'];
      case 'officer': return [t('chan.officer'), '#48c858'];
      case 'whisper': return [target ? t('chan.whisper_to', { name: target }) : t('chan.whisper'), '#ff8ef0'];
      case 'reply': return [t('chan.reply'), '#ff8ef0'];
      case 'channel': return [CHANNEL_NAMES[target] ? `${target}. ${t(CHANNEL_NAMES[target])}` : t('chan.numbered', { n: target }), '#ffc8b0'];
      default: return [channel, '#f3eee6'];
    }
  }

  const KEY_LABELS = { ctrl: 'Ctrl', control: 'Ctrl', shift: 'Shift', alt: 'Alt', pageup: 'PageUp', pagedown: 'PageDown', backspace: 'Backspace', esc: 'Esc' };
  const keyLabel = (key) => KEY_LABELS[key] || (/^num\d$/.test(key) ? `Num ${key.slice(3)}` : key.length <= 3 ? key.toUpperCase() : key[0].toUpperCase() + key.slice(1));

  const CODE_KEYS = { Home: 'home', End: 'end', Insert: 'insert', Delete: 'delete', PageUp: 'pageup', PageDown: 'pagedown', Space: 'space', Tab: 'tab', Backspace: 'backspace', Enter: 'enter' };
  function keyFromCode(code) {
    let m;
    if ((m = /^F(\d{1,2})$/.exec(code)) && +m[1] >= 1 && +m[1] <= 24) return `f${m[1]}`;
    if ((m = /^Key([A-Z])$/.exec(code))) return m[1].toLowerCase();
    if ((m = /^Digit(\d)$/.exec(code))) return m[1];
    if ((m = /^Numpad(\d)$/.exec(code))) return `num${m[1]}`;
    return CODE_KEYS[code] || null;
  }

  // The same combo as a WoW binding for "/vf key": modifiers in WoW's order (ALT-CTRL-SHIFT).
  const WOW_MODIFIERS = ['alt', 'ctrl', 'shift'];
  const WOW_KEYS = { control: 'ctrl', return: 'enter', esc: 'escape' };
  function wowBinding(keys) {
    const names = keys.map((k) => WOW_KEYS[k] || k);
    const mods = WOW_MODIFIERS.filter((m) => names.includes(m));
    const rest = names.filter((k) => !WOW_MODIFIERS.includes(k)).map((k) => k.replace(/^num(\d)$/, 'numpad$1'));
    return [...mods, ...rest].join('-').toUpperCase();
  }

  const TEST_TEXT = 'VoiceForever test';

  // What became of a recording, when nothing reached the chat (VoiceResult.status): our own
  // words for the statuses we know, so they follow the window's language.
  const VOICE_FAILED = ['stt_error', 'bad_audio', 'too_long', 'unsupported', 'offline', 'timeout', 'error'];
  // Why the server's account service refused a recording (VoiceResult.reason).
  const ACCESS_REASONS = ['unknown', 'revoked', 'no_access', 'limit', 'rate', 'unavailable'];

  const LEVEL_BARS = 24;
  const HOT_BAR = 20; // from here the meter turns red: too loud

  // The in-game check of the push-to-talk button, in the order the agent reports it.
  const BIND_STEPS = ['waiting_game', 'waiting_focus', 'sent', 'reloading', 'done'];

  // ------------------------------------------------------------------ state
  const S = {
    ready: false,
    lang: 'en', // the language in use
    language: '', // the setting: '' follows Windows
    systemLang: 'en',
    languages: [], // [{ code, name }]
    version: '',
    errors: {}, // the problems shown next to the fields
    pending: [],
    seq: 0,
    configured: false,
    saved: null, // the settings as written to config.toml
    form: null, // the settings being edited (wizard and settings tab share it)
    defaults: null,
    configPath: '',
    status: { state: 'disconnected', detail: '', attempt: 0, retry_at: null, since: null },
    game: 'closed',
    history: [],
    view: 'status',
    wizard: { step: 1, check: 'idle', detail: '' },
    capturing: false,
    actionsFor: null,
    savedNote: '', // shown in the save bar right after saving
    pad: { connected: false, name: '', playstation: false, error: '' },
    recording: { state: 'idle', started_at: null, max_s: 0 },
    padButtons: [],
    mics: null, // microphone names Windows lists; null until asked
    micError: '',
    micTest: false, // the microphone is open only to show its level
    padCapture: false, // waiting for a press to assign the button
    padNote: '', // what came of the last assignment
    bind: { check: null, last: -1 }, // the in-game check: its last step, and the last one before an error
    wizardVoice: null, // the recording made on the last wizard step
    account: null, // plan and messages left, from servers with accounts
  };

  const TIP = { owner: null }; // the (i) whose explanation is showing

  const clone = (value) => JSON.parse(JSON.stringify(value));
  const sameSettings = (a, b) => Object.keys({ ...a, ...b }).every((k) => JSON.stringify(a[k]) === JSON.stringify(b[k]));
  const focusWait = () => Math.round((S.saved || S.form || {}).focus_wait_s || 15);
  const api = () => window.pywebview.api;

  async function call(name, ...args) {
    try {
      return await api()[name](...args);
    } catch (error) {
      console.error(name, error);
      return null;
    }
  }

  // ------------------------------------------------------------------ start
  async function init() {
    if (init.started || !window.pywebview || !window.pywebview.api) return;
    init.started = true;
    const state = await call('get_state');
    if (!state) return;
    S.seq = state.seq;
    S.configured = state.configured;
    S.saved = state.config;
    S.form = clone(state.config);
    S.defaults = state.defaults;
    S.configPath = state.config_path;
    S.status = state.status;
    S.game = state.game;
    S.history = state.history;
    S.pad = state.pad;
    S.recording = state.recording;
    S.padButtons = state.pad_buttons;
    S.bind.check = state.bind_check;
    S.account = state.account;
    S.version = state.version;
    S.languages = state.languages;
    applyLanguage(state);
    for (const meter of $$('.lvl')) meter.replaceChildren(...Array.from({ length: LEVEL_BARS }, () => h('span')));

    bindControls();
    renderAll();

    if (state.first_run) {
      showErrors(state.errors);
      showView('wizard');
      renderWizard();
    } else {
      showErrors(state.errors);
      showView(Object.keys(state.errors).length ? 'settings' : 'status');
    }
    S.ready = true;
    for (const event of S.pending.splice(0)) onEvent(event);
    setInterval(renderStatusLine, 1000);
    setInterval(() => { if (S.recording.state === 'recording') renderRecording(); }, 250);
    loadMics();
  }

  function onEvent(event) {
    if (!S.ready) {
      S.pending.push(event);
      return;
    }
    if (event.seq <= S.seq) return; // already part of the snapshot
    S.seq = event.seq;
    switch (event.type) {
      case 'status':
        S.status = event.status;
        renderStatus();
        wizardOnStatus();
        break;
      case 'delivery':
        S.history.unshift(event.delivery);
        S.history.length = Math.min(S.history.length, 50);
        renderMessages();
        break;
      case 'game':
        S.game = event.game;
        renderGame();
        break;
      case 'voice':
        S.history.unshift(event.voice);
        S.history.length = Math.min(S.history.length, 50);
        renderMessages();
        if (S.view === 'wizard' && S.wizard.step === 4) {
          S.wizardVoice = event.voice;
          renderVoiceTest();
          $('#w-voice-result').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        }
        break;
      case 'pad':
        S.pad = event.pad;
        renderVoice();
        break;
      case 'recording':
        S.recording = event.recording;
        renderRecording();
        break;
      case 'mic':
        renderLevel(event.level);
        break;
      case 'bind_check':
        onBindCheck(event.check);
        break;
      case 'account':
        S.account = event.account;
        renderAccount();
        break;
    }
  }

  window.vf = { onEvent };
  window.addEventListener('pywebviewready', init);
  init();

  // ------------------------------------------------------------------ language
  function applyLanguage(state) {
    L = state.strings;
    S.lang = state.lang;
    S.language = state.language;
    S.systemLang = state.system_lang;
    document.documentElement.lang = S.lang;
    for (const el of $$('[data-i18n]')) el.textContent = t(el.dataset.i18n);
    for (const el of $$('[data-i18n-attr]')) {
      for (const pair of el.dataset.i18nAttr.split(';')) {
        const [attr, key] = pair.split(':');
        el.setAttribute(attr, t(key));
      }
    }
    for (const el of $$('.info')) el.setAttribute('aria-label', t('common.more'));
    hideTip();
    const names = S.languages.map((l) => h('option', { value: l.code }, l.name));
    const pick = $('#lang-pick');
    pick.replaceChildren(...names.map((o) => o.cloneNode(true)));
    pick.value = S.lang;
    $('#lang-code').textContent = S.lang.toUpperCase();
    const system = S.languages.find((l) => l.code === S.systemLang);
    const setting = $('#s-lang');
    setting.replaceChildren(h('option', { value: '' }, t('settings.language_auto', { name: system ? system.name : S.systemLang })), ...names);
    setting.value = S.language;
  }

  // Everything drawn from state, again: after the start and after a language change.
  function renderAll() {
    $('#version').textContent = t('app.version', { version: S.version });
    for (const button of $$('[data-reveal]')) button.textContent = t($(`#${button.dataset.reveal}`).type === 'password' ? 'common.show' : 'common.hide');
    S.actionsFor = null;
    fillForm();
    renderStatus();
    renderGame();
    renderMessages();
    renderRecording();
    renderBind();
    renderWizard();
    $('#st-test-hint').textContent = '';
  }

  async function switchLanguage(value) {
    const result = await call('set_language', value);
    if (!result) return;
    applyLanguage(result);
    S.padButtons = result.pad_buttons;
    for (const values of [S.form, S.saved]) if (values) values.language = result.language;
    renderAll();
    const shown = Object.keys(S.errors);
    if (shown.length) { // the same problems, in the new language
      const errors = (await call('check_config', S.form)) || {};
      showErrors(Object.fromEntries(shown.filter((k) => errors[k]).map((k) => [k, errors[k]])));
    }
  }

  // ------------------------------------------------------------------ views
  function showView(name) {
    if (name !== S.view) setMicTest(false);
    S.view = name;
    for (const view of ['wizard', 'status', 'settings']) $(`#view-${view}`).hidden = view !== name;
    $('#tabs').hidden = name === 'wizard';
    for (const tab of $$('.tab')) {
      const on = tab.dataset.tab === name;
      tab.classList.toggle('on', on);
      if (on) tab.setAttribute('aria-current', 'page');
      else tab.removeAttribute('aria-current');
    }
  }

  function showErrors(errors = {}) {
    S.errors = errors;
    for (const el of $$('[data-error-for]')) el.textContent = errors[el.dataset.errorFor] || '';
    for (const input of $$('[data-field]')) input.classList.toggle('bad', Boolean(errors[input.dataset.field]));
    const fileError = $('#s-file-error');
    fileError.textContent = errors.file || '';
    fileError.hidden = !errors.file;
  }

  // ------------------------------------------------------------------ status tab
  function renderStatus() {
    const s = S.status;
    const look = STATES[s.state] || STATES.disconnected;
    const orb = $('#st-orb');
    orb.style.background = `radial-gradient(circle at 36% 30%, rgba(255,255,255,.75) 0 7%, ${look.c1} 34%, ${look.c2} 100%)`;
    orb.style.boxShadow = `0 0 0 3px #0b0907, 0 0 0 5px #7d6130, 0 0 0 6px #000, 0 0 26px ${look.glow}`;
    orb.classList.toggle('pulse', Boolean(look.pulse));
    const title = $('#st-title');
    title.textContent = t(`conn.${STATES[s.state] ? s.state : 'disconnected'}`);
    title.style.color = look.c1;
    $('#st-url').textContent = S.saved && S.saved.server_url ? S.saved.server_url : t('status.no_url');
    renderStatusLine();
    renderAccount();
    const key = `${s.state}|${S.configured}|${s.link || ''}`;
    if (S.actionsFor !== key) {
      S.actionsFor = key;
      $('#st-actions').replaceChildren(...statusActions(s.state));
    }
  }

  function statusLine(s) {
    const now = Date.now() / 1000;
    const detail = cap(s.detail);
    switch (s.state) {
      case 'connected': {
        const up = Math.max(0, Math.floor(now - (s.since || now)));
        const hms = `${Math.floor(up / 3600)}:${String(Math.floor((up % 3600) / 60)).padStart(2, '0')}:${String(up % 60).padStart(2, '0')}`;
        return t('status.online', { time: hms, mode: t(S.saved && S.saved.mode === 'chat' ? 'mode.chat' : 'settings.mode_addon') });
      }
      case 'reconnecting': {
        const wait = Math.max(0, Math.ceil((s.retry_at || now) - now));
        return t('status.retry', { detail, s: wait, n: s.attempt });
      }
      case 'auth_failed': return s.link ? detail : ''; // an account service says why: revoked, unknown
      case 'connecting': case 'replaced': return ''; // the title says it all
      default:
        if (!S.configured) return t('status.not_configured');
        return detail;
    }
  }

  function renderStatusLine() {
    $('#st-line').textContent = statusLine(S.status);
  }

  function statusActions(state) {
    const button = (label, ghost, action, disabled = false) =>
      h('button', { class: ghost ? 'btn ghost' : 'btn', type: 'button', style: 'flex: 1', disabled, onclick: action }, label);
    const connect = () => call('connect');
    switch (state) {
      case 'connected':
        return [button(t('action.disconnect'), true, () => call('disconnect')), button(t('action.reconnect'), true, () => call('reconnect'))];
      case 'connecting':
        return [button(t('common.cancel'), true, () => call('disconnect'))];
      case 'reconnecting':
        return [button(t('action.retry'), false, () => call('reconnect')), button(t('action.disconnect'), true, () => call('disconnect'))];
      case 'auth_failed': {
        const settings = button(t('action.open_settings'), false, () => { showView('settings'); $('#s-token').focus(); });
        const account = S.status.link ? [button(t('account.open'), true, () => call('open_link', S.status.link))] : [];
        return [settings, ...account, button(t('action.retry'), true, connect)];
      }
      case 'replaced':
        return [button(t('action.connect_here'), false, connect)];
      default:
        return [button(t('action.connect'), false, connect, !S.configured)];
    }
  }

  // Plan, AI messages left today and the paid-until date, from servers with accounts.
  function renderAccount() {
    const a = S.status.state === 'connected' ? S.account : null; // stale once the connection is gone
    $('#st-account').hidden = !a;
    if (!a) return;
    $('#st-plan').textContent = a.plan || t('account.plan');
    const parts = [];
    if (a.ai_left !== null) parts.push(a.ai_limit !== null ? t('account.ai_of', { left: a.ai_left, limit: a.ai_limit }) : t('account.ai_left', { left: a.ai_left }));
    if (a.until) parts.push(t('account.until', { date: new Date(a.until * 1000).toLocaleDateString(S.lang) }));
    $('#st-quota').textContent = parts.join(' · ');
    $('#st-account-open').hidden = !a.link;
  }

  function renderGame() {
    const game = GAME_COLORS[S.game] ? S.game : 'closed';
    const pill = $('#game-pill');
    pill.textContent = t(`game.${game}`);
    pill.style.color = GAME_COLORS[game];
    pill.style.borderColor = GAME_COLORS[game];
  }

  function renderMessages() {
    const list = $('#msgs');
    // A recording's message comes twice: as typed, then with what the server made of it. Keep the second.
    const rows = S.history.filter((d) => !(d.kind === 'delivery' && d.voice_id));
    if (!rows.length) {
      list.replaceChildren(h('li', { class: 'hint empty' }, t('msgs.empty')));
      return;
    }
    list.replaceChildren(...rows.map((d) => {
      const time = new Date(d.at * 1000).toLocaleTimeString(S.lang, { hour: '2-digit', minute: '2-digit' });
      const voice = d.kind === 'voice';
      const test = !voice && d.channel === 'say' && d.text === TEST_TEXT;
      const source = t(voice ? 'msgs.from_pad' : test ? 'msgs.from_test' : 'msgs.from_telegram');
      const view = messageView(d);
      return h('li', { class: 'msg' },
        h('span', { class: 'time' }, time),
        h('span', { class: 'src', role: 'img', 'aria-label': source, title: source }, test ? null : icon(voice ? 'mic' : 'tg')),
        h('div', { class: 'field', style: 'gap: 2px; min-width: 0', title: d.transcript ? t('msgs.heard', { text: d.transcript }) : null },
          view.line, view.note),
        h('span', { role: 'img', 'aria-label': view.label, title: view.label, style: 'padding-top: 2px' }, icon(view.mark)));
    }));
  }

  // A row of the list: a typed message (Delivery) or what became of a recording (VoiceResult).
  function messageView(d) {
    const tagged = () => {
      const [label, color] = channelLabel(d.channel, d.target);
      return h('span', { class: 'mtext', style: `color: ${color}` }, h('b', {}, `[${label}]`), ` ${d.text}`);
    };
    const plain = (text) => h('span', { class: 'mtext muted' }, text);
    const note = (kind, text) => h('span', { class: `mdetail ${kind}` }, text);
    switch (d.status) {
      case 'ok':
        return { line: tagged(), note: d.note === 'ai_limit' ? note('muted', t('note.ai_limit')) : null, mark: 'ok', label: t('msgs.sent') };
      case 'denied': {
        const why = ACCESS_REASONS.includes(d.reason) ? t(`access.${d.reason}`) : t('access.no_access');
        return { line: plain(t('msgs.voice_refused')), mark: 'bad', label: t('msgs.denied_label'), note: note('bad', why) };
      }
      case 'not_focused':
        return {
          line: tagged(), mark: 'wait', label: t('msgs.not_focused'),
          note: note('warn', t('msgs.not_focused_note')),
        };
      case 'skip':
        return {
          line: plain(d.transcript ? t('msgs.quote', { text: d.transcript }) : t('msgs.no_text')), mark: 'wait', label: t('msgs.not_for_chat'),
          note: note('muted', t('msgs.skip_note')),
        };
      case 'empty':
        return { line: plain(t('msgs.empty_voice')), note: null, mark: 'wait', label: t('msgs.empty_voice_label') };
      default:
        if (d.kind === 'voice' && !d.text) {
          // Known statuses in our words, so they follow the window's language; the server's own goes to the tooltip.
          const why = VOICE_FAILED.includes(d.status) ? t(`voice.${d.status}`) : d.detail || t('voice.error');
          const line = plain(t('msgs.voice_refused'));
          if (d.detail) line.title = d.detail;
          return { line, mark: 'bad', label: t('msgs.voice_refused_label'), note: note('bad', t('msgs.server_said', { detail: why })) };
        }
        return {
          line: d.text ? tagged() : plain(t('msgs.not_sent')), mark: 'bad', label: t('msgs.input_error'),
          note: note('bad', t('msgs.input_failed', { detail: d.detail || t('msgs.unknown_error') })),
        };
    }
  }

  async function runTest(button, onTick, onDone) {
    button.disabled = true;
    let left = focusWait();
    onTick(left);
    const timer = setInterval(() => onTick((left = Math.max(0, left - 1))), 1000);
    const result = (await call('send_test')) || { status: 'error', detail: t('msgs.agent_silent') };
    clearInterval(timer);
    button.disabled = false;
    onDone(result);
  }

  // ------------------------------------------------------------------ first-run wizard
  function renderWizard() {
    const { step, check } = S.wizard;
    for (const li of $$('#view-wizard .steps li')) {
      const n = Number(li.dataset.step);
      li.classList.toggle('on', n === step);
      li.classList.toggle('done', n < step);
      const mark = $('.sn', li);
      if (n < step) mark.replaceChildren(icon('ok'));
      else mark.textContent = String(n);
    }
    for (const page of $$('#view-wizard [data-page]')) page.hidden = Number(page.dataset.page) !== step;
    if (S.view === 'wizard' && step !== 3) setMicTest(false);
    $('#w-back').hidden = step === 1;
    $('#w-skip').hidden = !(step === 1 && (check === 'failed' || check === 'replaced'));
    $('#w-later').hidden = !(step === 4 && !(S.bind.check && S.bind.check.step === 'done'));
    const next = $('#w-next');
    next.textContent = t(['wizard.connect', 'common.next', 'common.next', 'common.done'][step - 1]);
    next.disabled = step === 1 && check === 'checking';
    $$('[data-focus-wait]').forEach((el) => { el.textContent = focusWait(); });
    renderCheck();
  }

  function renderCheck() {
    const { check, detail } = S.wizard;
    const box = $('#w-check');
    box.hidden = check === 'idle';
    const text = ['checking', 'ok', 'auth', 'failed', 'replaced'].includes(check) ? t(`wizard.check_${check}`, { detail }) : '';
    const kind = { checking: 'warn', ok: 'good', auth: 'bad', failed: 'bad', replaced: 'warn' }[check];
    box.className = `note ${kind || ''}`;
    if (check === 'checking') {
      box.replaceChildren(h('span', { class: 'pulse', style: 'width: 12px; height: 12px; flex: none; border-radius: 50%; background: #f2ad42; box-shadow: 0 0 10px rgba(242,173,66,.8)' }), h('span', {}, text));
    } else if (text) {
      box.replaceChildren(icon(kind === 'good' ? 'ok' : kind === 'bad' ? 'bad' : 'wait'), h('span', {}, text));
    }
  }

  function setCheck(check, detail = '') {
    S.wizard.check = check;
    S.wizard.detail = detail;
    renderWizard();
  }

  function wizardOnStatus() {
    if (S.view !== 'wizard' || S.wizard.step !== 1 || !['checking', 'failed', 'replaced'].includes(S.wizard.check)) return;
    const s = S.status;
    if (s.state === 'connected') {
      setCheck('ok');
      setTimeout(() => {
        if (S.wizard.step === 1) {
          S.wizard.step = 2;
          renderWizard();
        }
      }, 900);
    } else if (s.state === 'auth_failed') setCheck('auth', s.detail);
    else if (s.state === 'reconnecting') setCheck('failed', s.detail);
    else if (s.state === 'replaced') setCheck('replaced');
  }

  async function saveForm() {
    const result = await call('save_config', S.form);
    if (!result) return null;
    showErrors(result.errors);
    if (result.ok) applySaved(result.config);
    return result;
  }

  async function wizardNext() {
    const { step } = S.wizard;
    if (step === 1) {
      setCheck('checking');
      const result = await saveForm();
      if (!result || !result.ok) {
        setCheck('idle');
        return;
      }
      if (!result.reconnecting) await call('reconnect');
    } else if (step === 2 || step === 3) {
      const result = await saveForm();
      if (result && result.ok) {
        S.wizard.step = step + 1;
        if (S.wizard.step === 3) loadMics();
        if (S.wizard.step === 4) S.wizardVoice = null;
        renderWizard();
        renderVoiceTest();
      }
    } else {
      await wizardFinish();
    }
  }

  async function wizardFinish() {
    S.form.connect_on_start = $('#w-auto').checked;
    S.form.autostart = $('#w-boot').checked;
    const result = await saveForm();
    if (result && result.ok) {
      if (bindRunning()) call('cancel_game_binding');
      showView('status');
      if (S.status.state === 'disconnected' && S.saved.connect_on_start) call('connect');
    }
  }

  // ------------------------------------------------------------------ settings tab
  function fillForm() {
    for (const input of $$('[data-field]')) input.value = S.form[input.dataset.field] ?? '';
    for (const range of $$('[data-range]')) {
      range.value = S.form[range.dataset.range];
      showRange(range);
    }
    for (const box of $$('[data-check]')) box.checked = Boolean(S.form[box.dataset.check]);
    $('#s-lang').value = S.language;
    $('#w-auto').checked = Boolean(S.form.connect_on_start);
    $('#w-boot').checked = Boolean(S.form.autostart);
    $('#s-path').textContent = S.configPath;
    renderChips();
    renderMode();
    renderHotkey();
    renderVoice();
    renderDirty();
  }

  function showRange(range) {
    $(`output[for="${range.id}"]`).textContent = `${S.form[range.dataset.range]} ${t(`unit.${range.dataset.unit}`)}`;
  }

  function applySaved(config) {
    S.saved = config;
    S.form = clone(config);
    S.configured = true;
    fillForm();
    renderStatus();
    renderGame();
  }

  function renderDirty() {
    const dirty = !sameSettings(S.form, S.saved);
    const line = $('#s-dirty');
    line.textContent = dirty ? t('settings.unsaved')
      : !S.configured ? t('settings.never_saved')
      : S.savedNote;
    line.classList.toggle('good', !dirty && Boolean(S.savedNote));
    $('#s-save').disabled = !dirty && S.configured;
    $('#s-cancel').disabled = !dirty;
  }

  function changed() {
    renderDirty();
    S.savedNote = '';
  }

  function renderMode() {
    const mode = S.form.mode;
    for (const button of $$('[data-mode]')) {
      const on = button.dataset.mode === mode;
      button.classList.toggle('sel', on);
      button.setAttribute('aria-pressed', String(on));
    }
    for (const el of $$('[data-addon-only]')) el.hidden = mode !== 'addon';
    renderVoice();
  }

  function renderHotkey() {
    const keys = String(S.form.addon_hotkey || '').split('+').filter(Boolean);
    for (const box of $$('[data-hotkey]')) {
      if (S.capturing) {
        box.replaceChildren(h('span', { class: 'capturing pulse' }, t('hotkey.capturing')));
      } else {
        box.replaceChildren(...keys.flatMap((key, i) => [...(i ? [h('span', {}, '+')] : []), h('span', { class: 'key' }, keyLabel(key))]));
      }
    }
    for (const button of $$('[data-capture]')) button.disabled = S.capturing;
    const changedKey = S.saved && S.form.addon_hotkey !== S.saved.addon_hotkey;
    for (const hint of $$('[data-hotkey-hint]')) {
      hint.textContent = changedKey ? t('hotkey.changed', { binding: wowBinding(keys) }) : '';
    }
  }

  function renderChips() {
    for (const box of $$('[data-chips]')) {
      const field = box.dataset.chips;
      const items = S.form[field] || [];
      box.replaceChildren(
        ...items.map((name) => h('span', { class: 'chip' }, name,
          h('button', {
            type: 'button',
            'aria-label': t('chips.remove', { name }),
            onclick: () => {
              S.form[field] = S.form[field].filter((x) => x !== name);
              renderChips();
              changed();
            },
          }, icon('remove')))),
        chipAdder(field));
    }
  }

  function chipAdder(field) {
    const button = h('button', { class: 'addchip', type: 'button' }, t('chips.add'));
    button.addEventListener('click', () => {
      const input = h('input', { class: 'inp chipinput', type: 'text', spellcheck: 'false', 'aria-label': t('chips.new') });
      let done = false;
      const finish = (keep) => {
        if (done) return;
        done = true;
        const value = input.value.trim();
        if (keep && value && !S.form[field].includes(value)) {
          S.form[field] = [...S.form[field], value];
          changed();
        }
        renderChips();
      };
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') finish(true);
        else if (e.key === 'Escape') finish(false);
      });
      input.addEventListener('blur', () => finish(true));
      button.replaceWith(input);
      input.focus();
    });
    return button;
  }

  async function detectGame() {
    const button = $('#s-detect');
    const note = $('#s-detect-note');
    button.disabled = true;
    note.hidden = false;
    note.className = 'note warn';
    let left = 5;
    note.textContent = t('detect.countdown', { s: left });
    const timer = setInterval(() => {
      left = Math.max(1, left - 1);
      note.textContent = t('detect.countdown', { s: left });
    }, 1000);
    const found = (await call('detect_game_window')) || { title: '', exe: '' };
    clearInterval(timer);
    button.disabled = false;
    if (!found.title && !found.exe) {
      note.className = 'note bad';
      note.textContent = t('detect.missed');
      return;
    }
    let added = false;
    for (const [field, value] of [['window_titles', found.title], ['process_names', found.exe]]) {
      if (value && !S.form[field].some((x) => x.toLowerCase() === value.toLowerCase())) {
        S.form[field] = [...S.form[field], value];
        added = true;
      }
    }
    renderChips();
    changed();
    note.className = 'note good';
    note.textContent = t(added ? 'detect.added' : 'detect.listed',
      { title: found.title || t('detect.untitled'), exe: found.exe || t('detect.unknown') });
  }

  async function saveSettings() {
    const wasConfigured = S.configured;
    const result = await saveForm();
    if (!result) return;
    if (!result.ok) {
      const first = $$('#view-settings [data-error-for], #s-file-error').find((el) => el.textContent);
      if (first) (first.closest('.field') || first).scrollIntoView({ block: 'center', behavior: 'smooth' });
      return;
    }
    S.savedNote = t(result.reconnecting ? 'settings.saved_reconnecting' : 'settings.saved');
    renderDirty();
    clearTimeout(saveSettings.timer);
    saveSettings.timer = setTimeout(() => { S.savedNote = ''; renderDirty(); }, 6000);
    if (!wasConfigured && S.status.state === 'disconnected' && S.saved.connect_on_start) call('connect');
  }

  // ------------------------------------------------------------------ push-to-talk
  function padButton(name) {
    return S.padButtons.find((b) => b.name === name) || { name, xbox: name, playstation: name };
  }

  // The button in a sentence: "right stick (RS)", but "A", "LB" and German nouns stay as they are.
  const inSentence = (label) => (S.lang !== 'de' && /^\p{Lu}\p{Ll}/u.test(label) ? label[0].toLowerCase() + label.slice(1) : label);

  function padLabel(name) {
    const button = padButton(name);
    return S.pad.playstation ? button.playstation : button.xbox;
  }

  async function loadMics() {
    const result = await call('list_mics');
    if (!result) return;
    S.mics = result.mics;
    S.micError = result.error;
    renderVoice();
  }

  async function setMicTest(on) {
    if (on === S.micTest) return;
    S.micTest = on;
    renderVoice();
    if (!on) renderLevel(0);
    const result = await call('mic_test', on, (S.form && S.form.mic_device) || '');
    if (on && result && result.error) {
      S.micTest = false;
      S.micError = result.error;
      renderVoice();
    }
  }

  function renderLevel(level) {
    const lit = Math.round(Math.max(0, Math.min(1, level)) * LEVEL_BARS);
    for (const meter of $$('.lvl')) {
      [...meter.children].forEach((bar, i) => { bar.className = i < lit ? (i >= HOT_BAR ? 'hot' : 'on') : ''; });
      if (meter.hasAttribute('aria-valuenow')) meter.setAttribute('aria-valuenow', String(lit));
    }
  }

  function renderVoice() {
    if (!S.form) return;
    const form = S.form;
    const addon = form.mode === 'addon';
    const broken = Boolean(S.pad.error);

    // microphone
    const names = S.mics || [];
    const options = [h('option', { value: '' }, t('mic.default')),
      ...names.map((name) => h('option', { value: name }, name))];
    if (form.mic_device && !names.includes(form.mic_device)) {
      options.push(h('option', { value: form.mic_device }, t('mic.missing', { name: form.mic_device })));
    }
    for (const select of $$('[data-mic]')) {
      select.replaceChildren(...options.map((o) => o.cloneNode(true)));
      select.value = form.mic_device || '';
      select.disabled = broken;
    }
    for (const button of $$('[data-mic-test]')) {
      button.textContent = t(S.micTest ? 'mic.stop' : 'mic.test');
      button.disabled = broken;
    }
    const micHint = broken ? t('mic.no_sdl') : S.micError ? t('mic.error', { error: S.micError }) : '';
    for (const hint of $$('[data-mic-hint]')) hint.textContent = micHint;

    // button
    for (const card of $$('[data-padcard]')) card.classList.toggle('cap', S.padCapture);
    for (const info of $$('[data-pad-info]')) {
      if (S.padCapture) {
        info.replaceChildren(
          h('span', { class: 'big pulse', role: 'status', style: 'font-size: 17px' }, t('pad.press')),
          h('span', { class: 'hint' }, t('pad.press_esc')));
        continue;
      }
      const color = broken ? 'var(--bad)' : S.pad.connected ? 'var(--ok)' : 'var(--off)';
      const line = broken ? t('pad.broken', { error: S.pad.error })
        : S.padNote ? t(S.padNote)
        : S.pad.connected ? t('pad.connected', { name: S.pad.name })
        : t('pad.none');
      info.replaceChildren(
        h('span', { class: 'big' }, padLabel(form.ptt_button)),
        h('span', { class: 'hint row', style: 'gap: 6px' },
          h('span', { class: 'led', style: `background: ${color}; box-shadow: 0 0 6px ${color}` }), line));
    }
    for (const button of $$('[data-pad-capture]')) button.disabled = S.padCapture || broken || !S.pad.connected;

    // mode
    for (const button of $$('[data-pttmode]')) {
      const on = button.dataset.pttmode === form.ptt_mode;
      button.classList.toggle('sel', on);
      button.setAttribute('aria-pressed', String(on));
    }
    // What the game does with the same button: in the tooltip, amber when it needs attention.
    const [tip, warn] = !addon ? ['tip.ptt_button_chat', true]
      : form.ptt_button === 'rightstick' ? ['tip.ptt_button_rightstick', true]
      : form.ptt_button === 'misc1' ? ['tip.ptt_button_share', true]
      : ['tip.ptt_button', false];
    for (const button of $$('[data-ptt-tip]')) setTip(button, tip, warn);

    renderStatusVoice();
    renderVoiceTest();
    renderBind();
  }

  async function capturePad() {
    if (S.padCapture) return;
    S.padCapture = true;
    S.padNote = '';
    renderVoice();
    const result = await call('capture_pad_button', 20);
    const cancelled = !S.padCapture;
    S.padCapture = false;
    if (result && result.button) {
      if (result.button !== S.form.ptt_button) {
        S.form.ptt_button = result.button;
        changed();
      }
    } else if (!cancelled) {
      S.padNote = 'pad.no_press'; // a key, so it follows a language change
    }
    renderVoice();
  }

  function cancelPadCapture() {
    S.padCapture = false;
    renderVoice();
    call('cancel_pad_capture');
  }

  // ------------------------------------------------------------------ recording
  function recordingPill() {
    const rec = S.recording;
    if (rec.state === 'recording') {
      const up = Math.max(0, Math.floor(Date.now() / 1000 - (rec.started_at || Date.now() / 1000)));
      return h('span', { class: 'rec on', role: 'status' }, h('span', { class: 'redled pulse' }),
        t('rec.recording', { time: `${Math.floor(up / 60)}:${String(up % 60).padStart(2, '0')}` }));
    }
    if (rec.state === 'processing') return h('span', { class: 'rec busy pulse', role: 'status' }, t('rec.processing'));
    return null;
  }

  function renderRecording() {
    renderStatusVoice();
    renderVoiceTest();
  }

  function renderStatusVoice() {
    const saved = S.saved;
    const on = Boolean(saved && saved.ptt_enabled);
    $('#st-voice').hidden = !on;
    if (!on) return;
    $('#st-pad-label').textContent = padLabel(saved.ptt_button);
    const mode = t(saved.ptt_mode === 'hold' ? 'ptt.hold' : 'ptt.toggle');
    const mic = saved.mic_device || t('mic.default');
    $('#st-pad-hint').textContent = S.pad.error ? t('pad.broken', { error: S.pad.error }) : `${mic} · ${mode}`;
    const pill = recordingPill() || (S.pad.error ? h('span', { class: 'pill', style: 'color: var(--bad); border-color: var(--bad)' }, t('pad.pill_off'))
      : S.pad.connected ? h('span', { class: 'pill', style: 'color: var(--muted); border-color: var(--bronze)' }, t('pad.pill_ready'))
      : h('span', { class: 'pill', style: 'color: var(--off); border-color: var(--off)' }, t('pad.pill_none')));
    $('#st-rec').replaceChildren(pill);
    $('#st-level').hidden = S.recording.state !== 'recording';
  }

  // The last wizard step: say something and see it arrive.
  function renderVoiceTest() {
    if (!S.form) return;
    const say = $('#w-voice-say');
    const label = h('b', { style: 'color: var(--gold)' }, inSentence(padLabel(S.form.ptt_button)));
    if (S.pad.error) say.replaceChildren(t('pad.broken', { error: S.pad.error }));
    else if (!S.pad.connected) say.replaceChildren(t('test.connect_pad'));
    else say.replaceChildren(...tNodes(S.form.ptt_mode === 'hold' ? 'test.hold' : 'test.toggle', { button: label }));
    $('#w-voice-rec').replaceChildren(recordingPill() || '');
    const box = $('#w-voice-result');
    const v = S.wizardVoice;
    box.hidden = !v;
    if (!v) return;
    const view = messageView(v);
    const kind = v.status === 'ok' ? 'good' : view.mark === 'wait' ? 'warn' : 'bad';
    box.className = `note ${kind} top`;
    box.replaceChildren(icon(view.mark === 'wait' ? 'warn' : view.mark),
      h('div', { class: 'field', style: 'gap: 2px' }, view.line, view.note,
        v.status === 'ok' ? h('span', { class: 'mdetail' }, t('test.sent')) : null));
  }

  // ------------------------------------------------------------------ in-game check of the button
  const bindRunning = () => Boolean(S.bind.check && !['done', 'error'].includes(S.bind.check.step));

  function onBindCheck(check) {
    const index = BIND_STEPS.indexOf(check.step);
    if (index >= 0) S.bind.last = index;
    else if (check.error === 'wrong_button') S.bind.last = BIND_STEPS.indexOf('done');
    else if (['chat_mode', 'not_connected'].includes(check.error)) S.bind.last = -1; // failed before it began
    S.bind.check = check;
    renderBind();
    renderWizard();
    if (check.step === 'done' || check.step === 'error') {
      const shown = S.view === 'wizard' ? (check.step === 'done' ? $('#w-voice-say') : $('#view-wizard [data-bind-result]'))
        : $('#s-bind-box [data-bind-actions]');
      if (shown) shown.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }

  async function startBindCheck() {
    if (!sameSettings(S.form, S.saved)) {
      const result = await saveForm();
      if (!result || !result.ok) return;
    }
    S.bind = { check: { step: 'waiting_game' }, last: 0 };
    $('#s-bind-box').hidden = false;
    renderBind();
    call('check_game_binding');
  }

  function bindRows() {
    const c = S.bind.check;
    const label = inSentence(padLabel((S.saved || S.form).ptt_button));
    const rows = ['game', 'focus', 'sent', 'reload', 'done'].map((step) => ({
      text: t(`bind.row_${step}`),
      now: step === 'done' ? '' : t(`bind.row_${step}_now`, { button: label }),
    }));
    const at = !c ? -1 : c.step === 'error' ? S.bind.last : BIND_STEPS.indexOf(c.step);
    return rows.map((row, i) => {
      let mark = 'todo';
      let { text } = row;
      let detail = '';
      let tip = null;
      if (c && c.step === 'done') {
        mark = 'ok';
        if (i === 4 && c.previous) {
          mark = 'warn';
          text = t('bind.was_on', { name: c.previous_name || c.previous });
          tip = infoButton('tip.bind_was_on');
        } else if (i === 4) {
          text = t('bind.was_free');
        }
      } else if (i < at) mark = 'ok';
      else if (i === at) {
        mark = c.step === 'error' ? 'bad' : 'now';
        detail = c.step === 'error' ? '' : row.now;
      }
      const ic = mark === 'todo' ? h('span', { class: 'ring' }) : mark === 'now' ? h('span', { class: 'ring now pulse' }) : icon(mark);
      const color = mark === 'todo' ? 'var(--muted)' : mark === 'bad' ? '#ffb1a5' : 'var(--parch)';
      return h('li', { class: 'chk' }, h('span', { class: 'ic' }, ic),
        h('span', { class: 'field', style: 'gap: 1px; min-width: 0' },
          h('span', { class: 'lrow', style: `color: ${color}` }, text, tip), detail ? h('span', { class: 'hint' }, detail) : null));
    });
  }

  function bindError(c) {
    const label = inSentence(padLabel((S.saved || S.form).ptt_button));
    switch (c.error) {
      case 'addon_missing':
      case 'no_game_folder':
        return [t(`bind.err_${c.error}`), t(`bind.err_${c.error}_hint`)];
      case 'wrong_button':
        return [t('bind.err_wrong_button'), t('bind.err_wrong_button_hint', { button: label })];
      case 'chat_mode':
      case 'not_connected':
        return [t(`bind.err_${c.error}`), ''];
      default:
        return [t('bind.err_other'), cap(c.detail)];
    }
  }

  function renderBind() {
    if (!S.form) return;
    const c = S.bind.check;
    const addon = (S.saved || S.form).mode === 'addon';
    const running = bindRunning();
    const rows = bindRows();
    for (const list of $$('[data-bind-rows]')) {
      list.hidden = !addon;
      list.replaceChildren(...rows.map((row) => row.cloneNode(true)));
    }
    for (const box of $$('[data-bind-result]')) {
      box.hidden = !(c && c.step === 'error');
      if (box.hidden) continue;
      const [title, detail] = bindError(c);
      box.className = 'note bad top';
      box.replaceChildren(icon('bad'), h('div', { class: 'field', style: 'gap: 2px' },
        h('b', {}, title), detail ? h('span', {}, detail) : null));
    }
    for (const actions of $$('[data-bind-actions]')) {
      actions.hidden = !addon;
      const run = h('button', { class: 'btn', type: 'button', disabled: running, onclick: startBindCheck },
        t(running ? 'bind.running' : c ? 'bind.again' : 'bind.open'));
      const cancel = h('button', { class: 'link', type: 'button', style: 'align-self: center', onclick: () => call('cancel_game_binding') }, t('common.cancel'));
      actions.replaceChildren(...(running ? [run, cancel] : [run]));
    }
    $('#w-bind-intro').textContent = addon ? '' : t('bind.intro_chat');
    for (const button of $$('[data-bind-tip]')) setTip(button, addon ? 'tip.bind' : 'tip.ptt_button_chat', !addon);

    const line = $('#s-bind-line');
    if (!addon) line.textContent = '';
    else if (c && c.step === 'done') {
      line.textContent = c.previous ? t('bind.line_taken', { name: c.previous_name || c.previous }) : t('bind.line_free');
    } else line.textContent = t('bind.line_unchecked');
    $('#s-bind-open').hidden = !addon || !$('#s-bind-box').hidden;
    for (const button of $$('#view-settings [data-bind-tip]')) button.hidden = !addon;
  }

  // ------------------------------------------------------------------ tooltips
  // Explanations live behind an (i) next to a label: data-i18n-tip names the string. One
  // floating box shows it on hover or keyboard focus, and a click pins it for touch screens.
  function infoButton(key, warn = false) {
    const button = h('button', { class: 'info', type: 'button', 'aria-label': t('common.more') });
    setTip(button, key, warn);
    return button;
  }

  function setTip(button, key, warn = false) {
    button.dataset.i18nTip = key;
    button.classList.toggle('warn', warn);
    if (!button.firstElementChild) button.append(icon('info'));
    if (TIP.owner === button) showTip(button);
  }

  function showTip(button) {
    const box = $('#tip');
    TIP.owner = button;
    box.textContent = t(button.dataset.i18nTip, { s: focusWait() });
    box.classList.toggle('warn', button.classList.contains('warn'));
    box.hidden = false;
    button.classList.add('open');
    button.setAttribute('aria-describedby', 'tip');
    // Below the icon, or above it when the window ends; never past the window's sides.
    const r = button.getBoundingClientRect();
    const w = box.offsetWidth;
    const hgt = box.offsetHeight;
    const left = Math.min(Math.max(8, r.left + r.width / 2 - 24), window.innerWidth - w - 8);
    const below = r.bottom + 8 + hgt <= window.innerHeight - 8;
    box.style.left = `${left}px`;
    box.style.top = `${below ? r.bottom + 8 : Math.max(8, r.top - 8 - hgt)}px`;
  }

  function hideTip() {
    if (!TIP.owner) return;
    TIP.owner.classList.remove('open');
    TIP.owner.removeAttribute('aria-describedby');
    TIP.owner = null;
    $('#tip').hidden = true;
  }

  function bindTips() {
    for (const button of $$('.info')) if (!button.firstElementChild) button.append(icon('info'));
    const owner = (e) => e.target.closest && e.target.closest('.info[data-i18n-tip]');
    document.addEventListener('mouseover', (e) => { const b = owner(e); if (b && b !== TIP.owner) showTip(b); });
    document.addEventListener('mouseout', (e) => { const b = owner(e); if (b && !b.contains(e.relatedTarget) && document.activeElement !== b) hideTip(); });
    document.addEventListener('focusin', (e) => { const b = owner(e); if (b) showTip(b); else hideTip(); });
    document.addEventListener('focusout', (e) => { if (owner(e)) hideTip(); });
    document.addEventListener('click', (e) => {
      const b = owner(e);
      if (b) {
        e.preventDefault(); // inside a switch's label, the click must not flip the switch
        showTip(b);
      } else hideTip();
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && TIP.owner) hideTip(); });
    document.addEventListener('scroll', () => { // follow the (i), or let go once it scrolls away
      if (!TIP.owner) return;
      const r = TIP.owner.getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight) hideTip(); else showTip(TIP.owner);
    }, true);
    window.addEventListener('resize', hideTip);
  }

  // ------------------------------------------------------------------ wiring
  function bindControls() {
    bindTips();
    $('#btn-minimize').addEventListener('click', () => call('minimize'));
    $('#btn-close').addEventListener('click', () => call('close'));
    $('#st-account-open').addEventListener('click', () => S.account && call('open_link', S.account.link));
    $('#lang-pick').addEventListener('change', (e) => switchLanguage(e.target.value));
    $('#s-lang').addEventListener('change', (e) => switchLanguage(e.target.value));
    for (const tab of $$('.tab')) {
      tab.addEventListener('click', () => {
        showView(tab.dataset.tab);
        if (tab.dataset.tab === 'settings') loadMics();
      });
    }

    for (const input of $$('[data-field]')) {
      input.addEventListener('input', () => {
        S.form[input.dataset.field] = input.value;
        for (const twin of $$(`[data-field="${input.dataset.field}"]`)) if (twin !== input) twin.value = input.value;
        if (S.view === 'wizard' && S.wizard.check !== 'checking') setCheck('idle');
        changed();
      });
    }
    for (const id of ['#w-url', '#w-token']) {
      $(id).addEventListener('keydown', (e) => { if (e.key === 'Enter' && !$('#w-next').disabled) wizardNext(); });
    }
    for (const button of $$('[data-reveal]')) {
      button.addEventListener('click', () => {
        const input = $(`#${button.dataset.reveal}`);
        const show = input.type === 'password';
        input.type = show ? 'text' : 'password';
        button.textContent = t(show ? 'common.hide' : 'common.show');
      });
    }
    for (const range of $$('[data-range]')) {
      range.addEventListener('input', () => {
        S.form[range.dataset.range] = Number(range.value);
        showRange(range);
        if (range.dataset.range === 'max_record_s') renderVoice();
        changed();
      });
    }
    for (const box of $$('[data-check]')) {
      box.addEventListener('change', () => {
        S.form[box.dataset.check] = box.checked;
        changed();
      });
    }
    for (const button of $$('[data-mode]')) {
      button.addEventListener('click', () => {
        S.form.mode = button.dataset.mode;
        renderMode();
        changed();
      });
    }
    for (const button of $$('[data-capture]')) {
      button.addEventListener('click', () => {
        S.capturing = true;
        renderHotkey();
      });
    }
    for (const select of $$('[data-mic]')) {
      select.addEventListener('change', () => {
        S.form.mic_device = select.value;
        S.micError = '';
        renderVoice();
        changed();
        if (S.micTest) {
          call('mic_test', true, select.value);
        }
      });
      select.addEventListener('focus', () => { if (S.mics === null || S.micError) loadMics(); });
    }
    for (const button of $$('[data-mic-test]')) button.addEventListener('click', () => setMicTest(!S.micTest));
    for (const button of $$('[data-pad-capture]')) button.addEventListener('click', capturePad);
    for (const button of $$('[data-pttmode]')) {
      button.addEventListener('click', () => {
        S.form.ptt_mode = button.dataset.pttmode;
        renderVoice();
        changed();
      });
    }
    $('#s-bind-open').addEventListener('click', () => {
      $('#s-bind-box').hidden = false;
      renderBind();
      if (!bindRunning()) startBindCheck();
    });
    document.addEventListener('keydown', (e) => {
      if (S.padCapture && e.code === 'Escape') {
        e.preventDefault();
        cancelPadCapture();
        return;
      }
      if (!S.capturing) return;
      e.preventDefault();
      e.stopPropagation();
      if (e.code === 'Escape' && !e.ctrlKey && !e.shiftKey && !e.altKey) {
        S.capturing = false;
        renderHotkey();
        return;
      }
      const key = keyFromCode(e.code);
      if (!key) return; // a modifier on its own: wait for the main key
      S.form.addon_hotkey = [e.altKey && 'alt', e.ctrlKey && 'ctrl', e.shiftKey && 'shift', key].filter(Boolean).join('+');
      S.capturing = false;
      renderHotkey();
      changed();
    }, true);

    $('#w-next').addEventListener('click', wizardNext);
    $('#w-back').addEventListener('click', () => {
      if (S.wizard.step === 4 && bindRunning()) call('cancel_game_binding');
      S.wizard.step = Math.max(1, S.wizard.step - 1);
      renderWizard();
    });
    $('#w-later').addEventListener('click', wizardFinish);
    $('#w-skip').addEventListener('click', () => {
      S.wizard.step = 2;
      renderWizard();
    });

    $('#st-test').addEventListener('click', () => runTest($('#st-test'),
      (left) => { $('#st-test-hint').textContent = t('status.test_wait', { s: left }); },
      () => { $('#st-test-hint').textContent = ''; }));
    $('#btn-log').addEventListener('click', () => call('open_log'));

    $('#s-detect').addEventListener('click', detectGame);
    $('#s-reset-timings').addEventListener('click', () => {
      for (const key of ['focus_wait_s', 'open_delay_ms', 'part_delay_ms', 'key_delay_ms']) S.form[key] = S.defaults[key];
      for (const range of $$('[data-range]')) {
        range.value = S.form[range.dataset.range];
        showRange(range);
      }
      changed();
    });
    $('#s-open-folder').addEventListener('click', () => call('open_config_folder'));
    $('#s-cancel').addEventListener('click', () => {
      S.form = clone(S.saved);
      fillForm();
      showErrors({});
    });
    $('#s-save').addEventListener('click', saveSettings);
  }
})();
