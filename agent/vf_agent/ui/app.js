// VoiceForever agent window. Talks to Python through window.pywebview.api (see gui/api.py);
// Python pushes connection, game and message events into window.vf.onEvent.
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
    tg: '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M14.5 2L1.5 7.2l4.3 1.6L12 4.5 7.2 9.6v4l2.4-2.6 3 2.2z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>',
  };

  // ------------------------------------------------------------------ words
  const STATES = {
    connected: { title: 'На связи', c1: '#5ed474', c2: '#1d6b2c', glow: 'rgba(94,212,116,.55)' },
    connecting: { title: 'Подключение…', c1: '#f2ad42', c2: '#7a4a0c', glow: 'rgba(242,173,66,.5)', pulse: true },
    reconnecting: { title: 'Связь потеряна', c1: '#f2ad42', c2: '#7a4a0c', glow: 'rgba(242,173,66,.45)' },
    disconnected: { title: 'Отключено', c1: '#a59a88', c2: '#3a342b', glow: 'rgba(0,0,0,0)' },
    auth_failed: { title: 'Токен не принят', c1: '#ff7563', c2: '#7a1a10', glow: 'rgba(255,117,99,.5)' },
    replaced: { title: 'Агент на другом ПК', c1: '#f2ad42', c2: '#7a4a0c', glow: 'rgba(242,173,66,.35)' },
  };

  const GAME = {
    active: { label: 'окно активно', color: '#5ed474', hint: () => 'Сообщения сразу уходят в чат.' },
    background: { label: 'в фоне', color: '#f2ad42', hint: () => `Сообщение подождёт до ${focusWait()} с, пока ты вернёшься в игру.` },
    closed: { label: 'не запущена', color: '#a59a88', hint: () => 'Агент найдёт окно, когда игра запустится.' },
  };

  const CHANNEL_NAMES = { 1: 'Общий', 2: 'Торговля' };

  function channelLabel(channel, target) {
    switch (channel) {
      case 'say': return ['Сказать', '#f3eee6'];
      case 'yell': return ['Крик', '#ff6a5a'];
      case 'emote': return ['Эмоция', '#ff9a5a'];
      case 'group': case 'party': return ['Группа', '#aab4ff'];
      case 'raid': return ['Рейд', '#ff9a4a'];
      case 'raid_warning': return ['Объявление рейду', '#ff7a3a'];
      case 'instance': return ['Подземелье', '#ff9a4a'];
      case 'guild': return ['Гильдия', '#56e06a'];
      case 'officer': return ['Офицер', '#48c858'];
      case 'whisper': return [target ? `Шёпот → ${target}` : 'Шёпот', '#ff8ef0'];
      case 'reply': return ['Ответ на шёпот', '#ff8ef0'];
      case 'channel': return [CHANNEL_NAMES[target] ? `${target}. ${CHANNEL_NAMES[target]}` : `Канал ${target}`, '#ffc8b0'];
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

  const TEST_HINT = 'Строка уйдёт в /say, когда окно WoW станет активным.';
  const TEST_TEXT = 'VoiceForever test';

  // What became of a recording, when nothing reached the chat (VoiceResult.status).
  const VOICE_FAILED = {
    stt_error: 'не смог распознать речь',
    bad_audio: 'не смог прочитать запись',
    too_long: 'запись длиннее, чем он принимает',
    unsupported: 'не принимает голос от агента, его нужно обновить',
    offline: 'нет связи',
    timeout: 'не ответил вовремя',
    error: 'ошибка на сервере',
  };

  const LEVEL_BARS = 24;
  const HOT_BAR = 20; // from here the meter turns red: too loud

  // The in-game check of the push-to-talk button, in the order the agent reports it.
  const BIND_STEPS = ['waiting_game', 'waiting_focus', 'sent', 'reloading', 'done'];

  // ------------------------------------------------------------------ state
  const S = {
    ready: false,
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
  };

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
    $('#version').textContent = `агент ${state.version}`;
    for (const meter of $$('.lvl')) meter.replaceChildren(...Array.from({ length: LEVEL_BARS }, () => h('span')));

    bindControls();
    fillForm();
    renderStatus();
    renderGame();
    renderMessages();
    renderVoice();
    renderRecording();
    renderBind();
    $('#st-test-hint').textContent = TEST_HINT;

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
    }
  }

  window.vf = { onEvent };
  window.addEventListener('pywebviewready', init);
  init();

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
    title.textContent = look.title;
    title.style.color = look.c1;
    $('#st-url').textContent = S.saved && S.saved.server_url ? S.saved.server_url : 'Адрес сервера ещё не задан';
    renderStatusLine();
    const key = `${s.state}|${S.configured}`;
    if (S.actionsFor !== key) {
      S.actionsFor = key;
      $('#st-actions').replaceChildren(...statusActions(s.state));
    }
  }

  function statusLine(s) {
    const now = Date.now() / 1000;
    const detail = s.detail ? s.detail[0].toUpperCase() + s.detail.slice(1) : '';
    switch (s.state) {
      case 'connected': {
        const up = Math.max(0, Math.floor(now - (s.since || now)));
        const hms = `${Math.floor(up / 3600)}:${String(Math.floor((up % 3600) / 60)).padStart(2, '0')}:${String(up % 60).padStart(2, '0')}`;
        return `В сети ${hms} · режим: ${S.saved && S.saved.mode === 'chat' ? 'обычный чат' : 'аддон'}`;
      }
      case 'connecting': return 'Открываю соединение, жду ответа до 15 с';
      case 'reconnecting': {
        const wait = Math.max(0, Math.ceil((s.retry_at || now) - now));
        return `${detail}. Повтор через ${wait} с, попытка ${s.attempt}`;
      }
      case 'auth_failed': return `${detail}. Проверь токен в настройках`;
      case 'replaced': return 'С этим токеном подключились с другого компьютера';
      default:
        if (!S.configured) return 'Сначала заполни и сохрани настройки';
        return detail || 'Сообщения из Telegram сейчас не дойдут до игры';
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
        return [button('Отключить', true, () => call('disconnect')), button('Переподключить', true, () => call('reconnect'))];
      case 'connecting':
        return [button('Отменить', true, () => call('disconnect'))];
      case 'reconnecting':
        return [button('Повторить сейчас', false, () => call('reconnect')), button('Отключить', true, () => call('disconnect'))];
      case 'auth_failed':
        return [button('Открыть настройки', false, () => { showView('settings'); $('#s-token').focus(); }), button('Повторить', true, connect)];
      case 'replaced':
        return [button('Подключить здесь', false, connect)];
      default:
        return [button('Подключить', false, connect, !S.configured)];
    }
  }

  function renderGame() {
    const game = GAME[S.game] || GAME.closed;
    const pill = $('#game-pill');
    pill.textContent = game.label;
    pill.style.color = game.color;
    pill.style.borderColor = game.color;
    $('#game-hint').textContent = game.hint();
  }

  function renderMessages() {
    const list = $('#msgs');
    // A recording's message comes twice: as typed, then with what the server made of it. Keep the second.
    const rows = S.history.filter((d) => !(d.kind === 'delivery' && d.voice_id));
    if (!rows.length) {
      list.replaceChildren(h('li', { class: 'hint empty' }, 'Пока пусто. Скажи что-нибудь с геймпада, боту в Telegram или нажми «Тест в /say».'));
      return;
    }
    list.replaceChildren(...rows.map((d) => {
      const time = new Date(d.at * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
      const voice = d.kind === 'voice';
      const test = !voice && d.channel === 'say' && d.text === TEST_TEXT;
      const source = voice ? 'Голос с геймпада' : test ? 'Тест' : 'Голосовое из Telegram';
      const view = messageView(d);
      return h('li', { class: 'msg' },
        h('span', { class: 'time' }, time),
        h('span', { class: 'src', role: 'img', 'aria-label': source, title: source }, test ? null : icon(voice ? 'mic' : 'tg')),
        h('div', { class: 'field', style: 'gap: 2px; min-width: 0', title: d.transcript ? `Распознано: «${d.transcript}»` : null },
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
        return { line: tagged(), note: null, mark: 'ok', label: 'Отправлено в игру' };
      case 'not_focused':
        return {
          line: tagged(), mark: 'wait', label: 'Не отправлено: окно WoW не было активно',
          note: note('warn', `Окно WoW не стало активным за ${focusWait()} с, сообщение не ушло.`),
        };
      case 'skip':
        return {
          line: plain(d.transcript ? `«${d.transcript}»` : 'Запись без текста'), mark: 'wait', label: 'Не для чата',
          note: note('muted', 'Не похоже на сообщение в чат, ничего не ушло.'),
        };
      case 'empty':
        return { line: plain('Не расслышал, в чат ничего не ушло'), note: null, mark: 'wait', label: 'Пустая запись' };
      default:
        if (d.kind === 'voice' && !d.text) {
          return {
            line: plain('Запись не принята'), mark: 'bad', label: 'Отказ сервера',
            note: note('bad', `Сервер: ${d.detail || VOICE_FAILED[d.status] || VOICE_FAILED.error}.`),
          };
        }
        return {
          line: d.text ? tagged() : plain('Сообщение не ушло'), mark: 'bad', label: 'Ошибка ввода',
          note: note('bad', `Не получилось ввести: ${d.detail || 'неизвестная ошибка'}`),
        };
    }
  }

  async function runTest(button, onTick, onDone) {
    button.disabled = true;
    let left = focusWait();
    onTick(left);
    const timer = setInterval(() => onTick((left = Math.max(0, left - 1))), 1000);
    const result = (await call('send_test')) || { status: 'error', detail: 'агент не ответил' };
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
    next.textContent = ['Подключиться', 'Далее', 'Далее', 'Готово'][step - 1];
    next.disabled = step === 1 && check === 'checking';
    $$('[data-focus-wait]').forEach((el) => { el.textContent = focusWait(); });
    renderCheck();
  }

  function renderCheck() {
    const { check, detail } = S.wizard;
    const box = $('#w-check');
    box.hidden = check === 'idle';
    const text = {
      checking: 'Подключаюсь к серверу, жду ответа до 15 секунд…',
      ok: 'Сервер ответил, токен принят.',
      auth: `Сервер не принял токен (${detail}). Сверь его со строкой VF_USERS на сервере.`,
      failed: `Не удалось подключиться: ${detail}. Агент будет пробовать сам, настройку можно продолжить.`,
      replaced: 'С этим токеном только что подключился другой агент.',
    }[check];
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
    $(`output[for="${range.id}"]`).textContent = `${S.form[range.dataset.range]} ${range.dataset.unit}`;
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
    line.textContent = dirty ? 'Есть несохранённые изменения'
      : !S.configured ? 'Настройки ещё не сохранены'
      : S.savedNote || 'Все изменения сохранены';
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
    $('#s-mode-hint').textContent = mode === 'addon'
      ? 'Чат не открывается, аддон сам выбирает группу, рейд или подземелье.'
      : 'Без аддона: агент открывает чат клавишей Enter и печатает /p, /g, /w.';
  }

  function renderHotkey() {
    const keys = String(S.form.addon_hotkey || '').split('+').filter(Boolean);
    for (const box of $$('[data-hotkey]')) {
      if (S.capturing) {
        box.replaceChildren(h('span', { class: 'capturing pulse' }, 'Нажми сочетание клавиш… Esc отменит'));
      } else {
        box.replaceChildren(...keys.flatMap((key, i) => [...(i ? [h('span', {}, '+')] : []), h('span', { class: 'key' }, keyLabel(key))]));
      }
    }
    for (const button of $$('[data-capture]')) button.disabled = S.capturing;
    const changedKey = S.saved && S.form.addon_hotkey !== S.saved.addon_hotkey;
    for (const hint of $$('[data-hotkey-hint]')) {
      hint.textContent = changedKey
        ? `Поменяй клавишу и в игре: /vf key ${wowBinding(keys)}`
        : 'Должна совпадать с клавишей, которую показывает /vf в игре. Аддон кладётся в папку Interface\\AddOns.';
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
            'aria-label': `Убрать ${name}`,
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
    const button = h('button', { class: 'addchip', type: 'button' }, '+ Добавить');
    button.addEventListener('click', () => {
      const input = h('input', { class: 'inp chipinput', type: 'text', spellcheck: 'false', 'aria-label': 'Новое значение' });
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
    $('#s-detect-hint').hidden = true;
    note.hidden = false;
    note.className = 'note warn';
    let left = 5;
    note.textContent = `Переключись в WoW. Запомню активное окно через ${left} с.`;
    const timer = setInterval(() => {
      left = Math.max(1, left - 1);
      note.textContent = `Переключись в WoW. Запомню активное окно через ${left} с.`;
    }, 1000);
    const found = (await call('detect_game_window')) || { title: '', exe: '' };
    clearInterval(timer);
    button.disabled = false;
    if (!found.title && !found.exe) {
      note.className = 'note bad';
      note.textContent = 'Активным осталось окно агента. Нажми ещё раз и успей переключиться в игру.';
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
    note.textContent = `Нашёл «${found.title || 'без названия'}», процесс ${found.exe || 'неизвестен'}. `
      + (added ? 'Добавил в списки, не забудь сохранить.' : 'Оба уже в списке.');
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
    S.savedNote = result.reconnecting ? 'Сохранено. Переподключаюсь с новыми настройками…' : 'Сохранено в config.toml';
    renderDirty();
    clearTimeout(saveSettings.timer);
    saveSettings.timer = setTimeout(() => { S.savedNote = ''; renderDirty(); }, 6000);
    if (!wasConfigured && S.status.state === 'disconnected' && S.saved.connect_on_start) call('connect');
  }

  // ------------------------------------------------------------------ push-to-talk
  function padButton(name) {
    return S.padButtons.find((b) => b.name === name) || { name, xbox: name, playstation: name };
  }

  function padLabel(name) {
    const button = padButton(name);
    return S.pad.playstation ? button.playstation : button.xbox;
  }

  // The button in a sentence: "правый стик (RS)", but "A" and "LB" stay as they are.
  const inSentence = (label) => (/^[А-ЯЁ][а-яё]/.test(label) ? label[0].toLowerCase() + label.slice(1) : label);

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
    const options = [h('option', { value: '' }, 'Микрофон Windows по умолчанию'),
      ...names.map((name) => h('option', { value: name }, name))];
    if (form.mic_device && !names.includes(form.mic_device)) {
      options.push(h('option', { value: form.mic_device }, `${form.mic_device} (не подключён)`));
    }
    for (const select of $$('[data-mic]')) {
      select.replaceChildren(...options.map((o) => o.cloneNode(true)));
      select.value = form.mic_device || '';
      select.disabled = broken;
    }
    for (const button of $$('[data-mic-test]')) {
      button.textContent = S.micTest ? 'Стоп' : 'Проверить';
      button.disabled = broken;
    }
    const micHint = broken ? 'Микрофон не работает, пока агент не видит SDL3.dll.'
      : S.micError ? `Микрофон: ${S.micError}`
      : S.micTest ? 'Скажи что-нибудь: полоска должна доходить до середины.'
      : 'Агент слушает микрофон только пока идёт запись.';
    for (const hint of $$('[data-mic-hint]')) {
      hint.textContent = micHint;
      hint.classList.toggle('bad', broken || Boolean(S.micError));
    }

    // button
    for (const card of $$('[data-padcard]')) card.classList.toggle('cap', S.padCapture);
    for (const info of $$('[data-pad-info]')) {
      if (S.padCapture) {
        info.replaceChildren(
          h('span', { class: 'big pulse', role: 'status', style: 'font-size: 17px' }, 'Нажми кнопку на геймпаде…'),
          h('span', { class: 'hint' }, 'Esc отменит.'));
        continue;
      }
      const color = broken ? 'var(--bad)' : S.pad.connected ? 'var(--ok)' : 'var(--off)';
      const line = broken ? `Геймпад не работает: ${S.pad.error}`
        : S.padNote ? S.padNote
        : S.pad.connected ? `${S.pad.name} подключён`
        : 'Геймпад не найден. Подключи его, агент заметит сам.';
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
    const modeHint = form.ptt_mode === 'hold'
      ? 'Говоришь, пока держишь кнопку. Отпустил, и текст уходит в чат.'
      : `Нажми, говори, нажми ещё раз, и текст уйдёт в чат. Не дольше ${form.max_record_s} с.`;
    for (const hint of $$('[data-pttmode-hint]')) hint.textContent = modeHint;

    // what the game does with the same button
    const warning = !addon ? 'Без аддона игра тоже получит это нажатие. Выбери кнопку, на которой в игре ничего нет.'
      : form.ptt_button === 'rightstick' ? 'В WoW Forever правый стик ставит метки. Аддон заберёт эту кнопку себе, метки можно перенести на другую в настройках управления игры.'
      : form.ptt_button === 'misc1' ? 'Кнопку Share агент видит через GameInput: после смены кнопки перезапусти агент. Игра её, скорее всего, не видит, и значка записи в игре может не быть.'
      : 'Аддон заберёт эту кнопку у игры, пока он включён. Что на ней было, покажет проверка в игре.';
    for (const note of $$('[data-ptt-warn]')) {
      note.hidden = S.padCapture;
      note.replaceChildren(icon('warn'), h('span', {}, warning));
    }

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
      S.padNote = 'Не дождался нажатия. Нажми «Назначить» ещё раз.';
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
        `Запись ${Math.floor(up / 60)}:${String(up % 60).padStart(2, '0')}`);
    }
    if (rec.state === 'processing') return h('span', { class: 'rec busy pulse', role: 'status' }, 'Распознаю…');
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
    const mode = saved.ptt_mode === 'hold' ? 'удерживать' : 'нажал и нажал';
    const mic = saved.mic_device || 'микрофон по умолчанию';
    $('#st-pad-hint').textContent = S.pad.error ? `Не работает: ${S.pad.error}` : `${mic} · ${mode}`;
    const pill = recordingPill() || (S.pad.error ? h('span', { class: 'pill', style: 'color: var(--bad); border-color: var(--bad)' }, 'выключено')
      : S.pad.connected ? h('span', { class: 'pill', style: 'color: var(--muted); border-color: var(--bronze)' }, 'готов')
      : h('span', { class: 'pill', style: 'color: var(--off); border-color: var(--off)' }, 'нет геймпада'));
    $('#st-rec').replaceChildren(pill);
    $('#st-level').hidden = S.recording.state !== 'recording';
  }

  // The last wizard step: say something and see it arrive.
  function renderVoiceTest() {
    if (!S.form) return;
    const say = $('#w-voice-say');
    const label = h('b', { style: 'color: var(--gold)' }, inSentence(padLabel(S.form.ptt_button)));
    if (S.pad.error) say.replaceChildren(`Геймпад не работает: ${S.pad.error}`);
    else if (!S.pad.connected) say.replaceChildren('Подключи геймпад, чтобы проверить голос.');
    else if (S.form.ptt_mode === 'hold') say.replaceChildren('Зажми ', label, ', скажи «Всем привет» и отпусти.');
    else say.replaceChildren('Нажми ', label, ', скажи «Всем привет», нажми ещё раз.');
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
        v.status === 'ok' ? h('span', { class: 'mdetail' }, 'Ушло в чат игры.') : null));
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
    const rows = [
      { text: 'Игра запущена', now: 'Жду Wow.exe. Войди персонажем.' },
      { text: 'Окно WoW активно', now: 'Переключись в игру, остальное агент сделает сам.' },
      { text: 'Кнопка передана аддону', now: `Аддон просит нажать кнопку: нажми ${label}.` },
      { text: 'Аддон перезагрузил интерфейс', now: 'Экран игры мигнёт, это /reload.' },
      { text: 'Кнопка в игре', now: '' },
    ];
    const at = !c ? -1 : c.step === 'error' ? S.bind.last : BIND_STEPS.indexOf(c.step);
    return rows.map((row, i) => {
      let mark = 'todo';
      let { text } = row;
      let detail = '';
      if (c && c.step === 'done') {
        mark = 'ok';
        if (i === 4 && c.previous) {
          mark = 'warn';
          text = `Кнопка была на «${c.previous_name || c.previous}»`;
          detail = 'Аддон забрал её, пока он включён. Это действие можно перенести на другую кнопку в настройках управления игры.';
        } else if (i === 4) {
          text = 'Кнопка была свободна, аддон взял её себе';
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
          h('span', { style: `color: ${color}` }, text), detail ? h('span', { class: 'hint' }, detail) : null));
    });
  }

  function bindError(c) {
    const label = inSentence(padLabel((S.saved || S.form).ptt_button));
    switch (c.error) {
      case 'addon_missing':
        return ['Аддон не ответил', 'Проверь, что VoiceForever включён в списке модификаций, и нажми «Проверить ещё раз».'];
      case 'wrong_button':
        return ['Нажата другая кнопка', `${c.detail[0].toUpperCase()}${c.detail.slice(1)}. Проверь ещё раз и в игре нажми ${label}.`];
      case 'no_game_folder':
        return ['Не нашёл папку игры', 'Аддон всё равно взял кнопку: что на ней было, он написал в чат игры.'];
      case 'chat_mode':
        return ['Нужен аддон', 'Проверка работает только в режиме с аддоном.'];
      case 'not_connected':
        return ['Сначала сохрани настройки', ''];
      default:
        return ['Проверка не закончилась', c.detail ? `${c.detail[0].toUpperCase()}${c.detail.slice(1)}.` : ''];
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
        running ? 'Проверяю…' : c ? 'Проверить ещё раз' : 'Проверить в игре');
      const cancel = h('button', { class: 'link', type: 'button', style: 'align-self: center', onclick: () => call('cancel_game_binding') }, 'Отменить');
      actions.replaceChildren(...(running ? [run, cancel] : [run]));
    }
    $('#w-bind-intro').textContent = addon
      ? 'Запусти WoW и войди персонажем. Агент передаст кнопку аддону, аддон проверит бинды и перезагрузит интерфейс.'
      : 'Без аддона проверять нечего: нажатие получит и игра. Убедись, что на этой кнопке в игре ничего нет.';

    const line = $('#s-bind-line');
    if (!addon) line.textContent = 'Без аддона игра тоже видит эту кнопку.';
    else if (c && c.step === 'done') {
      line.textContent = c.previous
        ? `В игре кнопку держит аддон. Раньше на ней было «${c.previous_name || c.previous}».`
        : 'В игре кнопку держит аддон, она была свободна.';
    } else line.textContent = 'В игре кнопку забирает аддон. Проверь, что на ней было.';
    $('#s-bind-open').hidden = !addon || !$('#s-bind-box').hidden;
  }

  // ------------------------------------------------------------------ wiring
  function bindControls() {
    $('#btn-minimize').addEventListener('click', () => call('minimize'));
    $('#btn-close').addEventListener('click', () => call('close'));
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
        button.textContent = show ? 'Скрыть' : 'Показать';
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
      (left) => { $('#st-test-hint').textContent = `Жду окно WoW: осталось ${left} с`; },
      () => { $('#st-test-hint').textContent = TEST_HINT; }));
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
